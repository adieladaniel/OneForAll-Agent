"""Multi-user authentication: sign-in, sessions, throttling, roles, approval rights and separation of duties, quotas, the
admin API and the audit trail. Database mode against ``oneforall_test``; the app is driven over ``httpx.ASGITransport``
(see ``tests/dbapp.py``). The v1 file mode is covered by the existing server tests."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

from dbapp import (
    ENTRY_TOKENS,
    ERP_PASSWORDS,
    MULTI_EMAIL,
    PASSWORD,
    audit_actions,
    call,
    finished,
    new_client,
    pending_request,
    reply,
    runner_of,
    sign_in,
    start_run,
    wire_script,
    write_script,
)
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from worker import auth as auth_module
from worker.config import Settings
from worker.db.models import AuditLog, AuthSession, Org, User
from worker.server import create_app

LOGIN = "/api/auth/login"
ROLES = ["viewer", "approver", "operator", "admin", "owner"]
RANK = {role: i for i, role in enumerate(ROLES)}


class Clock:
    """A controllable clock for ``AuthService.clock``."""

    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


async def sessions_of(world) -> list[AuthSession]:
    async with world.sm() as session:
        return list((await session.scalars(select(AuthSession))).all())


# --------------------------------------------------------------------------- sign-in


async def test_login_opens_a_server_side_session_stored_only_as_a_hash(world, db_app):
    app, _ = db_app()
    client = new_client(app)
    resp = await client.post(LOGIN, json={"email": "Operator@ACME.test", "password": PASSWORD})  # e-mail is case-insensitive
    assert resp.status_code == 200
    me = resp.json()
    assert me["mode"] == "db" and me["user"]["email"] == "operator@acme.test" and me["user"]["display_name"] == "Omar Operator"
    assert (me["org"]["slug"], me["org"]["role"]) == ("acme", "operator") and [o["slug"] for o in me["orgs"]] == ["acme"]
    assert me["capabilities"] == {"start_runs": True, "approve": False, "answer": True, "admin": False, "credentials": False}
    assert me["org"]["policy"]["separation_of_duties"] is True and me["org"]["environment_ready"] is True

    cookie = resp.headers["set-cookie"]
    assert cookie.startswith("ofa_session=") and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert "secure" not in cookie.lower()
    token = client.cookies["ofa_session"]
    assert len(token) >= 40
    [row] = await sessions_of(world)
    assert row.id == hashlib.sha256(token.encode()).hexdigest() and row.id != token  # only the hash is stored
    assert row.user_id == world.user_ids["operator@acme.test"] and row.org_id == world.org_ids["acme"]
    assert row.ip == "203.0.113.7" and row.revoked_at is None
    assert timedelta(hours=11, minutes=59) < row.expires_at - row.created_at <= timedelta(hours=12)
    assert timedelta(days=6, hours=23) < row.absolute_expires_at - row.created_at <= timedelta(days=7)
    async with world.sm() as session:  # the token itself is nowhere in the database
        dump = repr((await session.execute(text("select * from auth_sessions"))).all())
    assert token not in dump
    assert (await client.get("/api/auth/me")).json()["user"]["id"] == me["user"]["id"]


async def test_session_cookie_is_secure_in_deploy_mode(world, db_app):
    app, _ = db_app(DEPLOY_MODE=True)
    resp = await new_client(app).post(LOGIN, json={"email": "viewer@acme.test", "password": PASSWORD})
    assert resp.status_code == 200 and "secure" in resp.headers["set-cookie"].lower()


async def test_unknown_email_and_wrong_password_look_identical_and_both_pay_for_the_password_check(world, db_app, monkeypatch):
    app, _ = db_app()
    hashes: list[str] = []
    real = auth_module.verify_password

    def spy(password_hash: str, password: str) -> bool:
        hashes.append(password_hash)
        return real(password_hash, password)

    monkeypatch.setattr(auth_module, "verify_password", spy)
    client = new_client(app)
    wrong = await client.post(LOGIN, json={"email": "operator@acme.test", "password": "nope"})
    ghost = await client.post(LOGIN, json={"email": "nobody@acme.test", "password": "nope"})
    async with world.sm() as session, session.begin():
        await session.execute(User.__table__.update().where(User.email == "viewer@acme.test").values(is_active=False))
    inactive = await client.post(LOGIN, json={"email": "viewer@acme.test", "password": PASSWORD})  # right password
    for resp in (wrong, ghost, inactive):
        assert (resp.status_code, resp.json()) == (401, {"detail": "Invalid email or password."})
        assert "set-cookie" not in resp.headers
    assert len(hashes) == 3 and hashes[1].startswith("$argon2")  # the unknown e-mail was verified against a dummy hash
    assert hashes[0] != hashes[1] and await sessions_of(world) == []
    async with world.sm() as session:
        rows = (await session.scalars(select(AuditLog).where(AuditLog.action == "auth.login_failed").order_by(AuditLog.id))).all()
    assert [r.details["reason"] for r in rows] == ["bad_password", "unknown_user", "inactive"]
    assert "nobody" not in repr([r.details for r in rows])  # the attempted address is not recorded


async def test_failed_logins_are_throttled_per_email_and_per_ip_with_growing_backoff(world, db_app):
    app, _ = db_app(LOGIN_MAX_FAILURES_PER_EMAIL=3, LOGIN_MAX_FAILURES_PER_IP=6, LOGIN_BACKOFF_BASE_S=0.5)
    delays: list[float] = []

    async def record(seconds: float) -> None:
        delays.append(seconds)

    service = app.state.auth
    service.sleep = record
    clock = service.clock = Clock()
    bad = {"email": "operator@acme.test", "password": "wrong"}
    client = new_client(app, "198.51.100.1")
    for _ in range(3):
        assert (await client.post(LOGIN, json=bad)).status_code == 401
    assert delays == [0.5, 1.0, 2.0]  # each failure waits twice as long

    blocked = await client.post(LOGIN, json={**bad, "password": PASSWORD})  # even the right password is refused now
    assert blocked.status_code == 429 and 0 < int(blocked.headers["retry-after"]) <= 900
    assert "try again in" in blocked.json()["detail"].lower() and len(delays) == 3

    ghost = {"email": "ghost@acme.test", "password": "wrong"}  # unknown addresses are counted exactly like known ones
    assert [(await client.post(LOGIN, json=ghost)).status_code for _ in range(4)] == [401, 401, 401, 429]
    fresh = {"email": "viewer@acme.test", "password": PASSWORD}
    assert (await client.post(LOGIN, json=fresh)).status_code == 429  # 6 failures from this address: the IP is blocked
    other = new_client(app, "198.51.100.2")
    assert (await other.post(LOGIN, json=fresh)).status_code == 200  # another address is fine
    assert (await other.post(LOGIN, json={**bad, "password": PASSWORD})).status_code == 429  # ... but the e-mail stays blocked

    clock.advance(minutes=16)  # the next window
    assert (await client.post(LOGIN, json={**bad, "password": PASSWORD})).status_code == 200


async def test_a_successful_login_resets_the_email_counter(world, db_app):
    app, _ = db_app(LOGIN_MAX_FAILURES_PER_EMAIL=3)
    client = new_client(app)
    bad = {"email": "operator@acme.test", "password": "wrong"}
    good = {**bad, "password": PASSWORD}
    assert [(await client.post(LOGIN, json=bad)).status_code for _ in range(2)] == [401, 401]
    assert (await client.post(LOGIN, json=good)).status_code == 200
    assert [(await client.post(LOGIN, json=bad)).status_code for _ in range(2)] == [401, 401]  # not blocked
    assert (await client.post(LOGIN, json=good)).status_code == 200


async def test_logout_revokes_the_session_on_the_server(world, db_app):
    app, _ = db_app()
    client = await sign_in(app, "operator@acme.test")
    stolen = client.cookies["ofa_session"]
    assert (await client.get("/api/runs")).status_code == 200

    out = await client.post("/api/auth/logout")
    assert out.status_code == 200 and "ofa_session" in out.headers["set-cookie"] and "max-age=0" in out.headers["set-cookie"].lower()
    replay = new_client(app)
    replay.cookies.set("ofa_session", stolen)  # a copied cookie is worthless after logout
    assert (await replay.get("/api/runs")).status_code == 401
    [row] = await sessions_of(world)
    assert row.revoked_at is not None
    assert (await client.post("/api/auth/logout")).status_code == 200  # logging out twice is harmless
    assert await audit_actions(world.sm, world.org_ids["acme"]) == ["auth.login", "auth.logout"]


async def test_sessions_slide_while_used_but_end_at_the_absolute_limit(world, db_app):
    app, _ = db_app(SESSION_IDLE_TTL_HOURS=12, SESSION_ABSOLUTE_TTL_DAYS=7)
    clock = app.state.auth.clock = Clock()
    client = await sign_in(app, "viewer@acme.test")
    clock.advance(hours=11)
    assert (await client.get("/api/runs")).status_code == 200
    clock.advance(hours=11)  # 22 h after sign-in: alive only because the first use slid the idle timeout
    assert (await client.get("/api/runs")).status_code == 200
    clock.advance(hours=13)  # idle for longer than 12 h
    assert (await client.get("/api/runs")).status_code == 401

    client = await sign_in(app, "viewer@acme.test")  # busy user: used every 11 h, still cut off after 7 days
    for _ in range(15):  # 165 h
        clock.advance(hours=11)
        assert (await client.get("/api/runs")).status_code == 200
    clock.advance(hours=11)  # 176 h > 168 h
    assert (await client.get("/api/runs")).status_code == 401


async def test_revoked_deactivated_and_orphaned_sessions_stop_working(world, db_app):
    app, _ = db_app()
    client = await sign_in(app, "viewer@acme.test")
    async with world.sm() as session, session.begin():  # the user is switched off globally
        await session.execute(User.__table__.update().where(User.email == "viewer@acme.test").values(is_active=False))
    assert (await client.get("/api/runs")).status_code == 401
    bad_cookie = new_client(app)
    bad_cookie.cookies.set("ofa_session", "x" * 300)
    assert (await bad_cookie.get("/api/runs")).status_code == 401


async def test_switch_org_needs_membership_and_changes_what_you_see(world, db_app):
    app, _ = db_app()
    client = await sign_in(app, MULTI_EMAIL)
    me = (await client.get("/api/auth/me")).json()
    assert (me["org"]["slug"], me["org"]["role"]) == ("acme", "operator")
    assert sorted((o["slug"], o["role"]) for o in me["orgs"]) == [("acme", "operator"), ("globex", "approver")]
    acme_run = await start_run(client)
    await finished(client, acme_run)

    switched = await client.post("/api/auth/switch-org", json={"org_id": str(world.org_ids["globex"])})
    assert switched.status_code == 200 and (switched.json()["org"]["slug"], switched.json()["org"]["role"]) == ("globex", "approver")
    assert (await client.get("/api/runs")).json() == []  # globex has no runs
    assert (await client.get(f"/api/runs/{acme_run}")).status_code == 404  # the acme run is not visible from globex
    assert (await client.post("/api/runs", json={"task": "x"})).status_code == 403  # approver in globex
    assert (await client.get("/api/auth/me")).json()["org"]["slug"] == "globex"  # remembered by the session

    async with world.sm() as session, session.begin():
        stranger = Org(slug="initech", name="Initech")
        session.add(stranger)
    denied = await client.post("/api/auth/switch-org", json={"org_id": str(stranger.id)})
    assert denied.status_code == 403 and "not a member" in denied.json()["detail"]
    assert (await client.post("/api/auth/switch-org", json={"org_id": "nope"})).status_code == 422
    assert (await client.get("/api/auth/me")).json()["org"]["slug"] == "globex"  # a refused switch changes nothing

    back = await client.post("/api/auth/switch-org", json={"org_id": str(world.org_ids["acme"])})
    assert back.json()["org"]["role"] == "operator" and [r["id"] for r in (await client.get("/api/runs")).json()] == [acme_run]
    assert (await audit_actions(world.sm)).count("auth.switch_org") == 2


async def test_cross_site_state_changing_requests_are_refused(world, db_app):
    app, _ = db_app()
    client = await sign_in(app, "operator@acme.test")
    cross = {"Sec-Fetch-Site": "cross-site"}
    assert (await client.post("/api/runs", json={"task": "x"}, headers=cross)).status_code == 403
    assert (await client.post(LOGIN, json={"email": "operator@acme.test", "password": PASSWORD}, headers=cross)).status_code == 403
    assert (await client.get("/api/runs", headers=cross)).status_code == 200  # reading is not state-changing
    same_origin = await client.post("/api/runs", json={"task": "x"}, headers={"Sec-Fetch-Site": "same-origin"})
    assert same_origin.status_code == 200
    await finished(client, same_origin.json()["run_id"])


async def test_public_surface_is_minimal_and_everything_else_needs_a_session(world, db_app):
    app, _ = db_app()
    anon = new_client(app)
    assert (await anon.get("/api/health")).json() == {"status": "ok"}
    assert (await anon.get("/")).status_code == 200 and (await anon.get("/ui/app.js")).status_code == 200  # the sign-in page
    for method, path in [("GET", "/api/runs"), ("GET", "/api/runs/abc"), ("POST", "/api/runs"), ("GET", "/api/config"),
                         ("GET", "/api/scenarios"), ("GET", "/api/auth/me"), ("GET", "/api/sim/chaos"),
                         ("POST", "/api/sim/reset"), ("GET", "/runs/abc/files/report.md"), ("GET", "/api/admin/members"),
                         ("GET", "/api/admin/credentials"), ("POST", "/api/runs/abc/respond"), ("POST", "/api/auth/switch-org")]:
        resp = await anon.request(method, path, json={} if method == "POST" else None)
        assert resp.status_code == 401, (method, path, resp.status_code)
    signed_in = await sign_in(app, "viewer@acme.test")
    full = (await signed_in.get("/api/health")).json()
    assert full["status"] == "ok" and full["provider"] == "fake" and full["auth_mode"] == "db"


def test_file_mode_is_unchanged_and_reports_itself(tmp_path):
    settings = Settings(_env_file=None, AUTH_MODE="file", DATABASE_URL="postgresql://u:p@localhost/x", LLM_PROVIDER="fake",
                        RUNS_DIR=str(tmp_path / "runs"), WORKSPACE_DIR=str(tmp_path), SIM_BASE_URL="http://127.0.0.1:9")
    assert not settings.db_auth  # AUTH_MODE=file wins over DATABASE_URL
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/auth/me").json() == {"mode": "file"}
        assert client.get("/api/runs").json() == [] and client.get("/api/admin/members").status_code == 404
        assert client.get("/api/config").json()["auth_enabled"] is False
    auto = dict(_env_file=None, AUTH_MODE="auto")  # (the test suite's environment pins AUTH_MODE=file, see conftest)
    assert Settings(**auto, DATABASE_URL="postgresql://u:p@localhost/x").db_auth  # auto: DATABASE_URL implies db mode
    assert not Settings(**auto, DATABASE_URL="").db_auth
    assert Settings(_env_file=None, AUTH_MODE="db").db_auth


# --------------------------------------------------------------------------- roles


def allowed(role: str, minimum: str) -> bool:
    return RANK[role] >= RANK[minimum]


async def test_role_matrix_for_every_endpoint(world, db_app):
    app, _ = db_app()
    uid = "00000000-0000-0000-0000-000000000001"
    endpoints = [  # (method, path, json body, minimum role)
        ("GET", "/api/auth/me", None, "viewer"), ("GET", "/api/runs", None, "viewer"), ("GET", "/api/runs/abc", None, "viewer"),
        ("GET", "/api/config", None, "viewer"), ("GET", "/api/scenarios", None, "viewer"), ("GET", "/api/health", None, "viewer"),
        ("GET", "/api/sim/chaos", None, "viewer"), ("GET", "/runs/abc/files/report.md", None, "viewer"),
        ("POST", "/api/runs/abc/cancel", None, "operator"), ("GET", "/api/environment/enter", None, "operator"),
        ("POST", "/api/sim/reset", {"chaos": []}, "admin"),
        ("GET", "/api/admin/members", None, "admin"), ("POST", "/api/admin/members", {"email": "bad", "role": "viewer"}, "admin"),
        ("PATCH", f"/api/admin/members/{uid}", {"role": "viewer"}, "admin"), ("GET", "/api/admin/settings", None, "admin"),
        ("PUT", "/api/admin/settings", {"max_steps": 0}, "admin"), ("GET", "/api/admin/credentials", None, "admin"),
        ("PUT", "/api/admin/credentials/erp9", {"kind": "login", "fields": {}}, "admin"),
        ("DELETE", "/api/admin/credentials/none", None, "admin"), ("GET", "/api/admin/environment", None, "admin"),
        ("POST", "/api/admin/environment/provision", None, "admin"),
        ("POST", "/api/admin/environment/reset", {"chaos": []}, "admin"), ("GET", "/api/admin/audit", None, "admin"),
    ]
    for role in ROLES:
        local = {"owner": "owner", "admin": "admin", "operator": "operator", "approver": "approver", "viewer": "viewer"}[role]
        client = await sign_in(app, f"{local}@acme.test")
        for method, path, body, minimum in endpoints:
            resp = await client.request(method, path, json=body)
            if allowed(role, minimum):
                assert resp.status_code not in (401, 403), (role, method, path, resp.status_code, resp.text[:120])
            else:
                assert resp.status_code == 403, (role, method, path, resp.status_code)


async def test_only_operators_and_above_can_start_runs(world, db_app):
    app, _ = db_app()
    for role in ROLES:
        client = await sign_in(app, f"{role}@acme.test")
        resp = await client.post("/api/runs", json={"task": "Do the thing"})
        if allowed(role, "operator"):
            assert resp.status_code == 200, (role, resp.text)
            await finished(client, resp.json()["run_id"])
        else:
            assert resp.status_code == 403 and "operator" in resp.json()["detail"], role


async def test_starting_a_run_records_who_started_it_and_needs_an_environment(world, db_app):
    app, _ = db_app()
    client = await sign_in(app, "operator@acme.test")
    resp = await client.post("/api/runs", json={"task": "Do the thing"})
    run_id = resp.json()["run_id"]
    assert resp.json()["started_by"]["email"] == "operator@acme.test" and resp.json()["started_by"]["role"] == "operator"
    detail = await finished(client, run_id)
    assert detail["started_by"]["name"] == "Omar Operator"
    row = (await client.get("/api/runs")).json()[0]
    assert row["id"] == run_id and row["started_by"]["email"] == "operator@acme.test"
    async with world.sm() as session, session.begin():  # an org without a simulated company cannot run anything
        await session.execute(Org.__table__.update().where(Org.slug == "globex").values(sim_tenant_id=None))
    blocked = await (await sign_in(app, "operator@globex.test")).post("/api/runs", json={"task": "x"})
    assert blocked.status_code == 409 and "no environment" in blocked.json()["detail"].lower()


async def test_the_org_concurrency_limit_holds_runs_in_the_queue_and_orgs_are_independent(world, db_app):
    app, planner = db_app()
    admin = await sign_in(app, "admin@acme.test")
    assert (await admin.put("/api/admin/settings", json={"max_concurrent_runs": 1})).status_code == 200
    planner.add(*write_script())  # acme's first run waits for approval and keeps its only slot
    acme_op = await sign_in(app, "operator@acme.test")
    first = await start_run(acme_op)
    await pending_request(acme_op, first)
    second = await start_run(await sign_in(app, "operator2@acme.test"), "second")  # accepted, but it has to wait
    await asyncio.sleep(0.3)
    rows = {r["id"]: r for r in (await acme_op.get("/api/runs")).json()}
    assert rows[first]["status"] == "waiting_approval" and rows[second]["status"] == "queued"
    assert rows[second]["queue_position"] == 1 and rows[second]["active"] and rows[first]["queue_position"] is None
    detail = (await acme_op.get(f"/api/runs/{second}")).json()
    assert detail["status"] == "queued" and detail["queue_position"] == 1 and detail["pending"] == []
    globex = await sign_in(app, "operator@globex.test")
    globex_run = await start_run(globex)  # not held back by acme's limit
    assert globex_run != first
    await finished(globex, globex_run)
    assert (await acme_op.post(f"/api/runs/{first}/cancel")).status_code == 200
    await finished(acme_op, first)
    assert (await finished(acme_op, second))["status"] == "needs_attention"  # the freed slot let it start (the default script gives up)


# --------------------------------------------------------------------------- approvals


async def raise_approval(app, planner, starter: str, script) -> tuple:
    """The starter's client, the run id and its open approval request."""
    planner.add(*script)
    client = await sign_in(app, starter)
    run_id = await start_run(client)
    return client, run_id, await pending_request(client, run_id)


