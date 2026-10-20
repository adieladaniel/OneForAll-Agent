"""Ground-truth snapshots of the simulated company, used by the checkers.

``fetch_admin_state`` reads ``GET /admin/state`` (bills, sent mail, the invoices and inbox the sim actually holds,
SIM_TODAY, active chaos flags, the sim's instance id) over HTTP, so the evals work against a sim that runs as a separate
process; if the sim is unreachable it falls back to reading the sim database directly (``company_sim.state``), which
carries no instance id. With a ``tenant_id`` it reads that tenant's ``/admin/tenants/<id>/state`` instead, and
``provision_tenant`` / ``delete_tenant`` create and drop the per-scenario tenants of parallel runs.

``build_sim_state`` then adds the facts the checkers compare against. The ground truth is the sim's *actual* rows
(invoices, inbox), taken from the snapshot made right after the reset, never what the agent claims. The seed formula
(``seed.invoice_specs`` / ``seed.email_specs`` for that date, flag set and salt) is only a cross-check: when it disagrees
with the actual rows, or the baseline and the final snapshot come from different sims, ``env_errors`` lists the
differing fields and the scenario is reported as an environment error instead of being graded. Nothing is hard-coded to a date.
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx

from company_sim import seed
from company_sim.pdfgen import totals

HTTP_TIMEOUT_S = 10.0
PROVISION_TIMEOUT_S = 60.0
AMOUNT_TOLERANCE = 0.005
INVOICE_FIELDS = ("vendor", "issue_date", "due_date", "amount", "currency", "status", "portal_visible")
INBOX_FIELDS = ("recipient", "sent_at", "body", "attachment_invoice")


def admin_headers() -> dict[str, str]:
    """``X-Admin-Token`` for the sim's /admin endpoints when ``SIM_ADMIN_TOKEN`` is set in the environment."""
    token = os.environ.get("SIM_ADMIN_TOKEN", "")
    return {"X-Admin-Token": token} if token else {}


def expected_invoices(today: date, flags: set[str], salt: str = "") -> list[dict]:
    """All invoices of the seed for ``today``, ``flags`` and a tenant's ``salt`` (including portal-hidden ones), as plain dicts."""
    rows = []
    for spec in seed.invoice_specs(today, set(flags), salt):
        _, _, total = totals(spec["items"], seed.TAX_BY_CURRENCY[spec["currency"]])
        rows.append({
            "number": spec["number"], "vendor": spec["vendor"], "amount": total, "currency": spec["currency"],
            "issue_date": spec["issue"].isoformat(), "due_date": spec["due"].isoformat(),
            "status": spec["status"], "portal_visible": bool(spec["visible"]),
        })
    return rows


def latest_invoice(invoices: list[dict], vendor: str) -> dict | None:
    """Most recent invoice (by issue date) of an exact vendor name, like ``company_sim.state.latest_invoice_for``."""
    mine = [i for i in invoices if i["vendor"].lower() == vendor.strip().lower()]
    return max(mine, key=lambda i: i["issue_date"]) if mine else None


def expected_inbox(today: date, invoices: list[dict]) -> list[dict]:
    """Inbox messages of the seed (sender, subject, body, ...)."""
    latest = latest_invoice(invoices, "Company X Ltd")
    return seed.email_specs(today, latest["number"] if latest else "")


def _direct_admin_state(flags: list[str], tenant_id: str | None = None) -> dict:
    """Fallback when the sim is not reachable over HTTP: read its database directly. The result has no instance id."""
    from company_sim import tenants

    if tenant_id:
        return {**tenants.tenant_state(tenant_id), "instance_id": None}
    return {**tenants.tenant_state(tenants.DEFAULT), "chaos": sorted(flags), "instance_id": None}


def fetch_admin_state(base_url: str = "", http: Any = None, fallback_flags: list[str] | None = None,
                      tenant_id: str | None = None) -> dict:
    """``GET /admin/state`` of the default tenant, or of ``tenant_id`` (``http`` may be any object with ``get``
    such as an httpx or Starlette client)."""
    path = f"/admin/tenants/{tenant_id}/state" if tenant_id else "/admin/state"
    headers = admin_headers()
    try:
        if http is not None:
            resp = http.get(path, headers=headers) if headers else http.get(path)
        else:
            resp = httpx.get(base_url.rstrip("/") + path, headers=headers, timeout=HTTP_TIMEOUT_S)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, ValueError):
        return _direct_admin_state(fallback_flags or [], tenant_id)


def provision_tenant(base_url: str, chaos: list[str], http: Any = None) -> dict:
    """``POST /admin/tenants``: a fresh tenant with these chaos flags. Returns its id, entry link and credentials."""
    resp = _admin_request("post", base_url, "/admin/tenants", http, json={"chaos": chaos})
    if resp.status_code == 404:
        raise RuntimeError(f"The sim at {base_url} does not support tenants (POST /admin/tenants gave 404); restart it.")
    resp.raise_for_status()
    return resp.json()


