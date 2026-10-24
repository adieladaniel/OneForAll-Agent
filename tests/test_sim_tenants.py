"""Multi-tenant simulated company: isolation of data, sessions and chaos, tenant routing, admin token, eval helpers.

Everything runs in-process (Starlette TestClient / httpx ASGITransport) against a temporary ``SIM_DATA_DIR``.
"""
from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from company_sim import auth, chaos, config, db, seed, state, tenants
from evals import simstate
from evals.checkers import CHECKERS
from evals.simstate import build_sim_state, latest_invoice

BILL = {"vendor": "CX-001", "invoice_number": "T-1", "amount": "100.00", "currency": "EUR",
        "issue_date": "2026-09-28", "due_date": "2026-10-28", "notes": ""}


@pytest.fixture()
def sim(tmp_path, monkeypatch):
    """The simulator on a temp data dir, with its default tenant freshly seeded and no admin token."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "sim.db")
    monkeypatch.setattr(config, "PDF_DIR", tmp_path / "pdfs")
    monkeypatch.setattr(config, "WORKSPACE_DIR", tmp_path / "workspace")
    monkeypatch.delenv("SIM_ADMIN_TOKEN", raising=False)
    from company_sim.app import app

    chaos.set_flags([])
    with TestClient(app) as client:
        client.post("/admin/reset", json={"chaos": []})
        yield client
    chaos.set_flags([])


def new_tenant(sim, **body) -> dict:
    resp = sim.post("/admin/tenants", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def visitor(sim, info: dict | None = None) -> TestClient:
    """A separate browser (own cookie jar); with ``info`` it has opened that tenant's entry link."""
    client = TestClient(sim.app, base_url="http://testserver")
    if info:
        assert client.get(info["entry_path"]).status_code == 200
    return client


def login(client: TestClient, info: dict | None, app_key: str = "erp"):
    creds = info["credentials"][app_key] if info else {
        "erp": {"username": config.ERP_USER, "password": config.ERP_PASSWORD}}[app_key]
    return client.post(f"/{app_key}/login", data=creds)


def api_key(info: dict | None) -> dict:
    return {"X-API-Key": info["credentials"]["erp_api_key"] if info else config.ERP_API_KEY}


def tenant_bills(tenant_id: str) -> list[str]:
    return [b["invoice_number"] for b in tenants.tenant_state(tenant_id)["bills"]]


# --------------------------------------------------------------------------- provisioning


def test_provisioning_returns_entry_link_and_distinct_credentials(sim):
    a, b = new_tenant(sim), new_tenant(sim)
    assert a["tenant_id"] != b["tenant_id"] and a["token"] != b["token"]
    assert a["entry_url"] == f"http://testserver/t/{a['token']}/" and a["entry_path"] == f"/t/{a['token']}/"
    assert set(a["credentials"]) == {"mail", "portal", "erp", "erp_api_key"}
    for app_key in ("mail", "portal", "erp"):
        assert a["credentials"][app_key]["password"] != b["credentials"][app_key]["password"]
        assert a["credentials"][app_key]["password"] != getattr(config, f"{app_key.upper()}_PASSWORD")
    assert a["credentials"]["erp_api_key"] != b["credentials"]["erp_api_key"] != config.ERP_API_KEY
    assert (tenants.tenants_dir() / a["tenant_id"] / "sim.db").exists()
    assert len(list((tenants.tenants_dir() / a["tenant_id"] / "pdfs").glob("*.pdf"))) == 14


def test_provisioning_is_idempotent_and_validates(sim):
    first = new_tenant(sim, tenant_id="acme-1", chaos=["duplicate_bill"])
    again = sim.post("/admin/tenants", json={"tenant_id": "acme-1"})
    assert again.status_code == 200 and again.json()["token"] == first["token"]
    assert again.json()["chaos"] == ["duplicate_bill"]
    for bad in ({"tenant_id": "default"}, {"tenant_id": "../evil"}, {"tenant_id": "Upper"}, {"chaos": ["nope"]}):
        assert sim.post("/admin/tenants", json=bad).status_code == 422, bad


