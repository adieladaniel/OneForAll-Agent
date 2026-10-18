"""Tests for ``PolicyLLM``, the deterministic stand-in for a model used by the load test (``worker/llm/policy.py``).

The parsing and decision functions are tested on plain text. The end-to-end tests run the REAL ``AgentRunner`` with the real
tool registry, a real pooled Chromium and the simulated company served on a local port, then judge the outcome with the
eval ground-truth checkers (what is in the sim's ERP), never with what the policy says.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from company_sim import config, tenants
from evals.checkers import bill_mismatches, bills_for, run_check
from evals.simstate import build_sim_state, fetch_admin_state
from worker.agent.broker import AutoBroker
from worker.agent.runner import AgentRunner
from worker.agent.toolset import build_registry
from worker.agent.verifier import Verifier
from worker.config import Settings
from worker.llm.base import Message, ToolCall
from worker.llm.policy import (
    ERP_NOTE_KEY,
    PolicyLLM,
    is_verifier,
    parse_facts,
    parse_page,
    parse_task,
    plain_amount,
    same_amount,
)
from worker.store import FileRunStore
from worker.tools.browser import BrowserSession, close_browser
from worker.tools.browser_pool import POOL_KEY
from worker.tools.secrets import FillSecretTool
from worker.vault import Credential, VaultScope

LATEST_TASK = ("Find the latest invoice from Company X Ltd, extract the amount and due date, enter it into our internal "
               "system, and tell me once it is done.")

FORM_PAGE = """URL: http://127.0.0.1:8610/erp/bills/new
Title: New AP bill - Ledger - Accounts Payable
HTTP status of last navigation: 500
Alerts: [role=alert] "- Internal Server Error – please try again"
--- Page text ---
# New AP bill
- Internal Server Error – please try again
Vendor
Save bill[11] Cancel[12]
--- Interactive elements ---
[1] link "Bills" -> /erp/bills
[4] combobox "Vendor" selected="Globex Corporation" options=[-- Select vendor --, Company X Ltd, Globex Corporation, ...(+2)]
[5] textbox "Invoice number" value="GLX-88213"
[8] textbox "Issue date" value="2026-09-19" placeholder="YYYY-MM-DD"
[11] button "Save bill"
[12] link "Cancel" -> /erp/bills

=== CURRENT STATE (action 3 of at most 40) ===

Working memory:
- vendor: Globex Corporation  [portal invoice page /portal/invoices/GLX-88213, step 4]
- erp_new_bill_form: Dates are YYYY-MM-DD.  [learned, step 9]  (durable)
- amount: 5681.96

