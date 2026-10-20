"""Tenants: one isolated simulated company per organization, all served by one process.

The simulator stands in for each customer's *own* external systems (mail, vendor portal, ERP), so every tenant gets
a private copy: its own SQLite database and invoice PDFs, login sessions, chaos flags and credentials.

* The ``default`` tenant is the original single-company simulator and keeps the legacy paths and credentials
  (``config.DB_PATH``, ``config.PDF_DIR``, ``config.*_USER/PASSWORD/API_KEY``), so everything written for v1 works.
* Any other tenant lives in ``DATA_DIR/tenants/<tenant_id>/`` (``sim.db`` and ``pdfs/``). A small SQLite file,
  ``DATA_DIR/tenants/index.db``, maps tenant id -> secret entry token, API key, generated credentials, chaos flags.
* ``current_tenant`` (a contextvar) selects the tenant for ``db.connect()``, the PDF directory, sessions, chaos flags
  and credentials. The web layer sets it per request (see ``tenant_web.py``); in-process callers use ``use_tenant``.
* A tenant id is never accepted from a client. Browsers arrive through ``/t/<token>/`` (a cookie is set) and the ERP
  REST API recognises the tenant by its API key; both are random, unguessable strings.
* With ``SIM_TENANT_SECRET`` set they are not random but derived: HMAC-SHA256(secret, ``<tenant id>:<purpose>``), in the
  same format and length as the random ones. Every simulator that shares the secret then creates the *same* tenant for a
  given id, so a container that restarts (its SQLite data is ephemeral) comes back with the logins and entry token that the
  platform's vault already holds. Without the secret nothing changes. The seed salt already defaults to the tenant id.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import shutil
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config

DEFAULT = "default"
COOKIE = "sim_tenant"
TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
APPS = ("mail", "portal", "erp")

current_tenant: ContextVar[str] = ContextVar("company_sim_tenant", default=DEFAULT)

INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    api_key TEXT NOT NULL UNIQUE,
    credentials TEXT NOT NULL,
    chaos TEXT NOT NULL,
    seed_salt TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


class UnknownTenant(KeyError):
    """No tenant with this id (or token / key) exists."""


@dataclass(frozen=True)
class Tenant:
    """One company environment. ``credentials`` is ``{mail, portal, erp: {username, password}, erp_api_key}``."""

    id: str
    token: str
    api_key: str
    credentials: dict
    chaos: tuple[str, ...] = ()
    seed_salt: str = ""
    created_at: str = ""

    @property
    def is_default(self) -> bool:
        return self.id == DEFAULT

    @property
    def entry_path(self) -> str:
        return f"/t/{self.token}/"

    def provisioning_info(self, base_url: str) -> dict:
        """What the platform needs to use this tenant: where to start, and the logins (kept out of the shared workspace)."""
        return {
            "tenant_id": self.id, "entry_path": self.entry_path, "entry_url": base_url.rstrip("/") + self.entry_path,
            "token": self.token, "credentials": self.credentials, "chaos": list(self.chaos),
            "seed_salt": self.seed_salt, "created_at": self.created_at,
        }


# --------------------------------------------------------------------------- paths


def tenants_dir() -> Path:
    return config.DATA_DIR / "tenants"


def index_path() -> Path:
    return tenants_dir() / "index.db"


def _checked(tenant_id: str) -> str:
    if tenant_id != DEFAULT and not TENANT_ID_RE.match(tenant_id):
        raise ValueError(f"Invalid tenant id {tenant_id!r}: use 1-48 chars of a-z, 0-9, '-' and '_'")
    return tenant_id


def db_path(tenant_id: str | None = None) -> Path:
    """SQLite file of a tenant (default: the current one). The default tenant keeps ``config.DB_PATH``."""
    tid = _checked(tenant_id or current_tenant.get())
    return config.DB_PATH if tid == DEFAULT else tenants_dir() / tid / "sim.db"


def pdf_dir(tenant_id: str | None = None) -> Path:
    tid = _checked(tenant_id or current_tenant.get())
    return config.PDF_DIR if tid == DEFAULT else tenants_dir() / tid / "pdfs"


# --------------------------------------------------------------------------- the index


@contextmanager
def _index(create: bool = False) -> Iterator[sqlite3.Connection | None]:
    """Index connection (committed on success). ``None`` when no tenant was ever created and ``create`` is false,
    so a sim that only serves the default tenant never writes a ``tenants/`` folder."""
    path = index_path()
    if not path.exists():
        if not create:
            yield None
            return
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(INDEX_SCHEMA)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _from_row(row: sqlite3.Row) -> Tenant:
    return Tenant(id=row["id"], token=row["token"], api_key=row["api_key"], credentials=json.loads(row["credentials"]),
                  chaos=tuple(json.loads(row["chaos"])), seed_salt=row["seed_salt"], created_at=row["created_at"])


def _select(where: str, params: tuple) -> Tenant | None:
    with _index() as conn:
        row = conn.execute(f"SELECT * FROM tenants WHERE {where}", params).fetchone() if conn else None
    return _from_row(row) if row else None


def default_tenant() -> Tenant:
    """The legacy single company, built from ``config`` on each call (tests may monkeypatch it)."""
    creds = {
        "mail": {"username": config.MAIL_USER, "password": config.MAIL_PASSWORD},
        "portal": {"username": config.PORTAL_USER, "password": config.PORTAL_PASSWORD},
        "erp": {"username": config.ERP_USER, "password": config.ERP_PASSWORD},
        "erp_api_key": config.ERP_API_KEY,
    }
    return Tenant(id=DEFAULT, token="", api_key=config.ERP_API_KEY, credentials=creds)


def get_tenant(tenant_id: str) -> Tenant | None:
    """Tenant by id (server-side use only; clients are identified by token or API key)."""
    if tenant_id == DEFAULT:
        return default_tenant()
    if not TENANT_ID_RE.match(tenant_id or ""):
        return None
    return _select("id = ?", (tenant_id,))


def find_by_token(token: str) -> Tenant | None:
    """Tenant owning this entry token (the ``default`` tenant has none)."""
    return _select("token = ?", (token,)) if token else None


def find_by_api_key(api_key: str) -> Tenant | None:
    """Tenant owning this ERP API key, including the default tenant's legacy key."""
    if not api_key:
        return None
    if secrets.compare_digest(api_key.encode(), config.ERP_API_KEY.encode()):
        return default_tenant()
    return _select("api_key = ?", (api_key,))