def test_admin_tenant_lifecycle(sim):
    info = new_tenant(sim, tenant_id="life", chaos=["erp_submit_500_once"])
    listing = sim.get("/admin/tenants").json()["tenants"]
    assert [t["tenant_id"] for t in listing] == ["default", "life"] and listing[0]["default"]
    assert "token" not in str(listing) and info["token"] not in str(listing)  # the list never leaks secrets
    assert sim.get("/admin/tenants/life").json()["token"] == info["token"]

    st = sim.get("/admin/tenants/life/state").json()
    assert st["tenant"] == "life" and st["chaos"] == ["erp_submit_500_once"] and len(st["bills"]) == 5
    assert st["sim_today"] == config.SIM_TODAY.isoformat() and st["sent_mail"] == []

    reset = sim.post("/admin/tenants/life/reset", json={"chaos": ["duplicate_bill"]}).json()
    assert reset["status"] == "reset" and reset["chaos"] == ["duplicate_bill"]
    assert len(sim.get("/admin/tenants/life/state").json()["bills"]) == 6  # duplicate_bill seeds one more

    toggled = sim.post("/admin/tenants/life/chaos", json={"duplicate_bill": False, "slow_pages": True}).json()
    assert toggled["active"] == ["slow_pages"] and toggled["reseeded"] is True
    assert sim.get("/admin/tenants/life/chaos").json()["active"] == ["slow_pages"]
    assert chaos.active() == set()  # the default tenant was not touched

    assert sim.delete("/admin/tenants/life").json()["status"] == "deleted"
    assert not (tenants.tenants_dir() / "life").exists()
    assert sim.get("/admin/tenants/life/state").status_code == 404
    assert sim.delete("/admin/tenants/life").status_code == 404
    assert sim.delete("/admin/tenants/default").status_code == 400
    assert sim.get(info["entry_path"]).status_code == 404  # a deleted tenant's link stops working


def test_chaos_flags_survive_a_restart_of_the_sim_process(sim):
    new_tenant(sim, tenant_id="keeper", chaos=["slow_pages", "erp_session_expiry"])
    chaos._states.clear()  # what a fresh process would have in memory
    with tenants.use_tenant("keeper"):
        assert chaos.active() == {"slow_pages", "erp_session_expiry"}


# --------------------------------------------------------------------------- entry route and routing


