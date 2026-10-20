"""Value provenance: the normalizer (dates, numbers, ids, free text) and the runner's check before a write."""
from __future__ import annotations

import itertools

import pytest

from worker.agent.broker import AutoBroker
from worker.agent.prompts import UNSOURCED_VALUES_TEMPLATE
from worker.agent.provenance import SourceIndex, describe, find_dates, find_unsourced, number_candidates, source_texts
from worker.agent.runner import AgentRunner
from worker.agent.state import Fact, RunState, Step
from worker.config import Settings
from worker.llm.base import LLMResponse, ToolCall, Usage
from worker.llm.fake import ScriptedLLM
from worker.tools.base import Risk, Tool, ToolResult
from worker.tools.control import FinishTool, UpdatePlanTool
from worker.tools.http import HttpRequestTool
from worker.tools.interaction import AskUserTool
from worker.tools.memory_tools import RememberTool
from worker.tools.registry import ToolRegistry

# ---------------------------------------------------------------- the normalizer


def unsourced(sources: list[str], **fields: str) -> dict[str, str]:
    """{label: shown value} of the fields that have no source in ``sources``."""
    rows = [{"label": label.replace("_", " "), "value": value} for label, value in fields.items()]
    return {i["label"]: i["value"] for i in find_unsourced(rows, SourceIndex(sources))}


@pytest.mark.parametrize("value", [
    "2026-11-12", "2026-11-12T10:00:00Z", "2026-11-12 10:00", "12 November 2026", "12 Nov 2026", "12th of November 2026",
    "November 12, 2026", "Nov 12 2026", "12/11/2026", "12.11.2026", "12-Nov-2026",
])
def test_a_date_is_sourced_whatever_its_format(value):
    for source in ("Due date: 12 November 2026", "due 2026-11-12", "Nov 12, 2026", "sent 12/11/2026 by mail"):
        assert unsourced([source], Due=value) == {}, (source, value)


def test_a_numeric_date_is_read_day_first_and_month_first():
    index = SourceIndex(["paid on 12/11/2026"])
    assert {(2026, 11, 12), (2026, 12, 11)} <= index.dates
    assert unsourced(["paid on 12/11/2026"], A="2026-11-12", B="2026-12-11", C="11/12/2026") == {}
    assert unsourced(["paid on 12/11/2026"], A="2026-11-13") == {"A": "2026-11-13"}
    assert find_dates("version 1.2.34 and 192.168.0.1") == []  # not dates


@pytest.mark.parametrize("value", ["2026-11-13", "13 November 2026", "13/11/2026", "2025-11-12", "2026-12-12T10:00"])
def test_a_different_date_is_not_sourced(value):
    assert unsourced(["Due date: 12 November 2026"], Due=value) == {"Due": value}


def test_the_evidence_case_is_flagged_with_its_labels():
    page = "Invoice CX-2026-0401\nIssue date 2026-09-08\nDue date 2026-10-08\nTotal 6,414.10 EUR"
    found = unsourced([page], Invoice_number="CX-2026-0401", Issue_date="2026-09-28", Due_date="2026-10-28",
                      Amount="6414.10")
    assert found == {"Issue date": "2026-09-28", "Due date": "2026-10-28"}
    assert describe(find_unsourced([{"label": k, "value": v} for k, v in found.items()], SourceIndex([page]))) == (
        "Issue date '2026-09-28', Due date '2026-10-28'")


@pytest.mark.parametrize("source,value", [
    ("Total 6,414.10 EUR", "6414.1"), ("Total 6,414.10 EUR", "6414.10"), ("Total 6,414.10 EUR", "6,414.10 EUR"),
    ("Total 6,414.10 EUR", "EUR 6414.10"), ("Total 6,414.10 EUR", "€6,414.10"), ("Total 6,414.10 EUR", "$ 6414.1"),
    ("Total 6,414.10 EUR", "6.414,10"), ("Amount 6.414,10 €", "6414.10"), ("Amount 6 414,10", "6,414.10"),
    ("Amount 6414.1", "6,414.10 EUR"), ("Net 1,234", "1234"), ("Net 1234", "1,234"), ("qty 12", "12.0"),
    ("Total 100", "100.00"), ("Total 100.00", "100"), ("Rate 7.5%", "7.50"),
])
def test_numbers_match_after_stripping_separators_currency_and_trailing_zeros(source, value):
    assert unsourced([source], Amount=value) == {}