def list_tenants() -> list[Tenant]:
    """All provisioned tenants (not the implicit default), oldest first."""
    with _index() as conn:
        rows = conn.execute("SELECT * FROM tenants ORDER BY created_at, id").fetchall() if conn else []
    return [_from_row(r) for r in rows]


def save_chaos(tenant_id: str, flags: Iterable[str]) -> None:
    """Persist a tenant's chaos flags so they survive a restart of the sim (the default tenant uses SIM_CHAOS)."""
    if tenant_id == DEFAULT:
        return
    with _index() as conn:
        if conn:
            conn.execute("UPDATE tenants SET chaos = ? WHERE id = ?", (json.dumps(sorted(flags)), tenant_id))


# --------------------------------------------------------------------------- current tenant


def current() -> Tenant:
    tenant = get_tenant(current_tenant.get())
    if tenant is None:
        raise UnknownTenant(current_tenant.get())
    return tenant


def current_credentials(app_key: str) -> dict:
    """``{username, password}`` of one app (mail, portal, erp) for the current tenant."""
    return current().credentials[app_key]


@contextmanager
def use_tenant(tenant_id: str) -> Iterator[Tenant]:
    """Run the enclosed code against this tenant (``db``, ``chaos``, ``auth``, PDFs ...). Raises ``UnknownTenant``."""
    tenant = get_tenant(tenant_id)
    if tenant is None:
        raise UnknownTenant(tenant_id)
    marker = current_tenant.set(tenant.id)
    try:
        yield tenant
    finally:
        current_tenant.reset(marker)


# --------------------------------------------------------------------------- provisioning


def _secret(tenant_id: str, purpose: str, nbytes: int) -> str:
    """A secret string like ``secrets.token_urlsafe(nbytes)``: random, or (``SIM_TENANT_SECRET`` set) derived from the
    shared secret, the tenant id and the purpose."""
    key = config.tenant_secret()
    if not key:
        return secrets.token_urlsafe(nbytes)
    digest = hmac.new(key.encode(), f"{tenant_id}:{purpose}".encode(), hashlib.sha256).digest()[:nbytes]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _new_credentials(tenant_id: str) -> dict:
    default = default_tenant().credentials
    return {
        **{app: {"username": default[app]["username"], "password": _secret(tenant_id, f"{app}-password", 12)} for app in APPS},
        "erp_api_key": "ledger-" + _secret(tenant_id, "erp-api-key", 24),
    }


