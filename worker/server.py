"""Worker API + dashboard server (FastAPI, port 8000).

Run it with ``python -m worker.server``. Runs, events and reports are read from a ``RunStore``; a run's events are stored
by the runner and fanned out to WebSocket subscribers (``/api/runs/<id>/events``), which first receive a replay of
everything that happened so far. Approvals and questions raised by the agent are answered through
``POST /api/runs/<id>/respond``.

Two modes, chosen by ``Settings.db_auth`` (``AUTH_MODE``; ``auto`` = database mode when ``DATABASE_URL`` is set):

* **file mode** (v1): one shared password (``ACCESS_PASSWORD``) or no auth, runs in files, one run at a time, executed in
  this process (``launch_run``) with in-memory approvals.
* **database mode**: users, orgs and roles in PostgreSQL (``worker/auth.py``). Every endpoint is scoped to the caller's
  active organisation; another org's runs are indistinguishable from unknown ones (404). This process **never executes a
  run**: ``POST /api/runs`` queues it and a worker process (``python -m worker.jobs``) claims it. Approvals are database
  rows, live events travel through PostgreSQL NOTIFY (``worker/runs_api.py``, ``worker/live.py``), so any number of API
  and worker processes can run side by side.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from worker import jobqueue
from worker.admin_api import build_admin_router
from worker.agent.broker import InteractionBroker
from worker.agent.runner import AgentRunner
from worker.agent.state import utcnow
from worker.agent.toolset import build_registry
from worker.auth import (
    SESSION_COOKIE,
    AuthService,
    CsrfGuard,
    OrgContext,
    SessionGate,
    build_auth_router,
    require_role,
)
from worker.config import Settings, get_settings
from worker.db import migrate
from worker.db.models import Org
from worker.db.session import dispose_engine, get_sessionmaker
from worker.deploy import (
    PROXY_PREFIXES,
    AccessControl,
    DailyRunCap,
    install_access_control,
    install_sim_proxy,
)
from worker.event_log import synthesize_events
from worker.live import EventHub
from worker.llm.base import LLMClient, LLMError
from worker.llm.factory import exhausted_models, make_llm
from worker.llm.failover import active_model
from worker.logging_setup import setup_if_unconfigured, setup_logging
from worker.memory_api import build_memory_router
from worker.observability import ObservabilityMiddleware
from worker.org_settings import effective
from worker.pg_listen import dsn_of
from worker.provisioning import ProvisioningError, load_org_environment, org_chaos
from worker.run_context import headless_for, settings_for_run
from worker.runs_api import RespondRequest, RunRequest, build_runs_router  # noqa: F401 - re-exported
from worker.store import RUN_ID_RE, FileRunStore, RunStore
from worker.store_pg import PgRunStore
from worker.system_api import QueueDepthSampler, build_system_router, install_ops_routes
from worker.tools.registry import ToolRegistry
from worker.usage_api import build_usage_router
from worker.vault import Vault, VaultError, default_scope
from worker.vault_pg import open_pg_vault

log = logging.getLogger("worker.server")

UI_DIR = Path(__file__).parent / "ui"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = PROJECT_ROOT / "evals" / "scenarios"
SIM_TIMEOUT_S = 5.0

# Used when evals/scenarios/*.yaml is missing. ``chaos`` lists the sim flags the preset is meant to demonstrate.
BUILTIN_SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "invoice_entry",
        "title": "Latest invoice to ERP",
        "task": "Find the latest invoice from Company X, extract the amount and due date, enter it into our internal "
                "system, and tell me once it is done.",
        "chaos": [],
    },
    {
        "id": "invoice_entry_chaos",
        "title": "Invoice to ERP (server error + session expiry)",
        "task": "Find the latest invoice from Company X, extract the amount and due date, enter it into our internal "
                "system, and tell me once it is done.",
        "chaos": ["erp_submit_500_once", "erp_session_expiry"],
    },
    {
        "id": "invoice_duplicate",
        "title": "Invoice that is already in the ERP",
        "task": "Find the latest invoice from Company X, extract the amount and due date, enter it into our internal "
                "system, and tell me once it is done.",
        "chaos": ["duplicate_bill"],
    },
    {
        "id": "vendor_email_reply",
        "title": "Reply to a vendor payment question",
        "task": "Initech emailed asking when invoice INI-20502 will be paid. Check our records and reply to them with "
                "the expected payment date (the invoice due date).",
        "chaos": [],
    },
    {
        "id": "month_due_bills",
        "title": "Record every invoice due this month",
        "task": "Make sure every open vendor invoice due this month is recorded in our accounting system.",
        "chaos": [],
    },
]


# --------------------------------------------------------------------------- request models


class ResetRequest(BaseModel):
    chaos: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- run sessions


@dataclass
class RunSession:
    """File mode: one agent run executing in this process and everything the dashboard needs to follow it."""

    run_id: str
    task: str
    runner: AgentRunner
    broker: InteractionBroker
    store: RunStore
    last_seq: int = 0  # the newest stored event already fanned out to subscribers
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    task_handle: asyncio.Task | None = None
    finished: bool = False

    @property
    def active(self) -> bool:
        return self.task_handle is not None and not self.task_handle.done()

    async def publish(self) -> None:
        """Push the events the runner has stored since the last call to every subscriber."""
        if not self.subscribers:
            return  # late subscribers replay from the store and skip what they already have by ``seq``
        for event in await self.store.read_events(self.run_id, after_seq=self.last_seq):
            self.last_seq = event["seq"]
            for queue in list(self.subscribers):
                queue.put_nowait(event)

    def close_subscribers(self) -> None:
        for queue in list(self.subscribers):
            queue.put_nowait(None)


# --------------------------------------------------------------------------- helpers


def _valid_run_id(run_id: str) -> str:
    if not RUN_ID_RE.match(run_id):
        raise HTTPException(status_code=400, detail="Invalid run id")
    return run_id


def load_scenarios(directory: Path = SCENARIOS_DIR) -> list[dict[str, Any]]:
    """Preset tasks from ``evals/scenarios/*.yaml`` (fields: id, title, task, chaos), else the built-in list."""
    found: list[dict[str, Any]] = []
    if directory.is_dir():
        try:
            import yaml
        except ImportError:  # pragma: no cover - pyyaml is a project dependency
            yaml = None  # type: ignore[assignment]
        for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
            if yaml is None:
                break
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, yaml.YAMLError):
                continue
            if not isinstance(data, dict) or not str(data.get("task") or "").strip():
                continue
            chaos = data.get("chaos") or []
            if isinstance(chaos, dict):
                chaos = [name for name, on in chaos.items() if on]
            found.append({
                "id": str(data.get("id") or path.stem),
                "title": str(data.get("title") or data.get("name") or path.stem.replace("_", " ")),
                "task": str(data["task"]).strip(),
                "chaos": [str(c) for c in chaos] if isinstance(chaos, list) else [],
            })
    return found or [dict(s) for s in BUILTIN_SCENARIOS]


# --------------------------------------------------------------------------- app factory


def create_app(
    settings: Settings | None = None,
    llm_factory: Callable[..., LLMClient] | None = None,
    registry_factory: Callable[[Settings], ToolRegistry] | None = None,
    store: RunStore | None = None,
    vault: Vault | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    """Build the FastAPI app. ``llm_factory`` / ``registry_factory`` / ``store`` / ``vault`` / ``sessionmaker`` are seams for
    tests and deployments.

    File mode (``Settings.db_auth`` false): without ``vault``, the file vault in ``VAULT_DIR`` is used if it exists; runs get
    the credentials of the default owner and execute in this process. Database mode: ``sessionmaker`` defaults to the
    process-wide engine built from ``DATABASE_URL``; ``store`` (optional) must be a ``PgRunStore`` and is scoped per request
    to the caller's org; ``vault`` defaults to the PostgreSQL vault. The app only queues runs; workers (``worker.jobs``)
    execute them, and read ``llm_factory`` / ``registry_factory`` from ``app.state`` when a test embeds one."""
    base_settings = settings or get_settings()
    db_mode = base_settings.AUTH_MODE != "file" and (base_settings.db_auth or sessionmaker is not None)
    owns_engine = db_mode and sessionmaker is None
    sm = (sessionmaker or get_sessionmaker(base_settings)) if db_mode else None
    if db_mode and store is not None and not isinstance(store, PgRunStore):
        raise ValueError("database mode needs a PgRunStore (or none)")
    hub = EventHub(dsn_of(sm), poll_s=base_settings.LIVE_FALLBACK_POLL_S) if sm is not None else None

    sampler = QueueDepthSampler(sm, base_settings.QUEUE_SAMPLE_S) if sm is not None else None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        setup_if_unconfigured(base_settings)  # `uvicorn worker.server:app` skips main(); structured logs anyway
        if sm is not None:
            await check_schema(sm)
        if sampler is not None:
            sampler.start()
        yield
        if sampler is not None:
            await sampler.stop()
        close = getattr(app.state, "close_sim_client", None)
        if close is not None:
            await close()
        if hub is not None:
            await hub.stop()
        if owns_engine:
            await dispose_engine()
        try:
            from worker.tools.browser_pool import shutdown_pool
        except ImportError:
            return
        try:
            await shutdown_pool()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            log.exception("shutting down the browser pool failed")

    app = FastAPI(title="OneForAll Agent", version="2.0", lifespan=lifespan)
    app.state.llm_factory = llm_factory or make_llm
    app.state.registry_factory = registry_factory or build_registry
    app.state.settings = base_settings
    app.state.sim_http = None  # optional httpx client for the simulator's /admin API (tests route it in-process)
    app.state.sessionmaker = sm
    app.state.hub = hub
    sessions: dict[str, RunSession] = {}  # file mode only: runs executing in this process
    app.state.sessions = sessions

    service: AuthService | None = AuthService(sm, base_settings) if sm is not None else None
    app.state.auth = service
    pg_base: PgRunStore | None = None
    if sm is not None:
        # The base store is only a template: ``for_org`` hands every request a store scoped to the caller's org.
        pg_base = store or PgRunStore(sm, uuid.UUID(int=0), artifacts_root=base_settings.RUNS_DIR)
    run_store: RunStore = pg_base if pg_base is not None else (store or FileRunStore(base_settings.RUNS_DIR))
    app.state.store = run_store

    pg_vault_cache: list[Vault] = []

    def org_vault() -> Vault:
        """The PostgreSQL vault, opened on first use (so building the app creates no key file)."""
        if vault is not None:
            return vault
        if not pg_vault_cache:
            pg_vault_cache.append(open_pg_vault(sm, base_settings))
        return pg_vault_cache[0]

    app.state.org_vault = org_vault if db_mode else None
    access = AccessControl("" if db_mode else base_settings.ACCESS_PASSWORD, base_settings.SESSION_SECRET)
    run_cap = DailyRunCap(0 if db_mode else base_settings.MAX_RUNS_PER_DAY)
    app.state.access, app.state.run_cap = access, run_cap
    if db_mode and base_settings.ACCESS_PASSWORD:
        log.warning("ACCESS_PASSWORD is ignored in database mode: users sign in with their own accounts.")

    # ----------------------------------------------------------------- scoping helpers

    def store_for(ctx: OrgContext | None) -> RunStore:
        """The run store a request reads and writes: the caller's org only (database mode), else the one store."""
        return run_store if ctx is None else pg_base.for_org(ctx.org_id, ctx.user_id)  # type: ignore[union-attr]

    def active_session() -> RunSession | None:
        return next((s for s in sessions.values() if s.active), None)

    async def org_is_busy(org_id: uuid.UUID) -> bool:
        return await jobqueue.org_has_live_runs(sm, org_id)  # type: ignore[arg-type]

    async def load_org(org_id: uuid.UUID) -> Org:
        async with sm() as session:  # type: ignore[misc]
            org = await session.get(Org, org_id)
        if org is None:
            raise HTTPException(status_code=404, detail="Unknown organization")
        return org

    async def known_run(ctx: OrgContext | None, run_id: str) -> bool:
        """Is this run visible to the caller? (In database mode: in the caller's org.)"""
        if ctx is None:
            return True
        return await store_for(ctx).exists(run_id)  # type: ignore[attr-defined]

    async def runs_left(ctx: OrgContext | None) -> tuple[int | None, int | None]:
        """(runs allowed per UTC day, runs still available today); None = unlimited."""
        if ctx is None:
            return (run_cap.limit or None), run_cap.remaining()
        limit = effective((await load_org(ctx.org_id)).settings, base_settings)["max_runs_per_day"]
        if not limit:
            return None, None
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        used = await store_for(ctx).count_runs_since(today)  # type: ignore[attr-defined]
        return limit, max(0, limit - used)

    async def public_config(ctx: OrgContext | None) -> dict:
        cfg: Settings = app.state.settings
        limit, left = await runs_left(ctx)
        return {"deploy_mode": cfg.DEPLOY_MODE, "sim_proxy": cfg.PUBLIC_SIM_PROXY,
                "auth_enabled": db_mode or access.enabled, "max_runs_per_day": limit, "runs_left_today": left,
                **({"auth_mode": "db"} if db_mode else {})}

    # ----------------------------------------------------------------- runs

    if db_mode:
        app.include_router(build_runs_router(service=service, sessionmaker=sm, settings=base_settings, hub=hub,
                                             get_vault=org_vault, base_store=pg_base))
    else:
        _install_file_mode_runs(app, sessions, base_settings, run_cap, run_store, vault)

    # ----------------------------------------------------------------- files

    @app.get("/runs/{run_id}/files/{file_path:path}")
    async def run_file(run_id: str, file_path: str, ctx: OrgContext | None = Depends(require_role("viewer"))):
        _valid_run_id(run_id)
        if not await known_run(ctx, run_id):  # the org check comes before anything on disk is looked at
            raise HTTPException(status_code=404, detail="Not found")
        st = store_for(ctx)
        if file_path == "report.md":
            report_md = await st.load_report_markdown(run_id)
            if report_md is not None:
                return Response(report_md, media_type="text/markdown; charset=utf-8")
        target = st.artifact_file(run_id, file_path)
        if target is None:
            raise HTTPException(status_code=404, detail="Not found")
        if target.suffix.lower() == ".md":
            return Response(target.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")
        return FileResponse(target)

    # ----------------------------------------------------------------- scenarios, sim proxy, health

    @app.get("/api/scenarios")
    async def scenarios(ctx: OrgContext | None = Depends(require_role("viewer"))) -> list[dict]:
        return load_scenarios()

    async def sim_call(method: str, path: str, body: dict | None = None) -> Any:
        url = app.state.settings.SIM_BASE_URL.rstrip("/") + path
        try:
            if app.state.sim_http is not None:
                resp = await app.state.sim_http.request(method, url, json=body, headers=app.state.settings.sim_admin_headers)
            else:
                async with httpx.AsyncClient(timeout=SIM_TIMEOUT_S) as client:
                    resp = await client.request(method, url, json=body, headers=app.state.settings.sim_admin_headers)
        except httpx.HTTPError as err:
            raise HTTPException(status_code=502, detail=f"Simulated company is not reachable at {url}: {err}") from err
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
        return resp.json()

    @app.post("/api/sim/reset")
    async def sim_reset(req: ResetRequest, ctx: OrgContext | None = Depends(require_role("admin"))) -> Any:
        if ctx is not None:  # database mode never touches the shared default tenant: reset the org's own
            return RedirectResponse("/api/admin/environment/reset", status_code=307)
        if active_session() is not None:
            raise HTTPException(status_code=409, detail="A run is active; resetting the environment now would corrupt it.")
        return await sim_call("POST", "/admin/reset", {"chaos": req.chaos})

    @app.get("/api/sim/chaos")
    async def sim_chaos(ctx: OrgContext | None = Depends(require_role("viewer"))) -> Any:
        if ctx is None:
            return await sim_call("GET", "/admin/chaos")
        tenant_id = (await load_org(ctx.org_id)).sim_tenant_id
        if not tenant_id:
            raise HTTPException(status_code=409, detail="This organization has no environment yet.")
        try:
            return await org_chaos(tenant_id, app.state.settings, app.state.sim_http)
        except ProvisioningError as err:
            if err.status == 404:  # the simulator is up but lost this org's tenant (its data dir was wiped)
                raise HTTPException(status_code=409, detail=(
                    "This organization's simulated company is missing from the simulator. An admin can recreate it in "
                    "Admin > Simulated company > Provision environment, or restart with python run.py.")) from None
            raise HTTPException(status_code=502, detail=str(err)) from None

    @app.get("/api/environment/enter", include_in_schema=False)
    async def enter_environment(ctx: OrgContext | None = Depends(require_role("operator"))):
        """Open the org's own simulated company in the browser (via the proxy): sets the tenant cookie, then redirects."""
        if ctx is None or not base_settings.PUBLIC_SIM_PROXY:
            raise HTTPException(status_code=404, detail="Not found")
        environment = await load_org_environment(await load_org(ctx.org_id), org_vault())
        entry = environment.scope.internal("sim_entry") if environment else None
        if entry is None:
            raise HTTPException(status_code=409, detail="This organization has no environment yet.")
        return RedirectResponse(f"/t/{entry.fields['value']}/?next=/company", status_code=303)

    @app.get("/api/health")
    async def health(request: Request) -> dict:
        current: Settings = app.state.settings
        ctx: OrgContext | None = None
        if service is not None:
            ctx = await service.resolve(request.cookies.get(SESSION_COOKIE))
            if ctx is None:
                return {"status": "ok"}  # public health check: nothing about the deployment
        elif access.enabled and not access.is_authenticated(dict(request.cookies)):
            return {"status": "ok"}
        busy = active_session() if ctx is None else None
        active_run = busy.run_id if busy else (await jobqueue.newest_live_run(sm, ctx.org_id) if ctx else None)
        chain = current.gemini_models
        model = {"gemini": active_model(chain), "ollama": current.OLLAMA_MODEL}.get(current.LLM_PROVIDER, "scripted")
        in_use = getattr(busy.runner.llm, "active_model", None) if busy else None
        if in_use and current.LLM_PROVIDER == "gemini":
            model = in_use
        unavailable = await exhausted_models(current, chain, sm) if current.LLM_PROVIDER == "gemini" else []
        try:
            await sim_call("GET", "/admin/chaos")
            sim_ok = True
        except HTTPException:
            sim_ok = False
        return {
            "status": "ok", "provider": current.LLM_PROVIDER, "model": model, "models_unavailable": unavailable,
            "approval_mode": current.APPROVAL_MODE, "sim_reachable": sim_ok, "sim_url": current.SIM_BASE_URL,
            "api_key_set": bool(current.GEMINI_API_KEY) or current.LLM_PROVIDER != "gemini",
            "headless": current.HEADLESS or current.DEPLOY_MODE, "active_run": active_run,
            **await public_config(ctx),
        }

    @app.get("/api/config")
    async def config(ctx: OrgContext | None = Depends(require_role("viewer"))) -> dict:
        return await public_config(ctx)

    install_ops_routes(app, settings=base_settings, service=service, sessionmaker=sm, access=access)  # /healthz /readyz /metrics

    # ----------------------------------------------------------------- auth and administration

    if service is not None:
        app.include_router(build_auth_router(service))
        app.include_router(build_admin_router(service=service, get_vault=org_vault, settings=base_settings,
                                              is_busy=org_is_busy, sim_client=lambda: app.state.sim_http))
        app.include_router(build_usage_router(sessionmaker=sm))
        app.include_router(build_system_router(sessionmaker=sm, settings=base_settings))
        app.include_router(build_memory_router(service=service, sessionmaker=sm, settings=base_settings))
    else:
        @app.get("/api/auth/me")
        async def auth_me() -> dict:
            """The dashboard asks this to learn which mode it is in."""
            return {"mode": "file"}

    # ----------------------------------------------------------------- dashboard

    @app.middleware("http")
    async def no_cache_ui(request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/ui/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/", include_in_schema=False)
    async def index():
        index_file = UI_DIR / "index.html"
        if not index_file.is_file():
            return JSONResponse({"detail": "Dashboard files are missing (worker/ui/index.html)."}, status_code=404)
        return FileResponse(index_file)

    if UI_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")
    if base_settings.PUBLIC_SIM_PROXY:
        install_sim_proxy(app, base_settings)
    if service is not None:
        app.add_middleware(CsrfGuard)
        if base_settings.PUBLIC_SIM_PROXY:
            app.add_middleware(SessionGate, service=service, prefixes=tuple(f"/{p}" for p in PROXY_PREFIXES) + ("/company",))
    elif access.enabled:
        install_access_control(app, access)
    app.add_middleware(ObservabilityMiddleware)  # last = outermost: request ids and metrics also cover what the others reject
    return app


def _install_file_mode_runs(app: FastAPI, sessions: dict[str, RunSession], base_settings: Settings, run_cap: DailyRunCap,
                            run_store: RunStore, vault: Vault | None) -> None:
    """The v1 run endpoints: one run at a time, executed in this process, approvals in memory (no auth, or the shared
    password enforced by middleware)."""
    launch_lock = asyncio.Lock()

    async def launch_run(req: RunRequest) -> RunSession:
        """Create and start a run: daily cap, the one-at-a-time rule, the runner and its supervising task."""
        task_text = req.task.strip()
        if not task_text:
            raise HTTPException(status_code=422, detail="Task is empty")
        async with launch_lock:  # two simultaneous requests must not both pass the checks
            busy = next((s for s in sessions.values() if s.active), None)
            if busy is not None:
                raise HTTPException(status_code=409, detail=f"Run {busy.run_id} is still active; cancel it or wait for it to finish.")
            if not run_cap.allow():
                raise HTTPException(
                    status_code=429, headers={"Retry-After": str(run_cap.seconds_until_reset())},
                    detail=f"Daily demo limit reached: this deployment allows {run_cap.limit} runs per day (UTC) to "
                           "protect the shared Gemini quota. Please try again tomorrow.")
            run_settings = settings_for_run(app.state.settings, org_cfg=None, requested_mode=req.approval_mode,
                                            headless=headless_for(app.state.settings, req.headed))
            try:
                llm = app.state.llm_factory(run_settings)
                registry = app.state.registry_factory(run_settings)
            except LLMError as err:
                raise HTTPException(status_code=400, detail=str(err)) from err
            try:
                vault_scope = await default_scope(run_settings, vault=vault)
            except VaultError as err:
                raise HTTPException(status_code=500, detail=f"The credential vault cannot be opened: {err}") from err
            broker = InteractionBroker()  # the dashboard user answers when ready: no timeout
            session_holder: dict[str, RunSession] = {}

            async def sink(event_type: str, payload: dict) -> None:
                await session_holder["s"].publish()

            runner = AgentRunner(task_text, llm, registry, run_settings, broker, event_sink=sink, store=run_store,
                                 vault_scope=vault_scope)
            session = RunSession(run_id=runner.state.run_id, task=task_text, runner=runner, broker=broker, store=run_store)
            session_holder["s"] = session
            sessions[session.run_id] = session
            run_cap.record()
            session.task_handle = asyncio.create_task(_supervise(session), name=f"run-{session.run_id}")
            return session

    async def _supervise(session: RunSession) -> None:
        try:
            await session.runner.run()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the runner already converts its own errors; this is a last resort
            log.exception("run %s crashed", session.run_id)
        finally:
            session.finished = True
            session.close_subscribers()

    @app.post("/api/runs")
    async def start_run(req: RunRequest) -> dict:
        session = await launch_run(req)
        return {"run_id": session.run_id, "started_by": None}

    @app.get("/api/runs")
    async def list_runs() -> list[dict]:
        rows = {row["id"]: row for row in await run_store.list_runs()}
        for session in sessions.values():  # runs that have not been stored yet
            rows.setdefault(session.run_id, {"id": session.run_id, "task": session.task, "status": "pending",
                                             "started_at": session.runner.state.started_at, "finished_at": None})
            if session.active:
                rows[session.run_id]["status"] = session.runner.state.status
        for row in rows.values():
            row["started_by"] = None
        return sorted(rows.values(), key=lambda r: r["started_at"], reverse=True)

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict:
        _valid_run_id(run_id)
        session = sessions.get(run_id)
        if session is not None:
            data = session.runner.state.to_dict()
            data["pending"] = session.broker.pending()
            data["active"] = session.active
        else:
            stored = await run_store.load_state(run_id)
            if stored is None:
                raise HTTPException(status_code=404, detail="Unknown run")
            data = stored.to_dict()
            data["pending"], data["active"] = [], False
        data["started_by"] = None
        data["report"] = data.get("final_report")
        has_report = await run_store.load_report_markdown(run_id) is not None
        data["report_url"] = f"/runs/{run_id}/files/report.md" if has_report else None
        return json.loads(json.dumps(data, default=str))

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict:
        _valid_run_id(run_id)
        session = sessions.get(run_id)
        if session is None or not session.active:
            raise HTTPException(status_code=409, detail="This run is not active")
        session.runner.cancel()
        return {"status": "cancelling"}

    @app.post("/api/runs/{run_id}/respond")
    async def respond(run_id: str, req: RespondRequest) -> dict:
        _valid_run_id(run_id)
        session = sessions.get(run_id)
        if session is None or not session.active:
            raise HTTPException(status_code=409, detail="This run is not active")
        request = {p["request_id"]: p for p in session.broker.pending()}.get(req.request_id)
        if request is None:
            raise HTTPException(status_code=404, detail="No such pending request (already answered?)")
        if request["kind"] == "approval":
            response: dict[str, Any] = {"approved": bool(req.approved), "note": req.note or "", "remember": bool(req.remember)}
        else:
            response = {"answer": (req.answer if req.answer is not None else req.note) or ""}
        if not session.broker.resolve(req.request_id, response):
            raise HTTPException(status_code=409, detail="Request was already resolved")
        return {"status": "ok", "kind": request["kind"]}

    @app.websocket("/api/runs/{run_id}/events")
    async def run_events(ws: WebSocket, run_id: str) -> None:
        if not RUN_ID_RE.match(run_id):
            await ws.close(code=1008)
            return
        await ws.accept()
        session = sessions.get(run_id)
        queue: asyncio.Queue | None = None
        if session is not None:
            # Subscribe before reading the store; events that arrive on both paths are dropped by ``seq`` below.
            if session.active:
                queue = asyncio.Queue()
                session.subscribers.add(queue)
            replay = await run_store.read_events(run_id)
        else:
            replay = await run_store.read_events(run_id)
            if not replay:
                state = await run_store.load_state(run_id)
                replay = synthesize_events(state.to_dict()) if state is not None else None
            if replay is None:
                await ws.send_json({"seq": 0, "type": "error", "ts": utcnow(), "payload": {"message": "Unknown run"}})
                await ws.close(code=1000)
                return
        try:
            for event in replay:
                await ws.send_json(event)
            sent = replay[-1]["seq"] if replay else 0
            await ws.send_json({"seq": -1, "type": "replay_done", "ts": utcnow(), "payload": {"live": queue is not None}})
            if queue is not None:
                while True:
                    event = await queue.get()
                    if event is None:
                        break
                    if event["seq"] > sent:
                        await ws.send_json(event)
            await ws.close(code=1000)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if session is not None and queue is not None:
                session.subscribers.discard(queue)


async def check_schema(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    """Log a clear error at startup when the database is unreachable or not migrated to the current head."""
    try:
        async with sessionmaker() as session:
            current = (await session.execute(text("select version_num from alembic_version"))).scalar()
    except Exception as exc:  # noqa: BLE001 - the server still starts; requests will report the database problem
        log.error("The database is not usable (%s). Run: python scripts/bootstrap.py", type(exc).__name__)
        return
    if current != migrate.head_revision():
        log.error("The database schema is at revision %s but the code needs %s. Run: python -m worker.db migrate",
                  current, migrate.head_revision())


app = create_app()


def main() -> None:
    """Entry point: ``python -m worker.server``."""
    import uvicorn

    setup_logging(get_settings())  # JSON lines in DEPLOY_MODE (LOG_FORMAT), readable text otherwise
    uvicorn.run("worker.server:app", host="127.0.0.1", port=8000, log_config=None, access_log=False)  # the middleware logs requests


if __name__ == "__main__":
    main()