async def test_operators_cannot_approve_by_default_but_approvers_can_and_it_is_recorded(world, db_app):
    app, planner = db_app()
    starter, run_id, request = await raise_approval(app, planner, "operator@acme.test", write_script())
    assert request["kind"] == "approval" and request["payload"]["risk"] == "write"
    answer = {"request_id": request["request_id"], "approved": True}

    for who, snippet in (("operator2@acme.test", "operator"), ("viewer@acme.test", "viewer")):
        resp = await (await sign_in(app, who)).post(f"/api/runs/{run_id}/respond", json=answer)
        assert resp.status_code == 403 and snippet in resp.json()["detail"], who
    assert "allow operators" in (await (await sign_in(app, "operator2@acme.test")).post(
        f"/api/runs/{run_id}/respond", json=answer)).json()["detail"]
    assert (await starter.get(f"/api/runs/{run_id}")).json()["pending"], "refused answers must not resolve the request"

    approver = await sign_in(app, "approver@acme.test")
    ok = await approver.post(f"/api/runs/{run_id}/respond", json={**answer, "note": "looks fine"})
    assert ok.status_code == 200 and ok.json() == {"status": "ok", "kind": "approval"}
    detail = await finished(starter, run_id)
    approval = next(s for s in detail["steps"] if s["kind"] == "approval")
    assert approval["ok"] and approval["observation"].startswith("APPROVED by Alice Approver:")
    resolved = next(e for e in (await ws_events(app, approver, run_id)) if e["type"] == "approval_resolved")
    assert resolved["payload"]["response"]["by"]["email"] == "approver@acme.test"
    assert resolved["payload"]["response"]["by"]["role"] == "approver"

    async with world.sm() as session:
        row = (await session.scalars(select(AuditLog).where(AuditLog.action == "approval.respond"))).one()
    assert row.user_id == world.user_ids["approver@acme.test"] and row.org_id == world.org_ids["acme"]
    assert row.details["approved"] is True and row.details["risk"] == "write" and row.target_id == run_id