@pytest.mark.parametrize("value", ["6414.2", "64141", "6,415.10 EUR", "641.41", "6414.11"])
def test_a_different_number_is_not_sourced(value):
    assert unsourced(["Total 6,414.10 EUR"], Amount=value) == {"Amount": value}


def test_number_candidates():
    assert number_candidates("6,414.10") == {"6414.1"}
    assert number_candidates("6.414,10") == {"6414.1"}
    assert number_candidates("1,234") == {"1234", "1.234"}
    assert number_candidates("1.234.567") == {"1234567"}
    assert number_candidates("0.500") == {"500", "0.5"}


def test_a_number_inside_a_date_is_not_a_source_for_a_loose_number():
    assert unsourced(["Due 2026-10-08"], Qty="10") == {"Qty": "10"}
    assert unsourced(["Due 2026-10-08"], Year="2026") == {}  # a year typed on its own is read from the date


def test_identifiers_are_compared_case_insensitively_on_word_boundaries():
    page = "Invoice cx-2026-0401 from Globex, PO 4471"
    assert unsourced([page], Number="CX-2026-0401", PO="po 4471", Other="CX-2026-0402") == {"Other": "CX-2026-0402"}
    assert unsourced(["ref CX-2026-04011"], Number="CX-2026-0401") == {"Number": "CX-2026-0401"}
    assert unsourced(["ref CX 2026 0401"], Number="CX-2026-0401") == {}  # the separators may differ


def test_values_without_a_digit_and_empty_values_are_never_checked():
    assert unsourced(["nothing"], Vendor="Globex Corporation", Urgent="checked", Notes="", Pass="••••",
                     Secret="[REDACTED:erp.key]") == {}


def test_free_text_is_checked_by_its_dates_amounts_and_ids():
    page = "Invoice CX-2026-0401 for 6,414.10 EUR is due 8 October 2026."
    body = "Hello,\nplease pay CX-2026-0401 (6.414,10 EUR) by 2026-10-08. 2 reminders so far, step 3.\nThanks"
    assert unsourced([page], Message=body) == {}
    wrong = body.replace("2026-10-08", "2026-10-28").replace("6.414,10", "6.514,10")
    assert unsourced([page], Message=wrong) == {"Message": "2026-10-28, 6.514,10"}
    assert unsourced([page], Message="Please pay CX-2026-0402 soon") == {"Message": "CX-2026-0402"}
    assert unsourced([page], Message="Small numbers like 2 and 17 are not data") == {}


def test_describe_caps_the_list_and_truncates_long_values():
    items = [{"label": f"F{i}", "value": "9" * 100} for i in range(10)]
    text = describe(items)
    assert text.count("F") == 8 and text.endswith("(+2 more)") and "..." in text


# ---------------------------------------------------------------- what counts as read


def test_sources_are_the_task_facts_observations_answers_and_approver_notes_but_not_our_own_echo():
    state = RunState(run_id="r", task="Record bill 7788 for Globex")
    state.facts["due"] = Fact(key="due", value="2026-10-08", source="portal")
    state.add_step(Step(tool="read_page", observation="Total 111.11", tool_call_id="a"))
    state.add_step(Step(kind="ask_user", tool="ask_user", observation="User answered: use cost centre 4455"))
    state.add_step(Step(kind="approval", tool="x", thought="please use 2031-01-01", observation="DENIED: x(9999)"))
    state.add_step(Step(tool="update_plan", observation="Plan: pay 5555"))
    state.add_step(Step(tool="save", ok=False, error="unsourced_values", observation="Not submitted: Due '2026-10-28'"))
    state.add_step(Step(tool="save", ok=False, error="denied", observation="User DENIED this action. Note: use 6666."))
    index = SourceIndex(source_texts(state))
    assert unsourced_by_index(index, A="7788", B="2026-10-08", C="111.11", D="4455", E="2031-01-01", F="6666") == {}
    assert unsourced_by_index(index, A="5555", B="2026-10-28", C="9999") == {"A": "5555", "B": "2026-10-28", "C": "9999"}