NOTICE: something"""


# --------------------------------------------------------------------------- parsing (no browser)


def test_parse_page_reads_elements_alerts_and_stops_before_the_state_block():
    page = parse_page("Selected 'X'.\n" + FORM_PAGE)
    assert page is not None and page.path == "/erp/bills/new" and page.status == 500
    assert page.alerts == ['[role=alert] "- Internal Server Error – please try again"']
    vendor = page.find("combobox", "vendor")
    assert vendor.ref == 4 and vendor.selected == "Globex Corporation"
    assert vendor.options == ["-- Select vendor --", "Company X Ltd", "Globex Corporation"]  # the "(+2 more)" marker is dropped
    assert page.find("textbox", "Invoice number").value == "GLX-88213"
    assert page.find("link", "Cancel").href == "/erp/bills"
    assert [e.ref for e in page.elements] == [1, 4, 5, 8, 11, 12]
    assert parse_page("ERROR: Browser action failed: timeout") is None
    assert parse_page("HTTP 200 GET http://x/erp/api/bills\n[]") is None


def test_parse_facts_handles_sources_and_the_durable_mark():
    facts = parse_facts(FORM_PAGE)
    assert facts == {"vendor": "Globex Corporation", "erp_new_bill_form": "Dates are YYYY-MM-DD.", "amount": "5681.96"}
    assert parse_facts("no memory here") == {}
    assert parse_facts("WORKER'S WORKING-MEMORY FACTS (unverified):\n- bill_id: 7  [ERP bill page, step 3]\n\nRe-check") == {"bill_id": "7"}


@pytest.mark.parametrize("task, kind, vendor, number", [
    (LATEST_TASK, "latest", "Company X Ltd", ""),
    ("Find the latest invoice from Company X, extract the amount and due date, enter it into our internal system.", "latest", "Company X", ""),
    ("Please find the latest invoice from Initech Supplies and enter it into the ERP.", "latest", "Initech Supplies", ""),
    ("Find invoice GLX-88213 in the vendor portal, extract the amount and due date and enter it into our internal system.", "invoice", "", "GLX-88213"),
    ("Enter invoice cx-2026-0412 into the ERP", "invoice", "", "CX-2026-0412"),
])
def test_parse_task_understands_both_task_shapes(task, kind, vendor, number):
    target = parse_task(task)
    assert (target.kind, target.vendor, target.number) == (kind, vendor, number)
    assert parse_task("Water the plants") is None


def test_the_policy_verdict_submits_structured_ambiguity_evidence_and_accurate_texts():
    from worker.agent.verifier import ambiguity_check
    from worker.llm.policy import _verdict

    call = _verdict(["c"], [("c", True, "ok")], "fine", "Globex Corporation", ["the bill page"]).calls[0]
    assert call[0] == "submit_verdict"
    args = call[1]
    assert args["ambiguity"]["matching_entities"] == [{"name": "Globex Corporation", "checked": True, "candidate": ""}]
    check, unchecked = ambiguity_check(args["ambiguity"], asked=False)
    assert check["passed"] and not unchecked
    assert "It opened the bill page." in args["completeness"]["observed"]
    assert "specific invoice" not in args["completeness"]["observed"]
    nothing = _verdict(["c"], [("c", False, "no")], "bad").calls[0][1]
    assert nothing["ambiguity"]["matching_entities"] == [] and "could not open any record" in nothing["completeness"]["observed"]
    assert ambiguity_check(nothing["ambiguity"], asked=False)[0]["passed"]


def test_amount_helpers():
    assert plain_amount("5,681.96 USD") == "5681.96" and plain_amount("12.50") == "12.50" and plain_amount("") == ""
    assert same_amount("5,681.96", "5681.960") and not same_amount("5,681.96", "5681.97") and not same_amount("abc", "1")


# --------------------------------------------------------------------------- decisions (no browser)


def user(task: str, state: str = "Plan:\n(no plan yet)\n\nWorking memory:\n(nothing remembered yet)") -> list[Message]:
    return [Message(role="system", content="You are an autonomous digital worker."),
            Message(role="user", content=f"Task from the user:\n{task}\n\nToday's date: 2026-10-16\n\n{state}")]


async def decide(messages, tools=None, think_ms: float = 0.0, **kwargs):
    from worker.llm.base import ToolSpec

    specs = [ToolSpec(n, "", {"type": "object", "properties": {}}) for n in (tools or [])]
    return await PolicyLLM(think_ms=think_ms, seed=1, **kwargs).complete(messages, specs or None)


async def test_first_turn_defines_the_plan_and_looks_at_the_credentials():
    response = await decide(user(LATEST_TASK))
    assert [c.name for c in response.tool_calls] == ["update_plan", "list_credentials"]
    plan = response.tool_calls[0].args
    assert len(plan["success_criteria"]) == 3 and "Company X Ltd" in plan["goal_summary"]
    assert all(item["status"] in ("pending", "in_progress") for item in plan["items"])
    assert response.usage.input_tokens > 0 and response.usage.output_tokens > 0


async def test_an_unknown_task_is_declined_honestly():
    response = await decide(user("Water the plants"))
    assert response.tool_calls[0].name == "finish" and response.tool_calls[0].args["status"] == "blocked"


async def test_a_resumed_run_ignores_the_stale_page_and_reopens_from_its_facts():
    state = ("Plan:\n[>] 1. Do it\n\nWorking memory:\n- vendor: Company X Ltd  [portal, step 2]\n"
             "- invoice_number: CX-2026-0412  [portal, step 2]\n\nNOTICE: Run resumed after an interruption; the browser was restarted")
    messages = user(LATEST_TASK, state)
    # the transcript before the crash still shows an old form page
    messages += [Message(role="assistant", tool_calls=[ToolCall(id="c1", name="browser_click", args={"ref": 11})]),
                 Message(role="tool", tool_call_id="c1", name="browser_click", content=FORM_PAGE.split("\n\n=== ")[0] + "\n\n=== CURRENT STATE\n" + state)]
    response = await decide(messages)
    # it does not click "Save bill" on the dead page; it checks the ERP for the invoice first (never repeat a write blindly)
    assert [(c.name, c.args) for c in response.tool_calls] == [("browser_navigate", {"url": "/erp/bills?q=CX-2026-0412"})]


async def test_server_error_is_re_read_once_then_the_save_is_retried():
    facts = ("- vendor: Globex Corporation\n- invoice_number: GLX-88213\n- amount: 5681.96\n- currency: USD\n- issue_date: 2026-09-19\n"
             "- due_date: 2026-10-19\n- save_attempts: 1")
    state = f"Plan:\n[>] 1. Do it\n\nWorking memory:\n{facts}"
    page = FORM_PAGE.split("\n\n=== ")[0].replace("[5] textbox", "[5] textbox")
    # the page above has the amount, currency and due date missing from the form, so use a complete one
    complete = page.replace('[8] textbox "Issue date"', '[6] textbox "Amount" value="5681.96"\n[7] combobox "Currency" selected="USD" options=[USD, EUR, GBP]\n'
                            '[9] textbox "Due date" value="2026-10-19"\n[8] textbox "Issue date"')

    def turn(last_tool: str, last_args: dict) -> list[Message]:
        messages = user(LATEST_TASK, state)
        return messages + [Message(role="assistant", tool_calls=[ToolCall(id="c9", name=last_tool, args=last_args)]),
                           Message(role="tool", tool_call_id="c9", name=last_tool, content=complete + "\n\n=== CURRENT STATE\n" + state)]

    first = await decide(turn("browser_click", {"ref": 11}))
    assert [c.name for c in first.tool_calls] == ["browser_read_page"]
    second = await decide(turn("browser_read_page", {}))
    assert [c.name for c in second.tool_calls] == ["remember", "browser_click"]
    assert second.tool_calls[0].args["value"] == "2"  # save_attempts


async def test_a_rejected_audit_reopens_the_portal_once_then_gives_up_honestly():
    facts = "- vendor: Globex Corporation\n- invoice_number: GLX-88213\n- bill_id: 7"

    def after_audit(extra: str = "") -> list[Message]:
        state = f"Plan:\n[>] 1. Do it\n\nWorking memory:\n{facts}{extra}"
        return user(LATEST_TASK, state) + [
            Message(role="assistant", tool_calls=[ToolCall(id="f1", name="finish", args={"status": "success"})]),
            Message(role="tool", tool_call_id="f1", name="finish",
                    content="ERROR: Independent verification FAILED.\n\n=== CURRENT STATE\n" + state)]

    first = await decide(after_audit())
    assert [(c.name, c.args.get("url") or c.args.get("key")) for c in first.tool_calls] == [
        ("remember", "audit_resync"), ("browser_navigate", "/portal/invoices/GLX-88213")]
    second = await decide(after_audit("\n- audit_resync: reopened"))
    assert second.tool_calls[0].name == "finish" and second.tool_calls[0].args["status"] == "failed"


async def test_think_time_is_jittered_and_zero_means_none():
    started = time.monotonic()
    await decide(user(LATEST_TASK), think_ms=120)
    assert 0.06 <= time.monotonic() - started < 0.5
    started = time.monotonic()
    await decide(user(LATEST_TASK))
    assert time.monotonic() - started < 0.1


def test_the_factory_builds_the_policy_provider_without_a_gemini_key():
    from worker.llm.factory import make_llm

    llm = make_llm(Settings(_env_file=None, LLM_PROVIDER="policy", POLICY_THINK_MS=0))
    assert llm.name == "policy" and isinstance(llm.inner, PolicyLLM) and llm.inner.think_ms == 0
    # no model-style throttling for the stand-in unless a shared limiter is configured
    assert llm.limiter.min_interval_s == 0.0


# --------------------------------------------------------------------------- end to end: real tools, real browser, real sim


def free_port() -> int:
    for port in range(8701, 8800):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port in 8701-8799")


@pytest.fixture(scope="module")
def sim_url(tmp_path_factory):
    """The company simulator on a temporary data dir, served on a local port in a thread."""
    data = tmp_path_factory.mktemp("policy_sim")
    patch = pytest.MonkeyPatch()
    patch.setattr(config, "DATA_DIR", data)
    patch.setattr(config, "DB_PATH", data / "sim.db")
    patch.setattr(config, "PDF_DIR", data / "pdfs")
    patch.setattr(config, "WORKSPACE_DIR", data / "workspace")
    patch.delenv("SIM_ADMIN_TOKEN", raising=False)
    from company_sim.app import app

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "the simulator did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)
    patch.undo()


class Env:
    """One tenant of the sim plus what a run of its organization gets (vault scope, pinned cookie)."""

    def __init__(self, base: str, tenant_id: str, chaos: list[str], tmp_path):
        self.base, self.tmp_path = base, tmp_path
        self.tenant = tenants.provision_tenant(tenant_id, chaos)
        creds = self.tenant.credentials
        host = "127.0.0.1"
        self.scope = VaultScope(tenant_id, [
            Credential(tenant_id, "portal", "login", dict(creds["portal"]), url=f"{base}/portal", host=host),
            Credential(tenant_id, "erp", "login", dict(creds["erp"]), url=f"{base}/erp", host=host),
            Credential(tenant_id, "erp_api", "api_key", {"api_key": creds["erp_api_key"], "header_name": "X-API-Key"}, host=host),
            Credential(tenant_id, "sim_entry", "header", {"header_name": "X-Sim-Tenant", "value": self.tenant.token}, host=host),
        ])
        self.cookies = [{"name": "sim_tenant", "value": self.tenant.token, "url": f"{base}/", "httpOnly": True, "sameSite": "Lax"}]
        self.baseline = fetch_admin_state(base, tenant_id=tenant_id)

    def settings(self) -> Settings:
        return Settings(_env_file=None, HEADLESS=True, SIM_BASE_URL=self.base, WORKSPACE_DIR=str(self.tmp_path / "ws"),
                        RUNS_DIR=str(self.tmp_path / "runs"), APPROVAL_MODE="confirm_writes", MAX_STEPS=60, RUN_TIMEOUT_S=300)

    def sim_state(self) -> dict:
        return build_sim_state(fetch_admin_state(self.base, tenant_id=self.tenant.id), self.baseline)

    def runner(self, task: str, pool, broker: AutoBroker | None = None) -> AgentRunner:
        settings = self.settings()
        runner = AgentRunner(task, PolicyLLM(think_ms=0), build_registry(settings), settings, broker or AutoBroker.approve_all(),
                             store=FileRunStore(self.tmp_path / "runs"), vault_scope=self.scope)
        runner.ctx.extras[POOL_KEY] = pool
        runner.ctx.extras["browser_init_cookies"] = self.cookies
        return runner


def approvals_of(broker: AutoBroker) -> list[dict]:
    return [payload for kind, payload in broker.requests if kind == "approval"]


async def test_full_run_enters_the_latest_invoice_and_passes_the_ground_truth_check(sim_url, browser_pool, tmp_path):
    env = Env(sim_url, "pol-happy", [], tmp_path)
    broker = AutoBroker.approve_all()
    state = await env.runner(LATEST_TASK, browser_pool, broker).run()

    assert state.status == "succeeded", state.final_report
    sim = env.sim_state()
    result = run_check("invoice_entry", state.to_dict(), sim)
    assert result.passed, result.details
    # exactly one write was gated: the "Save bill" click, with the filled form shown to the approver
    approvals = approvals_of(broker)
    assert [(a["tool"], a["risk"]) for a in approvals] == [("browser_click", "write")]
    assert {r["label"]: r["value"] for r in approvals[0]["context"]["form_values"]}["Invoice number"].startswith("CX-")
    # the facts a human would want, remembered with their source, and one durable how-to note (not task data)
    assert state.facts["bill_status"].value == "created" and state.facts["pdf_check"].value.startswith("the PDF agrees")
    note = state.facts[ERP_NOTE_KEY]
    assert note.durable and note.scope == "erp" and not any(ch.isdigit() for ch in note.value)
    # the independent audit really ran, with the policy as the auditor, and passed
    assert [s.tool for s in state.steps if s.kind == "verify"] == ["browser_navigate", "browser_navigate", "http_request"]
    assert state.final_report["outputs"]["bill_id"] == state.facts["bill_id"].value
    # every login went through the vault: the password is never in the transcript
    assert env.tenant.credentials["erp"]["password"] not in (tmp_path / "runs" / state.run_id / "run.json").read_text(encoding="utf-8")
    assert browser_pool.active_contexts == 0


async def test_chaos_server_error_and_session_expiry_still_end_with_exactly_one_correct_bill(sim_url, browser_pool, tmp_path):
    env = Env(sim_url, "pol-chaos", ["erp_submit_500_once", "erp_session_expiry"], tmp_path)
    broker = AutoBroker.approve_all()
    task = ("Find invoice INI-20502 in the vendor portal, extract the amount and due date, enter it into our internal "
            "system, and tell me once it is done.")
    state = await env.runner(task, browser_pool, broker).run()

    assert state.status == "succeeded", state.final_report
    sim = env.sim_state()
    bills = bills_for(sim, "INI-20502")
    invoice = next(i for i in sim["invoices"] if i["number"] == "INI-20502")
    assert len(bills) == 1 and bill_mismatches(bills[0], invoice) == [], (bills, invoice)
    assert [b for b in sim["bills"] if b["id"] not in {x["id"] for x in sim["baseline_bills"]}] == bills  # nothing else created
    assert len(approvals_of(broker)) >= 2  # the failed save was retried (and approved again)
    assert sum(1 for s in state.steps if s.tool == "browser_fill_secret") >= 6  # logged in again after the session expired


async def test_parallel_runs_of_one_org_create_one_bill_and_report_the_other_as_existing(sim_url, browser_pool, tmp_path):
    env = Env(sim_url, "pol-race", [], tmp_path)
    brokers = [AutoBroker.approve_all(), AutoBroker.approve_all()]
    states = await asyncio.gather(*(env.runner(LATEST_TASK, browser_pool, b).run() for b in brokers))

    assert [s.status for s in states] == ["succeeded", "succeeded"], [s.final_report for s in states]
    sim = env.sim_state()
    assert run_check("invoice_entry", states[0].to_dict(), sim).passed
    assert sorted(s.facts["bill_status"].value for s in states) == ["created", "exists"]  # one wins the save, the other sees its bill
    # a third run later finds the bill and does not ask anybody for approval
    third_broker = AutoBroker.approve_all()
    third = await env.runner(LATEST_TASK, browser_pool, third_broker).run()
    assert third.status == "succeeded" and third.facts["bill_status"].value == "exists" and approvals_of(third_broker) == []
    assert run_check("invoice_entry", third.to_dict(), env.sim_state()).passed
    assert "already recorded" in third.final_report["summary"]


async def test_a_denied_approval_ends_honestly_without_a_bill(sim_url, browser_pool, tmp_path):
    env = Env(sim_url, "pol-deny", [], tmp_path)
    broker = AutoBroker(lambda kind, payload: {"approved": False, "note": "not today"})
    state = await env.runner(LATEST_TASK, browser_pool, broker).run()
    assert state.status == "needs_attention" and not env.sim_state()["bills"][len(env.baseline["bills"]):]


async def test_the_worker_recovers_when_the_auditor_cannot_see_the_portal(sim_url, browser_pool, tmp_path, monkeypatch):
    """The fallback for an auditor that cannot sign in (here: it is not given ``browser_fill_secret``). The portal session is
    gone, so the first audit fails; the worker opens the portal again and the second audit passes."""
    monkeypatch.setattr(FillSecretTool, "verifier_allowed", False)
    env = Env(sim_url, "pol-resync", [], tmp_path)
    holder: dict = {}

    async def lose_the_portal_session(kind: str, payload: dict) -> dict:
        if kind == "approval":  # between the work and the audit, the portal session is gone
            await holder["runner"].ctx.extras["browser"]._context.clear_cookies(name="portal_session")
        return {"approved": True}

    runner = holder["runner"] = env.runner(LATEST_TASK, browser_pool, AutoBroker(lose_the_portal_session))
    state = await runner.run()

    assert state.status == "succeeded", state.final_report
    assert [s.error for s in state.steps if s.tool == "finish"] == ["verification_failed", None]
    assert "audit_resync" in state.facts
    assert run_check("invoice_entry", state.to_dict(), env.sim_state()).passed


class SpyLLM(PolicyLLM):
    """A PolicyLLM that keeps what the auditor was sent (after the runner's redaction) and the tools it was offered."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.auditor_calls: list[tuple[list[Message], set[str]]] = []

    async def complete(self, messages, tools=None, **kwargs):
        if is_verifier(messages):
            self.auditor_calls.append((list(messages), {t.name for t in tools or []}))
        return await super().complete(messages, tools, **kwargs)


async def test_the_auditor_signs_in_when_its_browser_is_fresh_and_passes_the_first_round(sim_url, browser_pool, tmp_path, monkeypatch):
    """After a crash/resume (or a session expiry) the run's browser is new and signed in nowhere. The read-only auditor signs in
    with list_credentials + browser_fill_secret, re-observes both systems and passes on the first audit round. No workaround
    round of the worker is needed, and no secret reaches the auditor's messages, the recorded steps or the events."""
    env = Env(sim_url, "pol-login", [], tmp_path)
    original = Verifier.verify

    async def verify_in_a_fresh_browser(self, **kwargs):
        await kwargs["ctx"].extras["browser"].reset()  # the worker's context is dropped; the next page gets an empty one
        return await original(self, **kwargs)

    monkeypatch.setattr(Verifier, "verify", verify_in_a_fresh_browser)
    runner = env.runner(LATEST_TASK, browser_pool)
    spy = runner.llm = SpyLLM(think_ms=0)
    events: list[tuple[str, dict]] = []

    async def sink(kind: str, payload: dict) -> None:
        events.append((kind, payload))

    runner._sink = sink
    state = await runner.run()

    assert state.status == "succeeded", state.final_report
    assert [s.error for s in state.steps if s.tool == "finish"] == [None]  # passed at once
    assert "audit_resync" not in state.facts
    assert run_check("invoice_entry", state.to_dict(), env.sim_state()).passed
    assert state.final_report["verification"]["passed"]

    audit = [s for s in state.steps if s.kind == "verify"]
    assert [s.tool for s in audit] == ["browser_navigate", "list_credentials", "browser_fill_secret", "browser_fill_secret",
                                       "browser_click", "browser_navigate", "browser_fill_secret", "browser_fill_secret",
                                       "browser_click", "http_request"]
    assert all(s.ok for s in audit), [(s.tool, s.error) for s in audit if not s.ok]
    assert {(s.args["credential"], s.args["field"]) for s in audit if s.tool == "browser_fill_secret"} == {
        ("erp", "username"), ("erp", "password"), ("portal", "username"), ("portal", "password")}
    assert all("browser_fill_secret" in names for _, names in spy.auditor_calls[:-1])

    # redaction: the passwords are in no auditor message, step, event or file of the run
    secrets = [env.tenant.credentials[app]["password"] for app in ("portal", "erp")] + [env.tenant.credentials["erp_api_key"]]
    seen = "\n".join([
        *(m.content + json.dumps([c.args for c in m.tool_calls]) for messages, _ in spy.auditor_calls for m in messages),
        json.dumps(state.to_dict(), default=str), json.dumps(events, default=str),
        (tmp_path / "runs" / state.run_id / "run.json").read_text(encoding="utf-8")])
    assert spy.auditor_calls and "•••• (credential portal.password)" in seen
    for secret in secrets:
        assert secret not in seen


async def test_an_auditor_that_cannot_sign_in_reports_that_it_could_not_see_the_page(sim_url, browser_pool, tmp_path):
    """A sign-in that is rejected is tried once, not forever; the verdict then says the page could not be opened."""
    env = Env(sim_url, "pol-badlogin", [], tmp_path)
    wrong = Credential("pol-badlogin", "portal", "login", {"username": "nobody", "password": "wrong-password-1"},
                       url=f"{sim_url}/portal", host="127.0.0.1")
    env.scope = VaultScope("pol-badlogin", [c for c in env.scope.credentials() if c.name != "portal"] + [wrong])
    runner = env.runner("audit", browser_pool)
    ctx = runner.ctx
    sim = env.sim_state()
    invoice = next(i for i in sim["invoices"] if i["number"].startswith("INI-20502"))
    created = httpx.post(f"{sim_url}/erp/api/bills", headers={"X-API-Key": env.tenant.credentials["erp_api_key"]}, json={
        "vendor": invoice["vendor"], "invoice_number": invoice["number"], "amount": invoice["amount"],
        "currency": invoice["currency"], "issue_date": invoice["issue_date"], "due_date": invoice["due_date"]})
    assert created.status_code == 201, created.text
    try:
        claim = {"summary": "done", "outputs": {"bill_id": str(created.json()["id"]), "invoice_number": invoice["number"]}, "evidence": []}
        verdict = await Verifier(PolicyLLM(think_ms=0), runner.registry).verify(
            task="Enter invoice INI-20502", criteria=["Exactly one bill exists.", "Values match.", "Dates match."], claim=claim,
            facts_text="(nothing)", ctx=ctx)
    finally:
        await close_browser(ctx)
    assert not verdict.passed and not verdict.inconclusive
    assert any("Could not open the invoice in the vendor portal" in c["observed"] for c in verdict.failed_checks)
    assert verdict.llm_calls <= 12


async def login_both(ctx, env: Env) -> None:
    """Sign the run's browser in to the portal and the ERP (what the worker did before the auditor starts)."""
    session = BrowserSession(ctx)
    ctx.extras["browser"] = session
    await session.start()
    for app, creds in (("portal", env.tenant.credentials["portal"]), ("erp", env.tenant.credentials["erp"])):
        page = session.page
        await page.goto(f"{env.base}/{app}/login")
        await page.fill("input[name=username]", creds["username"])
        await page.fill("input[name=password]", creds["password"])
        async with page.expect_navigation():
            await page.click("button[type=submit]")


async def test_the_auditor_reports_only_what_it_sees(sim_url, browser_pool, tmp_path):
    """Pass for a faithful bill, fail (with the reason) for one whose amount is wrong."""
    verdicts = {}
    for label, delta in (("right", 0.0), ("wrong", 1.0)):
        env = Env(sim_url, f"pol-audit-{label}", [], tmp_path / label)
        sim = env.sim_state()
        invoice = next(i for i in sim["invoices"] if i["number"].startswith("INI-20502"))
        created = httpx.post(f"{sim_url}/erp/api/bills", headers={"X-API-Key": env.tenant.credentials["erp_api_key"]}, json={
            "vendor": invoice["vendor"], "invoice_number": invoice["number"], "amount": round(invoice["amount"] + delta, 2),
            "currency": invoice["currency"], "issue_date": invoice["issue_date"], "due_date": invoice["due_date"]})
        assert created.status_code == 201, created.text
        runner = env.runner("audit", browser_pool)
        ctx = runner.ctx
        try:
            await login_both(ctx, env)
            criteria = ["Exactly one bill exists for the invoice.", "Vendor, number, amount and currency match the portal.",
                        "Issue and due dates match the portal."]
            claim = {"summary": "done", "outputs": {"bill_id": str(created.json()["id"]), "invoice_number": invoice["number"]}, "evidence": []}
            verdicts[label] = await Verifier(PolicyLLM(think_ms=0), runner.registry).verify(
                task="Enter invoice INI-20502", criteria=criteria, claim=claim, facts_text="(nothing)", ctx=ctx)
        finally:
            await close_browser(ctx)
    assert verdicts["right"].passed and len(verdicts["right"].checks) == 5  # 3 criteria + the 2 mandatory checks
    mandatory = {c["criterion"]: c for c in verdicts["right"].checks[-2:]}
    assert mandatory["Ambiguity"]["passed"] and "no search for other entities" in mandatory["Ambiguity"]["observed"]
    completeness = mandatory["Completeness"]["observed"]
    assert "the vendor portal invoice page" in completeness and "specific invoice" not in completeness
    wrong = verdicts["wrong"]
    assert not wrong.passed and not wrong.inconclusive
    failed = wrong.failed_checks
    assert [c["criterion"] for c in failed] == ["Vendor, number, amount and currency match the portal."]
    assert "Bill:" in failed[0]["observed"] and "Portal:" in failed[0]["observed"]