async def ws_events(app, client, run_id: str) -> list[dict]:
    from dbapp import session_cookie, ws_connect

    result = await ws_connect(app, f"/api/runs/{run_id}/events", session_cookie(client))
    assert result.accepted
    return result.messages


async def test_operators_can_approve_when_the_org_allows_it(world, db_app):
    app, planner = db_app()
    admin = await sign_in(app, "admin@acme.test")
    assert (await admin.put("/api/admin/settings", json={"operators_can_approve": True})).status_code == 200
    starter, run_id, request = await raise_approval(app, planner, "operator@acme.test", write_script())
    resp = await (await sign_in(app, "operator2@acme.test")).post(
        f"/api/runs/{run_id}/respond", json={"request_id": request["request_id"], "approved": True})
    assert resp.status_code == 200
    assert (await finished(starter, run_id))["steps"][0]["observation"].startswith("APPROVED by Olga Operator")


async def test_separation_of_duties_blocks_the_starter_from_approving_irreversible_actions(world, db_app):
    app, planner = db_app()
    # an admin both starts the run and holds approval rights: the only thing stopping them is separation of duties
    starter, run_id, request = await raise_approval(app, planner, "admin@acme.test", wire_script())
    assert request["payload"]["risk"] == "irreversible"
    answer = {"request_id": request["request_id"], "approved": True}
    blocked = await starter.post(f"/api/runs/{run_id}/respond", json=answer)
    assert blocked.status_code == 403 and "separation of duties" in blocked.json()["detail"].lower()
    assert (await starter.get(f"/api/runs/{run_id}")).json()["pending"]

    other = await sign_in(app, "approver@acme.test")
    assert (await other.post(f"/api/runs/{run_id}/respond", json=answer)).status_code == 200
    detail = await finished(starter, run_id)
    assert detail["steps"][0]["observation"].startswith("APPROVED by Alice Approver") and detail["steps"][1]["ok"]  # it ran

    # the starter may always deny their own run's action
    planner.add(*wire_script())
    run2 = await start_run(starter)
    req2 = await pending_request(starter, run2)
    denied = await starter.post(f"/api/runs/{run2}/respond", json={"request_id": req2["request_id"], "approved": False, "note": "changed my mind"})
    assert denied.status_code == 200
    assert (await finished(starter, run2))["steps"][0]["observation"].startswith("DENIED by Adam Admin")