def unsourced_by_index(index: SourceIndex, **fields: str) -> dict[str, str]:
    return {i["label"]: i["value"] for i in find_unsourced([{"label": k, "value": v} for k, v in fields.items()], index)}


def test_what_the_agent_typed_and_the_page_echoes_back_is_not_a_source():
    state = RunState(run_id="r", task="Record the bill")
    state.add_step(Step(tool="read_page", observation='[2] textbox "Due date" value="2026-10-08"'))
    state.add_step(Step(tool="browser_type", args={"ref": 2, "text": "2026-10-28"},
                        observation='[2] textbox "Due date" value="2026-10-28"'))
    state.add_step(Step(tool="browser_type", args={"ref": 3, "text": "x" * 100},
                        observation='[3] textbox "Notes" value="' + "x" * 79 + '…"'))
    state.add_step(Step(tool="read_page", observation='[2] textbox "Due date" value="2026-10-28"\n[5] textbox "Ref" value="A-77"'))
    index = SourceIndex(source_texts(state))
    assert unsourced_by_index(index, Due="2026-10-08", Ref="A-77") == {}
    assert unsourced_by_index(index, Due="2026-10-28") == {"Due": "2026-10-28"}
    # a value the page showed before the agent typed the same text stays a source
    state = RunState(run_id="r", task="t")
    state.add_step(Step(tool="read_page", observation='[2] textbox "Due" value="2026-10-28"'))
    state.add_step(Step(tool="browser_type", args={"ref": 2, "text": "2026-10-28"}, observation='[2] textbox "Due" value="2026-10-28"'))
    assert unsourced_by_index(SourceIndex(source_texts(state)), Due="2026-10-28") == {}


# ---------------------------------------------------------------- the runner

_ids = itertools.count(1)
PAGE = "Invoice CX-2026-0401 Issue date 2026-09-08 Due date 2026-10-08 Total 6,414.10 EUR"
NOTE = ("Not submitted: these values do not appear in anything you have read in this run: "
        "Issue date '2026-09-28', Due date '2026-10-28'. Open the source document again, correct the fields, then submit.")


def call(name: str, **args) -> ToolCall:
    return ToolCall(id=f"c{next(_ids)}", name=name, args=args)


def reply(text: str = "", *calls: ToolCall) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=list(calls), usage=Usage(10, 5))


class PageTool(Tool):
    name = "read_page"
    description = "Read the portal page."
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        self.text = PAGE

    async def run(self, args, ctx):
        return ToolResult(True, self.text)


class TypeTool(Tool):
    """Named like the browser tool, so the runner treats its text as an echo; it shows the typed value like a snapshot."""

    name = "browser_type"
    description = "Type into a field."
    parameters = {"type": "object", "properties": {"ref": {"type": "integer"}, "text": {"type": "string"}}}
    read_only_safe = False

    async def run(self, args, ctx):
        return ToolResult(True, f'[{args.get("ref")}] textbox "Due date" value="{args["text"]}"')


class FormTool(Tool):
    """A write that submits the rows ``self.rows`` (as the approval card's "Values to be submitted")."""

    name = "save_bill"
    description = "Save the bill form."
    parameters = {"type": "object", "properties": {}}
    risk = Risk.WRITE
    read_only_safe = False

    def __init__(self):
        self.rows = [{"label": "Invoice number", "value": "CX-2026-0401"}, {"label": "Issue date", "value": "2026-09-28"},
                     {"label": "Due date", "value": "2026-10-28"}, {"label": "Vendor", "value": "Globex"}]
        self.saved: list[list[dict]] = []

    async def approval_preview(self, args, ctx):
        return list(self.rows)

    async def run(self, args, ctx):
        self.saved.append(list(self.rows))
        return ToolResult(True, "Saved bill R-1")


@pytest.fixture
def parts(tmp_path):
    (tmp_path / "ws").mkdir()
    settings = Settings(_env_file=None, RUNS_DIR=str(tmp_path / "runs"), WORKSPACE_DIR=str(tmp_path / "ws"),
                        MAX_STEPS=30, MAX_CONSECUTIVE_FAILURES=4, RUN_TIMEOUT_S=60, APPROVAL_MODE="confirm_writes")
    registry, form, page = ToolRegistry(), FormTool(), PageTool()
    for tool in (page, form, TypeTool(), HttpRequestTool(), RememberTool(), UpdatePlanTool(), FinishTool(), AskUserTool()):
        registry.register(tool)
    return settings, registry, form, page