def test_entry_route_sets_the_cookie_and_redirects(sim):
    info = new_tenant(sim)
    resp = sim.get(info["entry_path"], follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    cookie = resp.headers["set-cookie"].lower()
    assert f"sim_tenant={info['token']}".lower() in cookie
    assert "httponly" in cookie and "path=/" in cookie and "samesite=lax" in cookie
    assert sim.get(info["entry_path"].rstrip("/"), follow_redirects=False).status_code == 303  # without the slash too
    assert sim.get(info["entry_path"] + "?next=/mail/login", follow_redirects=False).headers["location"] == "/mail/login"
    for evil in ("//evil.example", "https://evil.example", "/\\evil"):
        assert sim.get(info["entry_path"], params={"next": evil}, follow_redirects=False).headers["location"] == "/"


def test_bad_token_is_404_and_ids_are_never_accepted(sim):
    info = new_tenant(sim)
    for path in ("/t/not-a-token/", f"/t/{info['tenant_id']}/", "/t/default/", "/t/"):
        resp = sim.get(path, follow_redirects=False)
        assert resp.status_code == 404 and "set-cookie" not in resp.headers, path
    # nor does a cookie naming a tenant id select it: only a real token does
    stranger = visitor(sim)
    stranger.cookies.set("sim_tenant", info["tenant_id"])
    assert login(stranger, info).status_code == 200 and "Invalid username or password" in login(stranger, info).text


def test_stale_cookie_falls_back_to_the_default_tenant(sim):
    stranger = visitor(sim)
    stranger.cookies.set("sim_tenant", "token-of-a-deleted-tenant")
    assert login(stranger, None).url.path == "/erp/bills"  # default tenant's own login still works


def test_cookie_selects_the_tenant_for_every_app(sim):
    a = new_tenant(sim)
    browser = visitor(sim, a)
    assert login(browser, a, "mail").url.path == "/mail/inbox"
    assert login(browser, a, "portal").url.path == "/portal/invoices"
    assert login(browser, a).url.path == "/erp/bills"
    assert "Company X Ltd" in browser.get("/erp/bills").text
    # the same logins mean nothing to the default tenant, whose passwords differ
    assert "Invalid username" in login(visitor(sim), a).text


def test_api_key_routes_the_rest_api_to_its_tenant(sim):
    a, b = new_tenant(sim), new_tenant(sim)
    body = {"vendor": "Globex Corporation", "invoice_number": "API-1", "amount": 10, "currency": "USD",
            "issue_date": "2026-09-01", "due_date": "2026-10-01"}
    anonymous = visitor(sim)  # no cookie: the key alone identifies the tenant
    assert anonymous.post("/erp/api/bills", headers=api_key(a), json=body).status_code == 201
    assert anonymous.post("/erp/api/bills", headers=api_key(None), json={**body, "invoice_number": "API-DEFAULT"}).status_code == 201
    assert tenant_bills(a["tenant_id"])[-1] == "API-1"
    assert "API-1" not in tenant_bills(b["tenant_id"]) and "API-1" not in tenant_bills("default")
    assert tenant_bills("default")[-1] == "API-DEFAULT"
    assert [x["invoice_number"] for x in anonymous.get("/erp/api/bills", headers=api_key(b)).json()] == tenant_bills(b["tenant_id"])
    assert anonymous.get("/erp/api/vendors", headers={"X-API-Key": "ledger-" + secrets.token_urlsafe(24)}).status_code == 401
    assert anonymous.get("/erp/api/vendors").status_code == 401
    # a key never opens another tenant's UI, and the docs show the caller's own key
    assert "Sign in" in anonymous.get("/erp/login", headers=api_key(a)).text
    assert a["credentials"]["erp_api_key"] in visitor(sim, a).get("/erp/api/docs-summary").text


def test_cookie_wins_over_the_api_key(sim):
    a, b = new_tenant(sim), new_tenant(sim)
    browser = visitor(sim, a)
    assert browser.get("/erp/api/vendors", headers=api_key(b)).status_code == 401  # a's cookie, b's key: not a's key
    assert browser.get("/erp/api/vendors", headers=api_key(a)).status_code == 200


# --------------------------------------------------------------------------- isolation


def test_a_bill_created_in_one_tenant_is_invisible_in_another(sim):
    a, b = new_tenant(sim), new_tenant(sim)
    in_a, in_b = visitor(sim, a), visitor(sim, b)
    login(in_a, a), login(in_b, b)
    created = in_a.post("/erp/bills/new", data=BILL)
    assert created.url.path == "/erp/bills/1006" and "Bill #1006 created" in created.text
    assert tenant_bills(a["tenant_id"])[-1] == "T-1"
    assert "T-1" not in tenant_bills(b["tenant_id"]) and "T-1" not in tenant_bills("default")
    assert "T-1" not in in_b.get("/erp/bills").text and "T-1" in in_a.get("/erp/bills").text
    assert in_b.get("/erp/bills/1006").status_code == 404
    # b may use the very same invoice number: its ledger is its own
    assert in_b.post("/erp/bills/new", data=BILL).url.path == "/erp/bills/1006"
    # sent mail is per tenant too
    login(in_a, a, "mail")
    in_a.post("/mail/compose", data={"to": "x@y.example", "subject": "Hi", "body": "b"})
    assert len(tenants.tenant_state(a["tenant_id"])["sent_mail"]) == 1
    assert tenants.tenant_state(b["tenant_id"])["sent_mail"] == []


def test_a_session_from_one_tenant_does_not_work_in_another(sim):
    a, b = new_tenant(sim), new_tenant(sim)
    in_a = visitor(sim, a)
    login(in_a, a)
    assert in_a.get("/erp/bills", follow_redirects=False).status_code == 200
    # carry a's session cookie into a browser that is inside b
    in_b = visitor(sim, b)
    in_b.cookies.set("erp_session", in_a.cookies["erp_session"])
    assert in_b.get("/erp/bills", follow_redirects=False).status_code == 303
    # ... and into the default tenant
    on_default = visitor(sim)
    on_default.cookies.set("erp_session", in_a.cookies["erp_session"])
    assert on_default.get("/erp/bills", follow_redirects=False).status_code == 303
    # sessions are keyed by (tenant, app)
    assert (a["tenant_id"], "erp") in auth.SESSIONS and len(auth.SESSIONS[(a["tenant_id"], "erp")]) == 1
    assert (b["tenant_id"], "erp") not in auth.SESSIONS or not auth.SESSIONS[(b["tenant_id"], "erp")]


def test_chaos_in_one_tenant_does_not_affect_another(sim):
    a = new_tenant(sim, chaos=["erp_submit_500_once"])
    b = new_tenant(sim)
    in_a, in_b = visitor(sim, a), visitor(sim, b)
    login(in_a, a), login(in_b, b)
    assert in_a.post("/erp/bills/new", data=BILL).status_code == 500  # a's one-time failure
    assert in_b.post("/erp/bills/new", data=BILL).status_code == 200  # b never sees it
    assert in_a.post("/erp/bills/new", data=BILL).status_code == 200  # a's retry works, as in v1
    # data-affecting flags are per tenant as well
    sim.post(f"/admin/tenants/{b['tenant_id']}/chaos", json={"chaos": ["ambiguous_vendor"]})
    assert any(v["vendor"] == "Company X Holdings" for v in _invoices(b["tenant_id"]))
    assert not any(v["vendor"] == "Company X Holdings" for v in _invoices(a["tenant_id"]))
    assert chaos.active() == set()
    with tenants.use_tenant(a["tenant_id"]):
        assert chaos.is_on("erp_submit_500_once") and not chaos.is_on("ambiguous_vendor")


def _invoices(tenant_id: str) -> list[dict]:
    with tenants.use_tenant(tenant_id):
        return state.list_invoices()


def test_session_expiry_chaos_counts_views_per_tenant(sim):
    a = new_tenant(sim, chaos=["erp_session_expiry"])
    b = new_tenant(sim, chaos=["erp_session_expiry"])
    in_a, in_b = visitor(sim, a), visitor(sim, b)
    login(in_a, a), login(in_b, b)
    for path in ("/erp/bills/new", "/erp/bills/1001"):  # the login itself was page view 1
        in_a.get(path)
    assert in_a.get("/erp/bills").url.path == "/erp/login"  # a's session expires after its 3rd page view
    assert in_b.get("/erp/bills").url.path == "/erp/bills"  # b has only looked once


def test_resetting_one_tenant_keeps_the_others_logged_in(sim):
    a, b = new_tenant(sim), new_tenant(sim)
    in_a, in_b, on_default = visitor(sim, a), visitor(sim, b), visitor(sim)
    login(in_a, a), login(in_b, b), login(on_default, None)
    in_b.post("/erp/bills/new", data=BILL)
    assert sim.post(f"/admin/tenants/{a['tenant_id']}/reset").status_code == 200
    assert in_a.get("/erp/bills").url.path == "/erp/login"  # a was logged out
    assert in_b.get("/erp/bills", follow_redirects=False).status_code == 200  # b was not
    assert on_default.get("/erp/bills", follow_redirects=False).status_code == 200
    assert "T-1" in tenant_bills(b["tenant_id"])  # and b's data is intact
    sim.post("/admin/reset", json={"chaos": []})  # the legacy reset only logs out the default tenant
    assert on_default.get("/erp/bills").url.path == "/erp/login"
    assert in_b.get("/erp/bills", follow_redirects=False).status_code == 200


def test_tenants_hold_different_amounts_but_the_same_vendors_and_numbers(sim):
    a, b = new_tenant(sim, tenant_id="salt-a"), new_tenant(sim, tenant_id="salt-b")
    inv = {t: {i["number"]: i for i in _invoices(t)} for t in ("default", a["tenant_id"], b["tenant_id"])}
    assert set(inv["default"]) == set(inv[a["tenant_id"]]) == set(inv[b["tenant_id"]])
    assert inv["default"]["GLX-88213"]["amount"] == pytest.approx(5551.20)  # the v1 figure
    amounts = {t: [rows[n]["amount"] for n in sorted(rows)] for t, rows in inv.items()}
    assert amounts["default"] != amounts["salt-a"] != amounts["salt-b"]
    for t, rows in inv.items():
        for n, row in rows.items():
            assert (row["vendor"], row["issue_date"], row["due_date"]) == (
                inv["default"][n]["vendor"], inv["default"][n]["issue_date"], inv["default"][n]["due_date"]), (t, n)
    # a tenant's dataset is a function of its id: provisioning it again later gives the same amounts
    sim.delete("/admin/tenants/salt-a")
    new_tenant(sim, tenant_id="salt-a")
    assert [r["amount"] for _, r in sorted({i["number"]: i for i in _invoices("salt-a")}.items())] == amounts["salt-a"]


# --------------------------------------------------------------------------- admin token


def test_admin_token_is_enforced_when_set(sim, monkeypatch):
    monkeypatch.setenv("SIM_ADMIN_TOKEN", "s3cret-admin")
    guarded = [("get", "/admin/chaos"), ("post", "/admin/chaos"), ("post", "/admin/reset"), ("get", "/admin/state"),
               ("get", "/admin/tenants"), ("post", "/admin/tenants"), ("get", "/admin/tenants/x"),
               ("delete", "/admin/tenants/x"), ("post", "/admin/tenants/x/reset"), ("get", "/admin/tenants/x/state"),
               ("get", "/admin/tenants/x/chaos"), ("post", "/admin/tenants/x/chaos")]
    for method, path in guarded:
        assert getattr(sim, method)(path).status_code == 401, path
        assert getattr(sim, method)(path, headers={"X-Admin-Token": "wrong"}).status_code == 401, path
    ok = {"X-Admin-Token": "s3cret-admin"}
    created = sim.post("/admin/tenants", headers=ok, json={"tenant_id": "guarded"})
    assert created.status_code == 201
    assert sim.get("/admin/tenants/guarded/state", headers=ok).status_code == 200
    assert sim.get("/admin/state", headers=ok).status_code == 200
    # the apps themselves, the entry link and the health check are not admin endpoints
    assert sim.get("/healthz").status_code == 200
    assert sim.get(created.json()["entry_path"]).status_code == 200
    assert sim.get("/erp/login").status_code == 200


def test_admin_is_open_when_no_token_is_configured(sim):
    assert sim.get("/admin/tenants").status_code == 200 and sim.get("/admin/state").status_code == 200


def test_admin_ignores_the_tenant_cookie(sim):
    a = new_tenant(sim)
    browser = visitor(sim, a)
    login(browser, a)
    browser.post("/erp/bills/new", data=BILL)
    assert [b["invoice_number"] for b in browser.get("/admin/state").json()["bills"]][-1] != "T-1"  # default tenant


# --------------------------------------------------------------------------- default tenant unchanged


def test_default_tenant_keeps_the_v1_data_credentials_and_paths(sim, tmp_path):
    assert tenants.db_path("default") == config.DB_PATH == tmp_path / "sim.db"
    assert tenants.pdf_dir("default") == tmp_path / "pdfs"
    creds = tenants.default_tenant().credentials
    assert creds["erp"] == {"username": config.ERP_USER, "password": config.ERP_PASSWORD}
    assert creds["erp_api_key"] == config.ERP_API_KEY == "ledger-api-key-001"
    assert [(b["id"], b["status"]) for b in state.get_bills()] == [(1001, "approved"), (1002, "approved"),
                                                                    (1003, "approved"), (1004, "approved"), (1005, "approved")]
    assert len(state.list_invoices()) == 14
    assert state.get_invoice("GLX-88213")["amount"] == pytest.approx(5551.20)
    # no salt = the exact v1 seed
    today = config.SIM_TODAY
    assert seed.invoice_specs(today, set(), "") == seed.invoice_specs(today, set())
    # the default tenant works with no cookie, no key, exactly like before
    anonymous = visitor(sim)
    assert login(anonymous, None).url.path == "/erp/bills"
    assert anonymous.get("/erp/api/vendors", headers=api_key(None)).status_code == 200
    assert sim.get("/admin/state").json()["bills"][0]["id"] == 1001
    assert not (tmp_path / "tenants").exists()  # a sim that only serves the default tenant writes no tenant files


def test_workspace_credentials_file_is_written_for_the_default_tenant_only(sim, tmp_path):
    new_tenant(sim, tenant_id="shared-check")
    workspace = config.WORKSPACE_DIR
    written = (workspace / "credentials.json").read_text(encoding="utf-8")
    assert config.ERP_API_KEY in written and config.ERP_PASSWORD in written
    info = sim.get("/admin/tenants/shared-check").json()
    assert info["credentials"]["erp_api_key"] not in written
    assert all(info["credentials"][a]["password"] not in written for a in ("mail", "portal", "erp"))
    assert sorted(p.name for p in workspace.iterdir()) == ["README.txt", "credentials.json"]


# --------------------------------------------------------------------------- python helpers


def test_use_tenant_switches_every_helper(sim):
    info = new_tenant(sim, tenant_id="helper")
    assert tenants.current_tenant.get() == "default"
    with tenants.use_tenant("helper") as tenant:
        assert tenant.token == info["token"] and tenants.current_tenant.get() == "helper"
        assert db.query_one("SELECT COUNT(*) AS n FROM bills")["n"] == 5
        db.execute("DELETE FROM bills")
        assert tenants.db_path() == tenants.tenants_dir() / "helper" / "sim.db"
    assert tenants.current_tenant.get() == "default"
    assert len(state.get_bills()) == 5 and tenants.tenant_state("helper")["bills"] == []
    with pytest.raises(tenants.UnknownTenant):
        with tenants.use_tenant("ghost"):
            pass


def test_provision_tenant_helper_and_lookup(sim):
    tenant = tenants.provision_tenant(chaos=["portal_missing_invoice"])
    assert tenants.get_tenant(tenant.id) == tenant and tenants.find_by_token(tenant.token) == tenant
    assert tenants.find_by_api_key(tenant.api_key) == tenant
    assert tenants.find_by_api_key(config.ERP_API_KEY).id == "default"
    assert tenants.get_tenant("nope") is None and tenants.find_by_token("") is None
    assert [t.id for t in tenants.list_tenants()] == [tenant.id]
    hidden = [i for i in _invoices(tenant.id) if not i["portal_visible"]]
    assert [i["vendor"] for i in hidden] == ["Company X Ltd"]
    assert tenants.delete_tenant(tenant.id) and not tenants.delete_tenant(tenant.id)
    assert tenants.find_by_token(tenant.token) is None


# --------------------------------------------------------------------------- evals against a tenant


def post_bill(client: TestClient, info: dict, inv: dict, **over) -> None:
    body = {"vendor": inv["vendor"], "invoice_number": inv["number"], "amount": inv["amount"], "currency": inv["currency"],
            "issue_date": inv["issue_date"], "due_date": inv["due_date"], **over}
    assert client.post("/erp/api/bills", headers=api_key(info), json=body).status_code == 201


def run_state(status="succeeded", summary="Done.") -> dict:
    return {"status": status, "steps": [{"kind": "tool", "tool": "http_request", "args": {}, "ok": True}],
            "final_report": {"status": status, "summary": summary, "outputs": {}, "stats": {}}}


def test_eval_checkers_judge_a_provisioned_tenant(sim):
    info = simstate.provision_tenant("http://testserver", ["duplicate_bill"], http=sim)
    tid = info["tenant_id"]
    baseline = simstate.fetch_admin_state(http=sim, tenant_id=tid)
    assert baseline["tenant"] == tid and baseline["chaos"] == ["duplicate_bill"] and baseline["seed_salt"] == tid

    snap = build_sim_state(baseline, baseline)
    with tenants.use_tenant(tid):  # the invoices derived from the seed (with this tenant's salt) match its database
        rows = {r["number"]: r for r in state.list_invoices()}
    assert set(rows) == {i["number"] for i in snap["invoices"]}
    assert all(rows[i["number"]]["amount"] == pytest.approx(i["amount"]) for i in snap["invoices"])
    default_amounts = {i["number"]: i["amount"] for i in build_sim_state(sim.get("/admin/state").json())["invoices"]}
    assert any(default_amounts[n] != rows[n]["amount"] for n in rows)  # the salt made a difference

    inv = latest_invoice(snap["invoices"], "Company X Ltd")
    honest = run_state("needs_attention", "This invoice is already recorded as bill #1006, so I did not enter it again.")
    assert CHECKERS["invoice_duplicate"](honest, snap).passed
    # a duplicate under another vendor in this tenant is caught by the checker, and only this tenant is affected
    post_bill(visitor(sim), info, inv, vendor="Company X Holdings")
    after = build_sim_state(simstate.fetch_admin_state(http=sim, tenant_id=tid), baseline)
    assert not CHECKERS["invoice_duplicate"](honest, after).passed
    assert len(sim.get("/admin/state").json()["bills"]) == 5  # the default tenant never saw it

    entry = simstate.provision_tenant("http://testserver", [], http=sim)
    base2 = simstate.fetch_admin_state(http=sim, tenant_id=entry["tenant_id"])
    inv2 = latest_invoice(build_sim_state(base2)["invoices"], "Company X Ltd")
    assert not CHECKERS["invoice_entry"](run_state(), build_sim_state(base2, base2)).passed
    post_bill(visitor(sim), entry, inv2)
    done = build_sim_state(simstate.fetch_admin_state(http=sim, tenant_id=entry["tenant_id"]), base2)
    assert CHECKERS["invoice_entry"](run_state(), done).passed

    simstate.delete_tenant("http://testserver", tid, http=sim)
    assert sim.get(f"/admin/tenants/{tid}/state").status_code == 404


def test_eval_helpers_send_the_admin_token(sim, monkeypatch):
    monkeypatch.setenv("SIM_ADMIN_TOKEN", "tok")
    assert simstate.admin_headers() == {"X-Admin-Token": "tok"}
    info = simstate.provision_tenant("http://testserver", [], http=sim)
    assert simstate.fetch_admin_state(http=sim, tenant_id=info["tenant_id"])["tenant"] == info["tenant_id"]
    monkeypatch.delenv("SIM_ADMIN_TOKEN")
    assert simstate.admin_headers() == {}


# --------------------------------------------------------------------------- concurrency


async def test_interleaved_tenants_do_not_cross_talk(sim):
    """Three tenants (two browsers in the first) post forms at the same time through one in-process ASGI app."""
    infos = [new_tenant(sim) for _ in range(3)]
    app = sim.app
    posts_per_browser = 6

    async def browser(info: dict, label: str) -> None:
        strangers = [x for x in ("A1", "A2", "B", "C") if x[0] != label[0]]  # A1 and A2 share a tenant, the rest do not
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sim",
                                     follow_redirects=True) as client:
            assert (await client.get(info["entry_path"])).status_code == 200
            assert (await client.post("/erp/login", data=info["credentials"]["erp"])).url.path == "/erp/bills"
            for n in range(posts_per_browser):
                bill = {**BILL, "invoice_number": f"{label}-{n}", "amount": f"{10 + n}.00"}
                resp = await client.post("/erp/bills/new", data=bill)
                assert resp.status_code == 200 and f"{label}-{n}" in resp.text, resp.text[:300]
                listing = (await client.get("/erp/bills")).text
                assert f"{label}-{n}" in listing
                for other in strangers:
                    assert f"<td>{other}-" not in listing, f"{label} saw {other}'s bill"
                await asyncio.sleep(0)

    await asyncio.gather(browser(infos[0], "A1"), browser(infos[0], "A2"), browser(infos[1], "B"), browser(infos[2], "C"))

    names = [{n for n in tenant_bills(i["tenant_id"]) if n.split("-")[0] in ("A1", "A2", "B", "C")} for i in infos]
    assert names[0] == {f"{lab}-{n}" for lab in ("A1", "A2") for n in range(posts_per_browser)}
    assert names[1] == {f"B-{n}" for n in range(posts_per_browser)}
    assert names[2] == {f"C-{n}" for n in range(posts_per_browser)}
    assert len(state.get_bills()) == 5  # the default tenant stayed untouched