async def test_writes_are_open_to_the_starter_unless_sod_for_writes_is_on(world, db_app):
    app, planner = db_app()
    admin, run_id, request = await raise_approval(app, planner, "admin@acme.test", write_script())
    ok = await admin.post(f"/api/runs/{run_id}/respond", json={"request_id": request["request_id"], "approved": True})
    assert ok.status_code == 200  # a WRITE the starter approves themselves
    await finished(admin, run_id)

    assert (await admin.put("/api/admin/settings", json={"sod_for_writes": True})).status_code == 200
    planner.add(*write_script())
    run2 = await start_run(admin)
    req2 = await pending_request(admin, run2)
    refused = await admin.post(f"/api/runs/{run2}/respond", json={"request_id": req2["request_id"], "approved": True})
    assert refused.status_code == 403 and "write" in refused.json()["detail"]
    other = await sign_in(app, "owner@acme.test")
    assert (await other.post(f"/api/runs/{run2}/respond", json={"request_id": req2["request_id"], "approved": True})).status_code == 200
    await finished(admin, run2)

    # switching separation of duties off lets the starter approve irreversible actions too
    assert (await admin.put("/api/admin/settings", json={"separation_of_duties": False, "sod_for_writes": False})).status_code == 200
    planner.add(*wire_script())
    run3 = await start_run(admin)
    req3 = await pending_request(admin, run3)
    assert (await admin.post(f"/api/runs/{run3}/respond", json={"request_id": req3["request_id"], "approved": True})).status_code == 200
    await finished(admin, run3)