class Run:
    def __init__(self, parts, script, broker=None, task="Record the open invoice in the ERP", **settings_update):
        settings, registry, self.form, self.page = parts
        self.events: list[tuple[str, dict]] = []
        self.broker = broker or AutoBroker.approve_all()
        self.llm = ScriptedLLM(list(script))
        self.runner = AgentRunner(task, self.llm, registry, settings.model_copy(update=settings_update), self.broker,
                                  run_id=f"p{next(_ids)}", event_sink=self._sink)

    async def _sink(self, event_type, payload):
        self.events.append((event_type, payload))

    async def go(self) -> RunState:
        return await self.runner.run()

    def of(self, event_type: str) -> list[dict]:
        return [p for t, p in self.events if t == event_type]

    def approvals(self) -> list[dict]:
        return [p for k, p in self.broker.requests if k == "approval"]


STOP = reply("stop", call("finish", status="blocked", summary="stop", evidence=[], outputs=[]))


async def test_an_unsourced_date_is_not_submitted_and_not_sent_for_approval(parts):
    seen: list[int] = []
    script = iter([reply("read", call("read_page")), reply("save", call("save_bill")), STOP])

    def respond(messages, tools):
        seen.append(run.runner.guards.to_dict()["failures"])
        return next(script)

    run = Run(parts, [])
    run.llm = ScriptedLLM(respond)
    run.runner.llm = run.llm
    state = await run.go()
    assert seen == [0, 0, 1]  # the blocked write counts toward the consecutive-failure guard
    blocked = next(s for s in state.steps if s.tool == "save_bill")
    assert not blocked.ok and blocked.error == "unsourced_values" and blocked.observation == NOTE
    assert blocked.observation == UNSOURCED_VALUES_TEMPLATE.format(values="Issue date '2026-09-28', Due date '2026-10-28'")
    assert run.form.saved == [] and run.approvals() == [] and not run.of("approval_requested")
    finished = [p for p in run.of("tool_finished") if p.get("tool") == "save_bill"][0]
    assert finished["error"] == "unsourced_values" and finished["ok"] is False
    assert NOTE in run.llm.calls[-1]["messages"][-1].content  # the agent is told, as the observation of its call
    assert [list(k) for k in sorted(run.runner._unsourced_blocked)] == [["due date", "2026-10-28"], ["issue date", "2026-09-28"]]
    assert sorted(run.runner._runtime()["unsourced_blocked"]) == [["due date", "2026-10-28"], ["issue date", "2026-09-28"]]


async def test_a_corrected_resubmit_passes(parts):
    form = parts[2]
    reads = iter([reply("read", call("read_page")), reply("save", call("save_bill"))])

    def script(messages, tools):
        try:
            return next(reads)
        except StopIteration:
            pass
        if not form.saved and form.rows[1]["value"] == "2026-09-28":
            form.rows[1]["value"], form.rows[2]["value"] = "2026-09-08", "2026-10-08"  # the agent re-read and corrected
            return reply("corrected", call("save_bill"))
        return STOP

    run = Run(parts, [])
    run.llm = ScriptedLLM(script)
    run.runner.llm = run.llm
    state = await run.go()
    saves = [s for s in state.steps if s.tool == "save_bill" and s.kind == "tool"]
    assert [s.error for s in saves] == ["unsourced_values", None]
    assert form.saved[0][1]["value"] == "2026-09-08" and len(run.approvals()) == 1  # the usual approval, no warning
    assert "unsourced_values" not in run.approvals()[0] and "warning" not in run.approvals()[0]["context"]