def delete_tenant(base_url: str, tenant_id: str, http: Any = None) -> None:
    """``DELETE /admin/tenants/<id>`` (best effort: cleanup must never mask a scenario's result)."""
    try:
        _admin_request("delete", base_url, f"/admin/tenants/{tenant_id}", http)
    except httpx.HTTPError:
        pass


def _admin_request(method: str, base_url: str, path: str, http: Any = None, **kwargs: Any) -> Any:
    headers = admin_headers()
    if http is not None:
        return getattr(http, method)(path, **({"headers": headers} if headers else {}), **kwargs)
    return getattr(httpx, method)(base_url.rstrip("/") + path, headers=headers, timeout=PROVISION_TIMEOUT_S, **kwargs)


def _differs(field: str, actual: Any, expected: Any) -> bool:
    if field == "amount":
        return abs(float(actual) - float(expected)) > AMOUNT_TOLERANCE
    return actual != expected


def _field_diffs(fields: tuple[str, ...], actual: dict, expected: dict) -> str:
    return "; ".join(f"{f} is {actual.get(f)!r} in the sim's data, the seed formula gives {expected.get(f)!r}"
                     for f in fields if _differs(f, actual.get(f), expected.get(f)))


def seed_differences(invoices: list[dict], inbox: list[dict], formula_invoices: list[dict], formula_inbox: list[dict]) -> list[str]:
    """Differences between the sim's actual rows and what the seed formula says it should hold (empty = they agree)."""
    lines = []
    actual, expected = {i["number"]: i for i in invoices}, {i["number"]: i for i in formula_invoices}
    for number in sorted(expected.keys() | actual.keys()):
        if number not in actual:
            lines.append(f"invoice {number} is missing from the sim's data (the seed formula expects it)")
        elif number not in expected:
            lines.append(f"invoice {number} is in the sim's data but not in the seed formula")
        elif diff := _field_diffs(INVOICE_FIELDS, actual[number], expected[number]):
            lines.append(f"invoice {number}: {diff}")
    actual_mail = {(m["sender"], m["subject"]): m for m in inbox}
    expected_mail = {(m["sender"], m["subject"]): m for m in formula_inbox}
    for key in sorted(expected_mail.keys() | actual_mail.keys()):
        label = f"inbox mail '{key[1]}' from {key[0]}"
        if key not in actual_mail:
            lines.append(f"{label} is missing from the sim's data (the seed formula expects it)")
        elif key not in expected_mail:
            lines.append(f"{label} is in the sim's data but not in the seed formula")
        elif diff := _field_diffs(INBOX_FIELDS, actual_mail[key], expected_mail[key]):
            lines.append(f"{label}: {diff}")
    return lines


def build_sim_state(admin_state: dict, baseline: dict | None = None) -> dict:
    """Combine an admin snapshot with the sim's actual data as ground truth.

    ``baseline`` is the snapshot taken right after the reset, before the agent ran; it lets checkers tell the bills
    and mails the agent created from the ones that were seeded, and it is where the invoices and inbox the agent saw
    are read from (the final snapshot when there is no baseline). ``env_errors`` is empty when the actual rows match the
    seed formula and both snapshots come from the same sim; otherwise it says what differs and the run must not be graded.
    """
    today = date.fromisoformat(admin_state["sim_today"])
    flags, salt = set(admin_state.get("chaos") or []), admin_state.get("seed_salt") or ""
    formula_invoices = expected_invoices(today, flags, salt)
    source = baseline if baseline and baseline.get("invoices") is not None and baseline.get("inbox") is not None else admin_state
    env_errors: list[str] = []
    if baseline:
        for key in ("instance_id", "sim_today"):
            if baseline.get(key) and baseline[key] != admin_state.get(key):
                env_errors.append(f"the snapshot taken before the run and the one after it disagree on {key} "
                                  f"({baseline[key]} vs {admin_state.get(key)}): they come from different simulators")
    if source.get("invoices") is None or source.get("inbox") is None:
        env_errors.append("the sim's state has no invoices/inbox rows, so its data cannot be checked (an older simulator?)")
        invoices, inbox = [], []
    else:
        invoices, inbox = list(source["invoices"]), list(source["inbox"])
        env_errors += seed_differences(invoices, inbox, formula_invoices, expected_inbox(today, formula_invoices))
    return {
        "sim_today": admin_state["sim_today"],
        "instance_id": admin_state.get("instance_id"),
        "chaos": list(admin_state.get("chaos") or []),
        "bills": list(admin_state.get("bills") or []),
        "sent_mail": list(admin_state.get("sent_mail") or []),
        "baseline_bills": list((baseline or admin_state).get("bills") or []),
        "baseline_sent_mail": list((baseline or admin_state).get("sent_mail") or []),
        "invoices": invoices,
        "inbox": inbox,
        "env_errors": env_errors,
    }