async def test_viewers_cannot_answer_questions_but_other_roles_can(world, db_app):
    app, planner = db_app()
    ask = [reply("", call("ask_user", question="Which vendor?", options=["A", "B"])), reply("", call("finish", status="blocked", summary="ok"))]
    starter, run_id, request = await raise_approval(app, planner, "operator@acme.test", ask)
    assert request["kind"] == "question"
    answer = {"request_id": request["request_id"], "answer": "A"}
    viewer = await sign_in(app, "viewer@acme.test")
    assert (await viewer.post(f"/api/runs/{run_id}/respond", json=answer)).status_code == 403
    ok = await (await sign_in(app, "approver@acme.test")).post(f"/api/runs/{run_id}/respond", json=answer)
    assert ok.status_code == 200 and ok.json()["kind"] == "question"
    detail = await finished(starter, run_id)
    assert "User answered: A" in detail["steps"][0]["observation"]
    async with world.sm() as session:
        assert (await session.scalars(select(AuditLog.action).where(AuditLog.action == "question.answer"))).one()


async def test_approval_mode_of_the_org_is_a_floor_and_limits_come_from_the_org(world, db_app):
    app, planner = db_app()
    admin = await sign_in(app, "admin@acme.test")
    out = await admin.put("/api/admin/settings", json={"max_steps": 7, "run_timeout_s": 120, "approval_timeout_s": 90})
    assert out.status_code == 200 and out.json()["settings"]["max_steps"] == 7
    operator = await sign_in(app, "operator@acme.test")
    looser = await start_run(operator, approval_mode="auto")  # asking for less confirmation than the org requires
    await finished(operator, looser)
    runner = runner_of(app, looser)
    assert runner.policy.mode == "confirm_writes"
    assert (runner.settings.MAX_STEPS, runner.settings.RUN_TIMEOUT_S, runner.broker.default_timeout) == (7, 120, 90)
    stricter = await start_run(operator, approval_mode="confirm_all")
    await finished(operator, stricter)
    assert runner_of(app, stricter).policy.mode == "confirm_all"