def _new_tenant(tenant_id: str, flags: list[str], seed_salt: str) -> Tenant:
    creds = _new_credentials(tenant_id)
    return Tenant(id=tenant_id, token=_secret(tenant_id, "entry-token", 24), api_key=creds["erp_api_key"], credentials=creds,
                  chaos=tuple(flags), seed_salt=seed_salt, created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))


def _insert(tenant: Tenant) -> bool:
    """Store a new tenant; False if the id is already taken."""
    with _index(create=True) as conn:
        try:
            conn.execute(
                "INSERT INTO tenants (id, token, api_key, credentials, chaos, seed_salt, created_at) VALUES (?,?,?,?,?,?,?)",
                (tenant.id, tenant.token, tenant.api_key, json.dumps(tenant.credentials), json.dumps(sorted(tenant.chaos)),
                 tenant.seed_salt, tenant.created_at))
        except sqlite3.IntegrityError:
            if _select("id = ?", (tenant.id,)) is not None:
                return False
            raise
    return True


def reset_tenant(tenant_id: str, chaos: Iterable[str] | None = None) -> dict:
    """Wipe and reseed one tenant (optionally replacing its chaos flags first). Only this tenant's sessions are dropped."""
    from . import auth, seed
    from . import chaos as chaos_flags

    with use_tenant(tenant_id):
        if chaos is not None:
            chaos_flags.set_flags(chaos)
        summary = seed.seed_database()
        chaos_flags.reset_counters()
        auth.clear_sessions(tenant_id)
    return summary


def provision_tenant(tenant_id: str | None = None, chaos: Iterable[str] | None = None,
                     seed_salt: str | None = None) -> Tenant:
    """Create a tenant (random id when omitted), seed its database and return it.

    Idempotent for an existing id: it is returned as is, or reset with the new flags when ``chaos`` is given.
    ``seed_salt`` varies the generated amounts (default: the tenant id, so the same id gives the same dataset).
    """
    from . import auth
    from . import chaos as chaos_flags

    flags = sorted(chaos_flags.validate(chaos)) if chaos is not None else None
    tid = _checked(tenant_id) if tenant_id else "t-" + secrets.token_hex(4)
    if tid == DEFAULT:
        raise ValueError("The default tenant is built in and cannot be provisioned")
    existing = get_tenant(tid)
    if existing is not None:
        if flags is not None:
            reset_tenant(tid, flags)
            existing = get_tenant(tid)
        return existing
    tenant = _new_tenant(tid, flags or [], tid if seed_salt is None else seed_salt)
    if not _insert(tenant):  # lost a race with a concurrent provision of the same id
        return provision_tenant(tid, flags)
    chaos_flags.forget(tid)  # drop state a previous tenant with this id may have left in memory
    auth.clear_sessions(tid)
    try:
        reset_tenant(tid)
    except BaseException:
        delete_tenant(tid)
        raise
    return tenant


def delete_tenant(tenant_id: str) -> bool:
    """Remove a tenant and all its data. The default tenant cannot be deleted. False if it did not exist."""
    from . import auth
    from . import chaos as chaos_flags

    if tenant_id == DEFAULT or not TENANT_ID_RE.match(tenant_id or ""):
        return False
    with _index() as conn:
        removed = bool(conn and conn.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,)).rowcount)
    shutil.rmtree(tenants_dir() / tenant_id, ignore_errors=True)
    chaos_flags.forget(tenant_id)
    auth.clear_sessions(tenant_id)
    return removed


def tenant_state(tenant_id: str = DEFAULT) -> dict:
    """Ground truth of one tenant (what ``GET /admin/state`` returns): bills, sent mail, the invoices and inbox the
    sim actually holds, chaos flags, SIM_TODAY and the id of this simulator process (``config.INSTANCE_ID``).

    Reads the tenant's database directly, with no HTTP and no login; this is what evals compare the agent against.
    """
    from . import chaos as chaos_flags
    from . import state

    with use_tenant(tenant_id) as tenant:
        return {
            "tenant": tenant.id, "instance_id": config.INSTANCE_ID, "sim_today": config.SIM_TODAY.isoformat(),
            "seed_salt": tenant.seed_salt, "chaos": sorted(chaos_flags.active()), "bills": state.get_bills(),
            "sent_mail": state.get_sent_mail(), "invoices": state.invoice_rows(), "inbox": state.get_inbox(),
        }