# --------------------------------------------------------------------------- evals: a tenant per scenario, in parallel


@pytest.fixture()
def tenant_harness(sim, tmp_path, monkeypatch):
    """evals.run wired to the in-process sim, a scripted LLM and a browser-less registry; records what each run saw."""
    from evals import run as eval_run
    from worker.config import Settings
    from worker.llm.base import LLMResponse, ToolCall, Usage
    from worker.llm.fake import ScriptedLLM
    from worker.tools.control import FinishTool, UpdatePlanTool
    from worker.tools.registry import ToolRegistry

    seen: list[dict] = []
    monkeypatch.setattr(eval_run, "provision_tenant", lambda base_url, chaos: simstate.provision_tenant(base_url, chaos, http=sim))
    monkeypatch.setattr(eval_run, "delete_tenant", lambda base_url, tid: simstate.delete_tenant(base_url, tid, http=sim))
    real_fetch = simstate.fetch_admin_state
    monkeypatch.setattr(eval_run, "fetch_admin_state",
                        lambda base_url="", http=None, fallback_flags=None, tenant_id=None: real_fetch(http=sim, tenant_id=tenant_id))
    monkeypatch.setattr(eval_run, "reset_sim", lambda base_url, flags: sim.post("/admin/reset", json={"chaos": flags}))

    def registry(settings):
        creds_file = Path(settings.WORKSPACE_DIR) / "credentials.json"
        seen.append({"entry": settings.SIM_ENTRY_URL, "workspace": settings.WORKSPACE_DIR,
                     "credentials": json.loads(creds_file.read_text(encoding="utf-8")) if creds_file.exists() else None})
        reg = ToolRegistry()
        reg.register(UpdatePlanTool())
        reg.register(FinishTool())
        return reg

    def llm(run_settings):
        # The scenarios run concurrently, so record the prompt on this run's own entry (matched by its entry link), not
        # on whichever run happened to build its registry last.
        entry = run_settings.SIM_ENTRY_URL

        def responder(messages, tools):
            for record in seen:
                if record["entry"] == entry:
                    record["prompt"] = messages[1].content
            call = ToolCall(id="c1", name="finish", args={"status": "blocked", "summary": "I was not allowed to create the bill."})
            return LLMResponse(text="", tool_calls=[call], usage=Usage(10, 5))
        return ScriptedLLM(responder)

    monkeypatch.setattr(eval_run, "build_registry", registry)
    monkeypatch.setattr(eval_run, "make_llm", llm)
    settings = Settings(LLM_PROVIDER="fake", RUNS_DIR=str(tmp_path / "runs"), WORKSPACE_DIR=str(tmp_path / "shared-ws"),
                        LLM_MIN_INTERVAL_S=0.0)
    return eval_run, settings, seen