# --------------------------------------------------------------------------- quota


async def test_per_org_daily_run_quota(world, db_app):
    app, _ = db_app()
    acme_admin = await sign_in(app, "admin@acme.test")
    assert (await acme_admin.put("/api/admin/settings", json={"max_runs_per_day": 2})).status_code == 200
    operator = await sign_in(app, "operator@acme.test")
    assert (await operator.get("/api/config")).json()["runs_left_today"] == 2
    for _ in range(2):
        await finished(operator, await start_run(operator))
    config = (await operator.get("/api/config")).json()
    assert (config["max_runs_per_day"], config["runs_left_today"]) == (2, 0)
    limited = await operator.post("/api/runs", json={"task": "one too many"})
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) > 0 and "limit of 2 runs" in limited.json()["detail"]

    globex = await sign_in(app, "operator@globex.test")  # another org has its own count and its own limit (default 50)
    await finished(globex, await start_run(globex))
    assert (await globex.get("/api/config")).json()["runs_left_today"] == 49

    assert (await acme_admin.put("/api/admin/settings", json={"max_runs_per_day": 3})).status_code == 200
    await finished(operator, await start_run(operator))
    assert (await operator.post("/api/runs", json={"task": "x"})).status_code == 429
    assert (await acme_admin.put("/api/admin/settings", json={"max_runs_per_day": 0})).status_code == 200  # 0 = unlimited
    cfg = (await operator.get("/api/config")).json()
    assert cfg["max_runs_per_day"] is None and cfg["runs_left_today"] is None
    await finished(operator, await start_run(operator))


# --------------------------------------------------------------------------- admin API


async def test_admin_can_add_change_and_deactivate_members(world, db_app):
    app, _ = db_app()
    admin = await sign_in(app, "admin@acme.test")
    members = (await admin.get("/api/admin/members")).json()
    assert [m["role"] for m in members][:2] == ["owner", "admin"] and len(members) == 7
    assert all(set(m) == {"user_id", "email", "display_name", "role", "is_active", "last_login_at", "joined_at"} for m in members)

    created = await admin.post("/api/admin/members", json={"email": "New.Person@Acme.test", "display_name": "Nina New", "role": "operator"})
    assert created.status_code == 201
    body = created.json()
    assert body["created_user"] is True and len(body["initial_password"]) >= 12 and body["email"] == "new.person@acme.test"
    newcomer = new_client(app, "198.51.100.50")
    login = await newcomer.post(LOGIN, json={"email": "new.person@acme.test", "password": body["initial_password"]})
    assert login.status_code == 200 and login.json()["org"]["role"] == "operator"
    assert "initial_password" not in (await admin.get("/api/admin/members")).text  # shown once, never again

    dupe = await admin.post("/api/admin/members", json={"email": "new.person@acme.test", "role": "viewer"})
    assert dupe.status_code == 409
    existing = await admin.post("/api/admin/members", json={"email": "operator@globex.test", "role": "viewer"})  # a person from another org
    assert existing.status_code == 201 and existing.json()["created_user"] is False and existing.json()["initial_password"] is None
    chosen = await admin.post("/api/admin/members", json={"email": "choice@acme.test", "role": "viewer", "password": "my-own-pass-123"})
    assert chosen.status_code == 201 and chosen.json()["initial_password"] is None  # an admin-chosen password is not echoed back
    assert (await new_client(app).post(LOGIN, json={"email": "choice@acme.test", "password": "my-own-pass-123"})).status_code == 200
    assert (await admin.post("/api/admin/members", json={"email": "not-an-email", "role": "viewer"})).status_code == 422
    assert (await admin.post("/api/admin/members", json={"email": "x@acme.test", "role": "viewer", "password": "short"})).status_code == 422
    assert (await admin.post("/api/admin/members", json={"email": "x@acme.test", "role": "boss"})).status_code == 422

    # who may assign what
    assert (await admin.post("/api/admin/members", json={"email": "king@acme.test", "role": "owner"})).status_code == 403
    owner = await sign_in(app, "owner@acme.test")
    assert (await owner.post("/api/admin/members", json={"email": "king@acme.test", "role": "owner"})).status_code == 201
    victor = world.user_ids["viewer@acme.test"]
    assert (await admin.patch(f"/api/admin/members/{victor}", json={"role": "approver"})).json()["role"] == "approver"
    assert (await admin.patch(f"/api/admin/members/{victor}", json={"role": "owner"})).status_code == 403
    assert (await admin.patch(f"/api/admin/members/{world.user_ids['owner@acme.test']}", json={"role": "viewer"})).status_code == 403
    mine = world.user_ids["admin@acme.test"]
    assert (await admin.patch(f"/api/admin/members/{mine}", json={"role": "owner"})).status_code == 400  # not yourself
    assert (await admin.patch(f"/api/admin/members/{mine}", json={"is_active": False})).status_code == 400
    assert (await admin.patch(f"/api/admin/members/{victor}", json={})).status_code == 422
    assert (await admin.patch("/api/admin/members/00000000-0000-0000-0000-000000000009", json={"role": "viewer"})).status_code == 404
    assert (await admin.patch(f"/api/admin/members/{world.user_ids['operator@globex.test']}", json={"role": "viewer"})).json()["role"] == "viewer"

    # a role change applies to the very next request of that person
    approver = await sign_in(app, "viewer@acme.test")
    assert (await approver.get("/api/auth/me")).json()["org"]["role"] == "approver"
    assert (await admin.patch(f"/api/admin/members/{victor}", json={"role": "viewer"})).status_code == 200
    assert (await approver.get("/api/auth/me")).json()["org"]["role"] == "viewer"

    # deactivation ends their sessions in this org and blocks sign-in; reactivation restores it
    target = world.user_ids["operator2@acme.test"]
    op2 = await sign_in(app, "operator2@acme.test")
    assert (await admin.patch(f"/api/admin/members/{target}", json={"is_active": False})).json()["is_active"] is False
    assert (await op2.get("/api/runs")).status_code == 401
    refused = await new_client(app).post(LOGIN, json={"email": "operator2@acme.test", "password": PASSWORD})
    assert refused.status_code == 403 and "not an active member" in refused.json()["detail"]
    listing = {m["email"]: m for m in (await admin.get("/api/admin/members")).json()}
    assert listing["operator2@acme.test"]["is_active"] is False
    assert (await admin.patch(f"/api/admin/members/{target}", json={"is_active": True})).json()["is_active"] is True
    assert (await new_client(app).post(LOGIN, json={"email": "operator2@acme.test", "password": PASSWORD})).status_code == 200