@pytest.mark.parametrize("mode", ["auto", "confirm_writes", "confirm_all"])
async def test_the_same_unsourced_values_a_second_time_go_to_a_human_even_in_auto_mode(parts, mode):
    broker = AutoBroker(lambda kind, p: {"approved": True, "remember": True})
    run = Run(parts, [reply("read", call("read_page")), reply("save", call("save_bill")),
                      reply("again", call("save_bill")), reply("and again", call("save_bill")), STOP],
              broker=broker, APPROVAL_MODE=mode)
    state = await run.go()
    saves = [s for s in state.steps if s.tool == "save_bill" and s.kind == "tool"]
    assert [s.error for s in saves] == ["unsourced_values", None, None]
    assert len(run.form.saved) == 2
    # approve-similar was ticked on the warned card, yet the next identical call is asked again
    cards = run.approvals()
    assert len(cards) == 2 and all(c["tool"] == "save_bill" for c in cards)
    card = cards[0]
    assert card["unsourced_values"] == [{"label": "Issue date", "value": "2026-09-28"},
                                        {"label": "Due date", "value": "2026-10-28"}]
    assert card["context"]["unsourced_values"] == card["unsourced_values"]
    assert "Issue date '2026-09-28', Due date '2026-10-28'" in card["context"]["warning"]
    assert card["context"]["form_values"][1] == {"label": "Issue date", "value": "2026-09-28"}  # the usual values table
    assert {"tool", "action", "args", "risk", "context"} <= set(card)  # the old payload shape is intact
    assert run.of("approval_requested")[0]["unsourced_values"] == card["unsourced_values"]
    assert run.runner.policy.to_dict()["approved_similar"] == []


async def test_a_new_unsourced_value_is_blocked_again_not_waved_through(parts):
    form = parts[2]
    script = iter([reply("read", call("read_page")), reply("save", call("save_bill")), reply("save", call("save_bill")),
                   STOP])

    def respond(messages, tools):
        item = next(script)
        if len(run.llm.calls) == 3:  # after the first block the agent changes one date to another invented one
            form.rows[2]["value"] = "2026-10-29"
        return item

    run = Run(parts, [])
    run.llm = ScriptedLLM(respond)
    run.runner.llm = run.llm
    state = await run.go()
    assert [s.error for s in state.steps if s.tool == "save_bill" and s.kind == "tool"] == ["unsourced_values", "unsourced_values"]
    assert form.saved == [] and run.approvals() == []


@pytest.mark.parametrize("rows", [
    [{"label": "Issue date", "value": "8 Sep 2026"}, {"label": "Due date", "value": "08/10/2026"},
     {"label": "Amount", "value": "6414.1"}, {"label": "Number", "value": "cx-2026-0401"}],
    [{"label": "Notes", "value": "Pay CX-2026-0401 (6.414,10 EUR) by 2026-10-08"}, {"label": "Vendor", "value": "Globex"},
     {"label": "Empty", "value": ""}],
])
async def test_sourced_values_pass_even_when_reformatted(parts, rows):
    run = Run(parts, [reply("read", call("read_page")), reply("save", call("save_bill")), STOP], APPROVAL_MODE="auto")
    run.form.rows = rows
    state = await run.go()
    assert [s.error for s in state.steps if s.tool == "save_bill" and s.kind == "tool"] == [None]
    assert len(run.form.saved) == 1 and run.approvals() == []  # auto mode: a sourced write needs no approval


async def test_the_task_remembered_facts_and_user_answers_are_sources(parts):
    run = Run(parts, [reply("ask", call("ask_user", question="Which issue date?")),
                      reply("remember", call("remember", key="due", value="2026-10-28", source="the colleague's mail")),
                      reply("save", call("save_bill")), STOP],
              broker=AutoBroker(lambda kind, p: {"answer": "It is 28 September 2026"} if kind == "question" else {"approved": True}),
              APPROVAL_MODE="auto")
    run.form.rows = [{"label": "Issue date", "value": "2026-09-28"}, {"label": "Due date", "value": "2026-10-28"}]
    state = await run.go()
    assert [s.error for s in state.steps if s.tool == "save_bill" and s.kind == "tool"] == [None]
    task_run = Run(parts, [reply("save", call("save_bill")), STOP], APPROVAL_MODE="auto",
                   task="Record the bill, issue date 2026-09-28 and due date 2026-10-28")
    task_run.form.rows, task_run.form.saved = run.form.rows, []
    assert [s.error for s in (await task_run.go()).steps if s.tool == "save_bill" and s.kind == "tool"] == [None]