async def test_each_scenario_gets_its_own_tenant_workspace_and_entry_link(tenant_harness, sim):
    eval_run, settings, seen = tenant_harness
    scenario = next(s for s in eval_run.load_scenarios() if s.id == "deny_write")
    results = await asyncio.gather(*(eval_run.run_scenario(scenario, settings, "http://testserver", repeat=n, per_tenant=True)
                                     for n in (1, 2, 3)))
    assert all(r["passed"] and r["outcome"] == "pass" for r in results)
    assert len({r["tenant"] for r in results}) == 3 and "default" not in {r["tenant"] for r in results}
    assert len({s["workspace"] for s in seen}) == 3 and settings.WORKSPACE_DIR not in {s["workspace"] for s in seen}
    for s in seen:
        assert "/t/" in s["entry"] and s["entry"] in s["prompt"] and "open this link first" in s["prompt"]
        assert s["entry"].startswith("http://testserver/t/")
        creds = s["credentials"]
        assert creds["erp"]["api_key"].startswith("ledger-") and creds["erp"]["api_key"] != config.ERP_API_KEY
        assert creds["mail"]["url"] == "http://testserver/mail"
    assert len({s["credentials"]["erp"]["api_key"] for s in seen}) == 3
    # tenants and their workspaces are cleaned up afterwards
    assert [t["tenant_id"] for t in sim.get("/admin/tenants").json()["tenants"]] == ["default"]
    assert not any(Path(s["workspace"]).exists() for s in seen)


async def test_default_mode_still_uses_the_default_tenant_and_the_shared_workspace(tenant_harness, sim):
    eval_run, settings, seen = tenant_harness
    scenario = next(s for s in eval_run.load_scenarios() if s.id == "deny_write")
    result = await eval_run.run_scenario(scenario, settings, "http://testserver")
    assert result["passed"] and result["tenant"] == "default"
    assert seen[0]["entry"] == "" and seen[0]["workspace"] == settings.WORKSPACE_DIR
    assert "Company intranet start page: http://localhost:8001" in seen[0]["prompt"]


async def test_cli_flags_for_parallel_runs(tenant_harness):
    eval_run, _settings, _seen = tenant_harness
    assert eval_run.parse_args([]).parallel == 1 and eval_run.parse_args([]).tenant_per_scenario is None
    args = eval_run.parse_args(["--parallel", "3", "--tenant-per-scenario"])
    assert args.parallel == 3 and args.tenant_per_scenario is True
    assert eval_run.parse_args(["--no-tenant-per-scenario"]).tenant_per_scenario is False
    # concurrent scenarios cannot share the default tenant
    assert await eval_run.amain(eval_run.parse_args(["--parallel", "2", "--no-tenant-per-scenario", "--only", "deny_write"])) == 2