async def test_deactivating_in_one_org_keeps_the_other_org_working(world, db_app):
    app, _ = db_app()
    admin = await sign_in(app, "admin@acme.test")
    multi = await sign_in(app, MULTI_EMAIL)
    assert (await multi.get("/api/auth/me")).json()["org"]["slug"] == "acme"
    assert (await admin.patch(f"/api/admin/members/{world.user_ids[MULTI_EMAIL]}", json={"is_active": False})).status_code == 200
    me = await sign_in(app, MULTI_EMAIL)  # still a member of globex: signs in there
    assert (await me.get("/api/auth/me")).json()["org"]["slug"] == "globex"


async def test_org_settings_are_validated_and_audited(world, db_app):
    app, _ = db_app()
    admin = await sign_in(app, "admin@acme.test")
    current = (await admin.get("/api/admin/settings")).json()["settings"]
    assert {"approval_mode": "confirm_writes", "separation_of_duties": True, "sod_for_writes": False,
            "operators_can_approve": False, "approval_timeout_s": 3600, "max_runs_per_day": 50, "max_steps": 60,
            "run_timeout_s": 1800}.items() <= current.items()  # settings added by later milestones are checked in their own tests
    assert (current["max_tokens_per_day"], current["max_llm_calls_per_day"], current["gemini_models"]) == (2_000_000, 500, [])
    ok = await admin.put("/api/admin/settings", json={"approval_mode": "confirm_all", "max_steps": 25, "operators_can_approve": True})
    assert ok.status_code == 200 and ok.json()["changed"] == ["approval_mode", "max_steps", "operators_can_approve"]
    again = await admin.put("/api/admin/settings", json={"max_steps": 25})
    assert again.json()["changed"] == []
    async with world.sm() as session:
        org = await session.get(Org, world.org_ids["acme"])
    assert org.settings["max_steps"] == 25 and org.settings["limits"] == {"runs_per_day": 0, "tokens_per_day": 0}  # other keys kept
    assert (await new_client(app).get("/api/admin/settings")).status_code == 401

    for bad in ({}, {"approval_mode": "yolo"}, {"max_steps": 0}, {"max_steps": 501}, {"max_steps": "10"}, {"max_steps": True},
                {"separation_of_duties": "yes"}, {"approval_timeout_s": 5}, {"max_runs_per_day": -1}, {"run_timeout_s": 1.5},
                {"limits": {"runs_per_day": 9}}, {"sod_for_writes": True, "separation_of_duties": False}):
        resp = await admin.put("/api/admin/settings", json=bad)
        assert resp.status_code == 422, (bad, resp.status_code)
    assert (await admin.get("/api/admin/settings")).json()["settings"]["max_steps"] == 25  # nothing partial was saved
    me = (await admin.get("/api/auth/me")).json()["org"]["policy"]  # members see the approval policy
    assert me["operators_can_approve"] is True and me["approval_mode"] == "confirm_all"

    async with world.sm() as session:
        rows = (await session.scalars(select(AuditLog).where(AuditLog.action == "org.settings_update").order_by(AuditLog.id))).all()
    assert len(rows) == 1 and rows[0].details["changed"]["max_steps"] == [60, 25]  # only real changes are recorded