async def test_a_value_the_agent_typed_itself_is_not_its_own_source(parts):
    run = Run(parts, [reply("read", call("read_page")), reply("type", call("browser_type", ref=3, text="2026-10-28")),
                      reply("save", call("save_bill")), STOP], APPROVAL_MODE="auto")
    run.form.rows = [{"label": "Due date", "value": "2026-10-28"}]
    state = await run.go()
    assert [s.error for s in state.steps if s.tool == "save_bill" and s.kind == "tool"] == ["unsourced_values"]
    assert run.form.saved == []


async def test_an_http_body_is_checked_and_a_read_is_not(parts):
    body = {"vendor": "Globex", "bill": {"number": "CX-2026-0401", "due": "2026-10-28", "lines": [{"amount": 6414.1}]}}
    run = Run(parts, [reply("read", call("read_page")),
                      reply("post", call("http_request", method="POST", url="http://localhost:9/bills", json_body=body)),
                      reply("get", call("http_request", method="GET", url="http://localhost:9/bills?due=2031-01-01")),
                      STOP], APPROVAL_MODE="auto")
    state = await run.go()
    post = next(s for s in state.steps if s.tool == "http_request" and s.args["method"] == "POST")
    assert post.error == "unsourced_values" and "bill.due '2026-10-28'" in post.observation
    assert "amount" not in post.observation and "number" not in post.observation
    get = next(s for s in state.steps if s.tool == "http_request" and s.args["method"] == "GET")
    assert get.error != "unsourced_values"  # a read submits nothing (it fails later: the host is not allowed)


async def test_the_check_can_be_switched_off(parts):
    run = Run(parts, [reply("read", call("read_page")), reply("save", call("save_bill")), STOP], APPROVAL_MODE="auto",
              PROVENANCE_CHECK=False)
    state = await run.go()
    assert [s.error for s in state.steps if s.tool == "save_bill" and s.kind == "tool"] == [None]
    assert len(run.form.saved) == 1 and run.approvals() == []
    assert Settings(_env_file=None).PROVENANCE_CHECK is True  # on by default


async def test_a_tool_without_values_and_a_failing_check_never_get_in_the_way(parts):
    run = Run(parts, [reply("read", call("read_page")), reply("save", call("save_bill")), STOP], APPROVAL_MODE="auto")
    run.form.rows = []
    state = await run.go()
    assert [s.error for s in state.steps if s.tool == "save_bill" and s.kind == "tool"] == [None]

    async def broken(args, ctx):
        raise RuntimeError("page closed")

    run = Run(parts, [reply("read", call("read_page")), reply("save", call("save_bill")), STOP], APPROVAL_MODE="auto")
    run.form.approval_preview = broken
    state = await run.go()
    assert [s.error for s in state.steps if s.tool == "save_bill" and s.kind == "tool"] == [None]


async def test_blocked_values_survive_a_resume(parts):
    run = Run(parts, [reply("read", call("read_page")), reply("save", call("save_bill")), STOP])
    await run.go()
    other = Run(parts, [])
    other.runner._restore(run.runner._runtime())
    assert other.runner._unsourced_blocked == run.runner._unsourced_blocked != set()


def test_tools_say_what_they_submit(tmp_path):
    from worker.tools.base import ToolContext
    from worker.tools.browser import TypeTool as BrowserTypeTool

    ctx = ToolContext(run_id="r", run_dir=tmp_path, workspace=tmp_path, settings=Settings(_env_file=None))
    typing = BrowserTypeTool()
    assert typing.submitted_values({"ref": 1, "text": "2026-10-28"}, ctx) == []  # typing alone submits nothing
    assert typing.submitted_values({"ref": 1, "text": "2026-10-28", "submit": True}, ctx) == [
        {"label": "Typed text", "value": "2026-10-28"}]
    http = HttpRequestTool()
    body = {"a": {"b": ["x", 2]}, "ok": True, "none": None}
    assert http.submitted_values({"method": "POST", "url": "u", "json_body": body, "query": {"q": "1"}}, ctx) == [
        {"label": "a.b[0]", "value": "x"}, {"label": "a.b[1]", "value": "2"}, {"label": "query.q", "value": "1"}]
    assert http.submitted_values({"method": "GET", "url": "u", "json_body": body}, ctx) == []