async def test_credentials_api_never_returns_values_and_keeps_blank_secrets(world, db_app):
    app, _ = db_app()
    admin = await sign_in(app, "admin@acme.test")
    listing = (await admin.get("/api/admin/credentials")).json()
    assert [c["name"] for c in listing] == ["erp"]  # the internal sim_entry is not listed
    assert set(listing[0]) == {"name", "kind", "description", "url", "host", "fields", "created_at", "updated_at"}
    assert listing[0]["fields"] == ["password", "username"] and listing[0]["host"] == "localhost"

    created = await admin.put("/api/admin/credentials/mail", json={
        "kind": "login", "fields": {"username": "ap@acme.example", "password": "mail-secret-77"}, "description": "Webmail",
        "url": "http://localhost:8001/mail"})
    assert created.status_code == 200 and created.json()["created"] is True
    assert "mail-secret-77" not in created.text and "ap@acme.example" not in created.text
    updated = await admin.put("/api/admin/credentials/mail", json={"kind": "login", "fields": {"username": "", "password": "new-secret-88"},
                                                                    "description": "Webmail", "url": "http://localhost:8001/mail"})
    assert updated.json()["created"] is False and updated.json()["fields"] == ["password", "username"]  # blank username kept
    stored = await world.vault.get(str(world.org_ids["acme"]), "mail")
    assert stored.fields == {"username": "ap@acme.example", "password": "new-secret-88"}
    async with world.sm() as session:  # ciphertext only
        dump = repr((await session.execute(text("select * from credentials"))).all())
    assert "new-secret-88" not in dump and "ap@acme.example" not in dump and ENTRY_TOKENS["acme"] not in dump

    assert (await admin.put("/api/admin/credentials/sim_entry", json={"kind": "header", "fields": {"header_name": "x", "value": "y"}})).status_code == 422
    assert (await admin.put("/api/admin/credentials/bad name", json={"kind": "login", "fields": {"password": "x"}})).status_code == 422
    assert (await admin.put("/api/admin/credentials/nopass", json={"kind": "login", "fields": {"username": "u"}})).status_code == 422
    assert (await admin.put("/api/admin/credentials/x", json={"kind": "voodoo", "fields": {}})).status_code == 422
    assert (await admin.delete("/api/admin/credentials/sim_entry")).status_code == 404  # internal entries cannot be deleted either
    assert (await admin.delete("/api/admin/credentials/mail")).json() == {"status": "deleted", "name": "mail"}
    assert (await admin.delete("/api/admin/credentials/mail")).status_code == 404
    assert await world.vault.get(str(world.org_ids["acme"]), "sim_entry") is not None

    globex_admin = await sign_in(app, "admin@globex.test")  # another org's admin cannot see or delete acme's entries
    assert [c["name"] for c in (await globex_admin.get("/api/admin/credentials")).json()] == ["erp"]
    assert (await globex_admin.delete("/api/admin/credentials/erp")).status_code == 200
    assert (await world.vault.get(str(world.org_ids["acme"]), "erp")).fields["password"] == ERP_PASSWORDS["acme"]


async def test_the_audit_log_records_security_relevant_actions(world, db_app):
    app, planner = db_app()
    admin = await sign_in(app, "admin@acme.test")
    await admin.put("/api/admin/settings", json={"max_steps": 20})
    await admin.put("/api/admin/credentials/api", json={"kind": "api_key", "fields": {"api_key": "key-12345678"}})
    await admin.delete("/api/admin/credentials/api")
    created = (await admin.post("/api/admin/members", json={"email": "aud@acme.test", "role": "viewer"})).json()
    await admin.patch(f"/api/admin/members/{created['user_id']}", json={"role": "operator"})
    await admin.patch(f"/api/admin/members/{created['user_id']}", json={"is_active": False})
    await admin.patch(f"/api/admin/members/{created['user_id']}", json={"is_active": True})
    starter, run_id, request = await raise_approval(app, planner, "operator@acme.test", write_script())
    await (await sign_in(app, "approver@acme.test")).post(f"/api/runs/{run_id}/respond", json={"request_id": request["request_id"], "approved": True})
    await finished(starter, run_id)
    planner.add(*write_script())
    cancel_id = await start_run(starter)
    await pending_request(starter, cancel_id)
    await starter.post(f"/api/runs/{cancel_id}/cancel")
    await finished(starter, cancel_id)
    await admin.post("/api/auth/logout")

    actions = await audit_actions(world.sm, world.org_ids["acme"])
    for expected in ("auth.login", "org.settings_update", "credential.put", "credential.delete", "member.add", "member.role_change",
                     "member.deactivate", "member.reactivate", "run.start", "approval.respond", "run.cancel", "auth.logout"):
        assert expected in actions, expected
    assert await audit_actions(world.sm, world.org_ids["globex"]) == []  # nothing leaks into another org's trail
    log = (await admin.get("/api/admin/audit?limit=500"))
    assert log.status_code == 401  # the admin logged out above
    admin = await sign_in(app, "admin@acme.test")
    rows = (await admin.get("/api/admin/audit?limit=500")).json()
    assert rows[0]["action"] == "auth.login" and {r["user"] for r in rows} >= {"admin@acme.test", "operator@acme.test", "approver@acme.test"}
    assert (await admin.get("/api/admin/audit?limit=0")).status_code == 422
    assert "key-12345678" not in repr(rows)  # secrets never reach the trail
