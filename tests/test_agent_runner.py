"""End-to-end tests of the AgentRunner using a scripted LLM and simple fake tools (no network, no browser)."""
from __future__ import annotations

import asyncio
import itertools
import json

import pytest

from worker.agent.broker import AutoBroker, InteractionBroker
from worker.agent.runner import AgentRunner
from worker.config import Settings
from worker.llm.base import LLMError, LLMResponse, ToolCall, Usage
from worker.llm.fake import ScriptedLLM
from worker.tools.base import Risk, Tool, ToolResult
from worker.tools.control import FinishTool, UpdatePlanTool
from worker.tools.interaction import AskUserTool
from worker.tools.memory_tools import RememberTool
from worker.tools.registry import ToolRegistry

AUDITOR_PREFIX = "You are an independent auditor"
_ids = itertools.count(1)


# ---------------------------------------------------------------- scripting helpers


def call(name: str, **args) -> ToolCall:
    return ToolCall(id=f"call_{next(_ids)}", name=name, args=args)


def reply(text: str = "", *calls: ToolCall) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=list(calls), usage=Usage(10, 5))


def plan_call(*items: tuple[str, str]) -> ToolCall:
    return call(
        "update_plan",
        goal_summary="Goal",
        success_criteria=["The record is saved", "Nothing else changed"],
        items=[{"text": t, "status": s} for t, s in items],
    )


def finish_call(status: str = "success", summary: str = "All done.") -> ToolCall:
    return call(
        "finish",
        status=status,
        summary=summary,
        evidence=[{"claim": "record saved", "source": "save_record result"}],
        outputs=[{"name": "record_id", "value": "R-1"}],
    )


OK_CHECK = {"passed": True, "observed": "only one candidate, in the only source"}  # the mandatory verdict checks


def verdict(passed: bool, observed: str = "looked right") -> LLMResponse:
    return reply("", call("submit_verdict", passed=passed, summary="audit summary",
                          checks=[{"criterion": "The record is saved", "passed": passed, "observed": observed}],
                          ambiguity=OK_CHECK, completeness=OK_CHECK))


def make_llm(worker, auditor=()) -> ScriptedLLM:
    """Route calls by system prompt: worker turns vs. auditor turns. ``worker`` may be a list or a callable."""
    worker_queue, auditor_queue = list(worker) if not callable(worker) else worker, list(auditor)

    def responder(messages, tools):
        if messages[0].content.startswith(AUDITOR_PREFIX):
            return auditor_queue.pop(0)
        return worker_queue(messages, tools) if callable(worker_queue) else worker_queue.pop(0)

    return ScriptedLLM(responder)


def worker_calls(llm: ScriptedLLM) -> list[dict]:
    return [c for c in llm.calls if not c["messages"][0].content.startswith(AUDITOR_PREFIX)]


def auditor_calls(llm: ScriptedLLM) -> list[dict]:
    return [c for c in llm.calls if c["messages"][0].content.startswith(AUDITOR_PREFIX)]


def full_text(messages) -> str:
    return "\n".join(m.content for m in messages)


# ---------------------------------------------------------------- fake tools


class ReadNoteTool(Tool):
    name = "read_note"
    description = "Read a note (read-only)."
    parameters = {"type": "object", "properties": {"topic": {"type": "string"}}}

    async def run(self, args, ctx):
        return ToolResult(True, f"note about {args.get('topic', 'x')}: value 42")


class SaveRecordTool(Tool):
    name = "save_record"
    description = "Save a record (write)."
    parameters = {"type": "object", "properties": {"value": {"type": "string"}}}
    risk = Risk.WRITE
    read_only_safe = False

    def __init__(self):
        self.saved: list[dict] = []

    async def run(self, args, ctx):
        self.saved.append(args)
        return ToolResult(True, f"Saved record R-{len(self.saved)}")


class FlakyTool(Tool):
    """Always fails with a semantic (non-retryable) error."""

    name = "primary_path"
    description = "A path that is broken."
    parameters = {"type": "object", "properties": {"n": {"type": "integer"}}}
    read_only_safe = False

    async def run(self, args, ctx):
        return ToolResult(False, "Validation failed: Due date must be YYYY-MM-DD", "validation")


class AlternativeTool(Tool):
    name = "alternative_path"
    description = "A path that works."
    parameters = {"type": "object", "properties": {}}
    risk = Risk.WRITE
    read_only_safe = False

    async def run(self, args, ctx):
        return ToolResult(True, "Saved via alternative path")


class DeleteTool(Tool):
    name = "delete_thing"
    description = "Delete (irreversible)."
    parameters = {"type": "object", "properties": {}}
    risk = Risk.IRREVERSIBLE
    read_only_safe = False

    async def run(self, args, ctx):
        return ToolResult(True, "Deleted")


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "ws").mkdir()
    return Settings(
        _env_file=None, RUNS_DIR=str(tmp_path / "runs"), WORKSPACE_DIR=str(tmp_path / "ws"),
        MAX_STEPS=30, MAX_CONSECUTIVE_FAILURES=4, RUN_TIMEOUT_S=60, APPROVAL_MODE="confirm_writes",
    )


@pytest.fixture
def save_tool():
    return SaveRecordTool()


@pytest.fixture
def registry(save_tool):
    reg = ToolRegistry()
    for tool in (ReadNoteTool(), save_tool, FlakyTool(), AlternativeTool(), DeleteTool(),
                 RememberTool(), UpdatePlanTool(), FinishTool(), AskUserTool()):
        reg.register(tool)
    return reg


class Harness:
    """Runs a task and collects events."""

    def __init__(self, llm, registry, settings, broker=None, task="Do the task"):
        self.events: list[tuple[str, dict]] = []
        self.broker = broker or AutoBroker.approve_all()
        self.runner = AgentRunner(task, llm, registry, settings, self.broker, run_id="t1", event_sink=self._sink)

    async def _sink(self, event_type, payload):
        self.events.append((event_type, payload))

    async def run(self):
        return await self.runner.run()

    def types(self) -> list[str]:
        return [t for t, _ in self.events]

    def of(self, event_type: str) -> list[dict]:
        return [p for t, p in self.events if t == event_type]


# ---------------------------------------------------------------- tests


async def test_happy_path_plan_tool_remember_finish_verified(registry, settings, save_tool):
    llm = make_llm(
        worker=[
            reply("Plan it.", plan_call(("Read the note", "in_progress"), ("Save the record", "pending")),
                  call("read_note", topic="a")),
            reply("Remember and save.", call("remember", key="answer", value="42", source="read_note a"),
                  call("save_record", value="42")),
            reply("Verified in output.", finish_call()),
        ],
        auditor=[reply("Check.", call("read_note", topic="a")), verdict(True, "saw R-1")],
    )
    h = Harness(llm, registry, settings)
    state = await h.run()

    assert state.status == "succeeded"
    assert save_tool.saved == [{"value": "42"}]
    assert state.goal_summary == "Goal" and state.success_criteria == ["The record is saved", "Nothing else changed"]
    assert [p.text for p in state.plan] == ["Read the note", "Save the record"]
    assert state.facts["answer"].value == "42" and state.facts["answer"].source == "read_note a"
    assert state.facts["answer"].step > 0

    report = state.final_report
    assert report["status"] == "succeeded" and report["summary"] == "All done."
    assert report["outputs"] == {"record_id": "R-1"}
    assert report["evidence"][0]["claim"] == "record saved"
    assert report["verification"]["passed"] and report["verification"]["checks"][0]["observed"] == "saw R-1"
    assert report["stats"]["llm_calls"] == 5 and report["stats"]["approvals"] == 1 and report["stats"]["tool_failures"] == 0

    run_dir = settings.RUNS_DIR + "/t1"
    saved = json.loads(open(run_dir + "/run.json", encoding="utf-8").read())
    assert saved["status"] == "succeeded" and saved["facts"]["answer"]["value"] == "42"
    assert "Independent audit: **PASSED**" in open(run_dir + "/report.md", encoding="utf-8").read()

    types = h.types()
    for expected in ("run_started", "plan_updated", "thought", "tool_started", "tool_finished", "memory_updated",
                     "approval_requested", "approval_resolved", "verification_started", "verification_finished",
                     "run_finished", "usage"):
        assert expected in types, expected
    assert types[0] == "run_started" and types[-1] == "run_finished"
    assert all(p["run_id"] == "t1" for _, p in h.events)
    json.dumps([p for _, p in h.events], default=str)  # payloads are JSON-serializable


async def test_auditor_sees_only_read_only_tools_and_fresh_context(registry, settings):
    llm = make_llm(
        worker=[reply("Go.", call("read_note"), ), reply("Done.", finish_call())],
        auditor=[verdict(True)],
    )
    h = Harness(llm, registry, settings, task="Secret task text")
    h.runner.ctx.extras["read_only"] = "sentinel"
    await h.run()
    audit = auditor_calls(llm)[0]
    tool_names = {t.name for t in audit["tools"]}
    assert tool_names == {"read_note", "submit_verdict"}
    briefing = audit["messages"][1].content
    assert "Secret task text" in briefing and "WORKER'S CLAIM" in briefing
    assert "Plan updated" not in full_text(audit["messages"])  # worker transcript is not shared
    assert h.runner.ctx.extras["read_only"] == "sentinel"  # restored after verification


async def test_tool_failure_then_alternative_tool_then_success(registry, settings):
    llm = make_llm(
        worker=[
            reply("Try the main path.", call("primary_path", n=1)),
            reply("Failed on validation; use the other path.", call("alternative_path")),
            reply("Done.", finish_call()),
        ],
        auditor=[verdict(True)],
    )
    h = Harness(llm, registry, settings)
    state = await h.run()
    assert state.status == "succeeded"
    assert state.final_report["stats"]["tool_failures"] == 1
    second_turn = worker_calls(llm)[1]["messages"]
    assert "ERROR: " in second_turn[-1].content and "Due date must be YYYY-MM-DD" in second_turn[-1].content


async def test_write_requiring_approval_approved(registry, settings, save_tool):
    llm = make_llm(worker=[reply("Save it.", call("save_record", value="v")), reply("Done.", finish_call("blocked", "stop"))])
    h = Harness(llm, registry, settings, broker=AutoBroker.approve_all())
    await h.run()
    assert save_tool.saved == [{"value": "v"}]
    request = h.of("approval_requested")[0]
    assert request["tool"] == "save_record" and request["risk"] == "write" and request["args"] == {"value": "v"}
    assert request["action"].startswith("save_record(") and request["context"]["reason"] == "Save it."
    assert "request_id" in request


async def test_approval_context_carries_the_tool_preview_and_survives_a_broken_preview(registry, settings, save_tool):
    save_tool.approval_preview = _async_returning([{"label": "Issue date", "value": "2026-09-28"}])
    llm = make_llm(worker=[reply("Save it.", call("save_record", value="v")), reply("Done.", finish_call("blocked", "stop"))])
    h = Harness(llm, registry, settings, broker=AutoBroker.approve_all(),
                task="Save the bill with issue date 2026-09-28")
    await h.run()
    assert h.of("approval_requested")[0]["context"]["form_values"] == [{"label": "Issue date", "value": "2026-09-28"}]

    async def broken(args, ctx):
        raise RuntimeError("page closed")

    save_tool.approval_preview = broken
    llm = make_llm(worker=[reply("Save it.", call("save_record", value="v")), reply("Done.", finish_call("blocked", "stop"))])
    h = Harness(llm, registry, settings, broker=AutoBroker.approve_all(),
                task="Save the bill with issue date 2026-09-28")
    await h.run()
    request = h.of("approval_requested")[0]
    assert "form_values" not in request["context"] and request["context"]["reason"] == "Save it."
    assert len(save_tool.saved) == 2  # the approval still went through


def _async_returning(value):
    async def preview(args, ctx):
        return value

    return preview


async def test_write_denied_agent_gets_denial_observation_and_tool_is_not_run(registry, settings, save_tool):
    broker = AutoBroker(lambda kind, p: {"approved": False, "note": "amount looks wrong"})
    llm = make_llm(worker=[reply("Save it.", call("save_record", value="v")),
                           reply("Denied; I stop.", finish_call("blocked", "User denied the write."))])
    h = Harness(llm, registry, settings, broker=broker)
    state = await h.run()
    assert save_tool.saved == []
    next_messages = worker_calls(llm)[1]["messages"]
    assert "User DENIED this action. Note: amount looks wrong. Do not retry the same action; adapt or ask the user." in next_messages[-1].content
    assert state.status == "needs_attention"  # blocked finish is not verified and not a success
    assert state.final_report["verification"]["ran"] is False
    assert state.final_report["stats"]["denials"] == 1
    assert not auditor_calls(llm)


async def test_approve_similar_asks_once_for_repeated_writes(registry, settings, save_tool):
    broker = AutoBroker(lambda kind, p: {"approved": True, "remember": True})
    llm = make_llm(worker=[reply("1", call("save_record", value="a")), reply("2", call("save_record", value="b")),
                           reply("3", finish_call("failed", "enough"))])
    await Harness(llm, registry, settings, broker=broker).run()
    assert len(save_tool.saved) == 2 and len(broker.requests) == 1


async def test_irreversible_asked_even_in_auto_mode(registry, settings):
    settings = settings.model_copy(update={"APPROVAL_MODE": "auto"})
    broker = AutoBroker(lambda kind, p: {"approved": False})
    llm = make_llm(worker=[reply("Save", call("save_record", value="a")), reply("Delete", call("delete_thing")),
                           reply("end", finish_call("failed", "x"))])
    await Harness(llm, registry, settings, broker=broker).run()
    assert [p["tool"] for k, p in broker.requests if k == "approval"] == ["delete_thing"]


async def test_ask_user_routed_through_broker(registry, settings):
    broker = AutoBroker(lambda kind, p: {"answer": "Holdings"})
    llm = make_llm(worker=[reply("Two matches.", call("ask_user", question="Which vendor?", options=["Ltd", "Holdings"])),
                           reply("Thanks.", finish_call("blocked", "done asking"))])
    h = Harness(llm, registry, settings, broker=broker)
    await h.run()
    assert broker.requests == [("question", {"question": "Which vendor?", "options": ["Ltd", "Holdings"]})]
    assert "User answered: Holdings" in worker_calls(llm)[1]["messages"][-1].content
    assert h.of("question_asked")[0]["question"] == "Which vendor?"
    assert h.of("question_answered")[0]["answered"] is True
    statuses_during = [s.kind for s in h.runner.state.steps]
    assert "ask_user" in statuses_during


async def test_verifier_failure_feeds_back_and_worker_fixes(registry, settings, save_tool):
    llm = make_llm(
        worker=[
            reply("Save wrong.", call("save_record", value="wrong")),
            reply("Claim done.", finish_call()),
            reply("Fix per feedback.", call("save_record", value="right")),
            reply("Claim done again.", finish_call()),
        ],
        auditor=[verdict(False, "amount was wrong"), verdict(True, "now right")],
    )
    h = Harness(llm, registry, settings)
    state = await h.run()
    assert state.status == "succeeded"
    assert [s["value"] for s in save_tool.saved] == ["wrong", "right"]
    after_fail = worker_calls(llm)[2]["messages"]
    assert "Independent verification FAILED" in after_fail[-1].content
    assert "amount was wrong" in after_fail[-1].content
    assert "VERIFIER FEEDBACK (unresolved)" in after_fail[-1].content
    assert [p["passed"] for p in h.of("verification_finished")] == [False, True]
    assert state.final_report["stats"]["tool_failures"] == 0  # a rejected finish is not a tool malfunction


async def test_verification_failing_repeatedly_ends_failed_honestly(registry, settings):
    llm = make_llm(worker=lambda m, t: reply("Claim.", finish_call()), auditor=[verdict(False, "still wrong")] * 3)
    state = await Harness(llm, registry, settings).run()
    assert state.status == "failed"
    assert len(auditor_calls(llm)) == 3
    report = state.final_report
    assert report["verification"]["passed"] is False and "still wrong" in report["verification"]["checks"][0]["observed"]
    assert "Independent verification failed" in report["summary"]


async def test_inconclusive_verification_needs_attention(registry, settings):
    def auditor_fails(messages, tools):
        raise LLMError("auditor down")

    def responder(messages, tools):
        if messages[0].content.startswith(AUDITOR_PREFIX):
            return auditor_fails(messages, tools)
        return reply("Claim.", finish_call())

    state = await Harness(ScriptedLLM(responder), registry, settings).run()
    assert state.status == "needs_attention" and state.final_report["verification"]["inconclusive"]


async def test_repetition_guard_nudges_then_escalates_to_user(registry, settings):
    broker = AutoBroker(lambda kind, p: {"answer": "stop"})
    llm = make_llm(worker=lambda m, t: reply("Again.", call("read_note", topic="same")))
    h = Harness(llm, registry, settings, broker=broker)
    state = await h.run()
    guard_events = h.of("guard_triggered")
    assert [(g["guard"], g["level"]) for g in guard_events] == [("repetition", "nudge"), ("repetition", "escalate")]
    nudged_call = worker_calls(llm)[3]["messages"]  # the call right after the 3rd identical action
    assert "repeated the same action" in nudged_call[-1].content
    assert broker.requests[0][0] == "question" and "stuck" in broker.requests[0][1]["question"]
    assert state.status == "needs_attention"


async def test_consecutive_failures_nudge_then_escalate_and_user_guidance_resumes(registry, settings):
    answers = iter(["Try the alternative path", "unused"])
    broker = AutoBroker(lambda kind, p: {"answer": next(answers)})
    counter = itertools.count()

    def worker(messages, tools):
        n = next(counter)
        if n < 4:
            return reply("fail", call("primary_path", n=n))  # distinct args so only the failure guard fires
        if n == 4:
            return reply("Use alternative.", call("alternative_path"))
        return reply("Done.", finish_call("blocked", "gave up after help"))

    llm = make_llm(worker)
    h = Harness(llm, registry, settings, broker=broker)
    state = await h.run()
    levels = [(g["guard"], g["level"]) for g in h.of("guard_triggered")]
    assert levels == [("failures", "nudge"), ("failures", "escalate")]
    assert "consecutive failures" in worker_calls(llm)[2]["messages"][-1].content  # nudge after 2nd failure
    assert "Try the alternative path" in worker_calls(llm)[4]["messages"][-1].content  # guidance injected
    assert state.status == "needs_attention"  # the scripted worker finished "blocked"


async def test_escalation_without_answer_ends_needs_attention(registry, settings):
    llm = make_llm(worker=lambda m, t: reply("fail", call("primary_path", n=next(_ids))))
    state = await Harness(llm, registry, settings, broker=AutoBroker(lambda k, p: None)).run()
    assert state.status == "needs_attention" and "No guidance" in state.final_report["summary"]


async def test_step_budget_ends_needs_attention_with_partial_report(registry, settings):
    settings = settings.model_copy(update={"MAX_STEPS": 3})
    llm = make_llm(worker=lambda m, t: reply("more", call("read_note", topic=str(next(_ids)))))
    h = Harness(llm, registry, settings)
    state = await h.run()
    assert state.status == "needs_attention"
    assert "Step budget" in state.final_report["summary"]
    assert state.final_report["stats"]["steps"] == 3
    assert h.of("guard_triggered")[-1]["guard"] == "budget"


async def test_timeout_guard(registry, settings):
    settings = settings.model_copy(update={"RUN_TIMEOUT_S": -1})
    state = await Harness(make_llm(worker=[]), registry, settings).run()
    assert state.status == "needs_attention" and "time limit" in state.final_report["summary"]


async def test_text_only_reply_is_nudged_and_forces_a_tool_call(registry, settings):
    llm = make_llm(worker=[reply("I think I should look at it."), reply("OK.", finish_call("failed", "x"))])
    await Harness(llm, registry, settings).run()
    first, second = worker_calls(llm)
    assert first["tool_choice"] == "auto" and second["tool_choice"] == "any"
    assert "contained no tool call" in second["messages"][-1].content


async def test_two_text_only_replies_in_a_row_end_needs_attention_with_the_text(registry, settings):
    llm = make_llm(worker=[reply("The answer is 42."), reply("The answer is 42, really.")])
    state = await Harness(llm, registry, settings).run()
    assert state.status == "needs_attention"
    assert "The answer is 42, really." in state.final_report["summary"]


async def test_failed_finish_ends_failed_without_verification(registry, settings):
    llm = make_llm(worker=[reply("Cannot.", finish_call("failed", "The portal is down."))])
    state = await Harness(llm, registry, settings).run()
    assert state.status == "failed" and "The portal is down." in state.final_report["summary"]
    assert not auditor_calls(llm)


async def test_llm_failure_ends_needs_attention_with_error(registry, settings):
    def boom(messages, tools):
        raise LLMError("quota exhausted")

    state = await Harness(ScriptedLLM(boom), registry, settings).run()
    assert state.status == "needs_attention" and "quota exhausted" in state.final_report["summary"]


async def test_finish_must_be_alone_and_unknown_tools_are_reported(registry, settings):
    llm = make_llm(worker=[
        reply("Rush.", call("read_note"), finish_call()),
        reply("Oops.", call("teleport")),
        reply("Fine.", finish_call("failed", "x")),
    ])
    state = await Harness(llm, registry, settings).run()
    msgs = worker_calls(llm)
    assert "must be called alone" in msgs[1]["messages"][-1].content
    assert "Unknown tool 'teleport'" in msgs[2]["messages"][-1].content
    assert state.status == "failed"


async def test_calls_after_a_failure_in_the_same_turn_are_skipped(registry, settings, save_tool):
    llm = make_llm(worker=[reply("Two at once.", call("primary_path", n=1), call("save_record", value="x")),
                           reply("end", finish_call("failed", "x"))])
    await Harness(llm, registry, settings).run()
    assert save_tool.saved == []
    messages = worker_calls(llm)[1]["messages"]
    assert [m.role for m in messages[-3:]] == ["assistant", "tool", "tool"]  # pairing preserved
    assert "Skipped" in messages[-1].content


async def test_cancel_while_waiting_for_user_ends_cancelled(registry, settings):
    broker = InteractionBroker()  # nobody ever answers
    llm = make_llm(worker=[reply("Ask.", call("ask_user", question="Which one?"))])
    h = Harness(llm, registry, settings, broker=broker)
    original = h._sink

    async def cancel_on_question(event_type, payload):
        await original(event_type, payload)
        if event_type == "question_asked":
            h.runner.cancel()

    h.runner._sink = cancel_on_question
    state = await asyncio.wait_for(h.run(), timeout=5)
    assert state.status == "cancelled"
    assert broker.pending() == []


async def test_failing_event_sink_does_not_break_the_run(registry, settings):
    async def bad_sink(event_type, payload):
        raise RuntimeError("ui crashed")

    llm = make_llm(worker=[reply("end", finish_call("failed", "x"))])
    runner = AgentRunner("t", llm, registry, settings, AutoBroker.approve_all(), run_id="t2", event_sink=bad_sink)
    assert (await runner.run()).status == "failed"


# ---------------------------------------------------------------- model failover and thought summaries


async def test_all_models_out_of_quota_ends_needs_attention_with_clear_message(registry, settings):
    from worker.llm.base import QuotaExhaustedError
    from worker.llm.failover import FailoverLLM, ModelHealth
    from worker.llm.resilient import ResilientLLM

    class Dead(ScriptedLLM):
        async def complete(self, messages, tools=None, **kwargs):
            raise QuotaExhaustedError("20/day")

    llm = ResilientLLM(FailoverLLM(["a", "b"], lambda m: Dead([]), health=ModelHealth()), min_interval_s=0.0)
    state = await Harness(llm, registry, settings).run()
    assert state.status == "needs_attention"
    summary = state.final_report["summary"]
    assert "All configured Gemini models are out of free-tier quota for today" in summary
    assert "GEMINI_MODELS" in summary and "tomorrow" in summary


async def test_thought_summary_fills_empty_thought_and_is_not_echoed_back(registry, settings):
    summary_reply = LLMResponse(text="", tool_calls=[call("read_note", topic="a")], usage=Usage(10, 5),
                                thought="Checking the note.\n" + "very long reasoning " * 40)
    llm = make_llm(worker=[summary_reply, reply("", finish_call("blocked", "stop"))])
    harness = Harness(llm, registry, settings)
    state = await harness.run()
    step = next(s for s in state.steps if s.tool == "read_note")
    assert step.thought.startswith("Checking the note.") and step.thought_is_summary and len(step.thought) <= 301
    assert harness.of("thought")[0]["text"] == step.thought
    second_turn = worker_calls(llm)[1]["messages"]
    assert all(not m.content.startswith("Checking") for m in second_turn if m.role == "assistant")


async def test_gemini_style_summary_is_condensed_for_the_dashboard(registry, settings):
    summary = ("**Reading the note**\n\n"
               "Okay, here's my take on those thoughts, tailored for an expert audience: "
               "Alright, let me see... I need the note about topic a. Then I finish.")
    llm = make_llm(worker=[LLMResponse(text="", tool_calls=[call("read_note", topic="a")], usage=Usage(10, 5), thought=summary),
                           reply("", finish_call("blocked", "stop"))])
    harness = Harness(llm, registry, settings)
    await harness.run()
    assert harness.of("thought")[0]["text"] == "Reading the note: I need the note about topic a; then I finish."


async def test_network_wait_emits_llm_waiting_and_cancel_interrupts_it(registry, settings):
    from worker.llm.base import TransientLLMError
    from worker.llm.resilient import ResilientLLM

    class Offline(ScriptedLLM):
        async def complete(self, messages, tools=None, **kwargs):
            raise TransientLLMError("getaddrinfo failed", network=True)

    async def instant(_s):
        await asyncio.sleep(0)  # yield to the event loop like a real sleep

    llm = ResilientLLM(Offline([]), min_interval_s=0.0, sleep=instant)
    harness = Harness(llm, registry, settings)
    first = asyncio.ensure_future(harness.run())
    for _ in range(50):
        await asyncio.sleep(0)
        if harness.of("llm_waiting"):
            break
    harness.runner.cancel()
    state = await asyncio.wait_for(first, 5)
    waiting = harness.of("llm_waiting")
    assert waiting and "Network unavailable, retrying the language model for up to 5 min" in waiting[0]["message"]
    assert state.status == "cancelled"


async def test_usage_event_reports_active_model(registry, settings):
    class ModelLLM(ScriptedLLM):
        active_model, model_switches = "gemini-3.6-flash", 1

    llm = ModelLLM(lambda messages, tools: reply("", finish_call("blocked", "stop")))
    harness = Harness(llm, registry, settings)
    state = await harness.run()
    assert harness.of("usage")[0]["model"] == "gemini-3.6-flash" and harness.of("usage")[0]["model_switches"] == 1
    assert state.final_report["stats"]["model"] == "gemini-3.6-flash"


# ---------------------------------------------------------------- malformed tool calls from the model


async def test_malformed_call_does_not_end_the_run_the_next_turn_gets_a_corrective_note(registry, settings):
    from worker.llm.base import MalformedCallError

    queue = [MalformedCallError("Gemini produced a malformed function call"), reply("Fine now.", finish_call("failed", "x"))]

    def responder(messages, tools):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    llm = ScriptedLLM(responder)
    h = Harness(llm, registry, settings)
    state = await h.run()
    first, second = llm.calls
    assert "malformed tool call and was discarded" in second["messages"][-1].content
    assert "malformed tool call" not in first["messages"][-1].content
    assert state.status == "failed"  # the scripted finish decided it, not the glitch
    assert [s.error for s in state.steps if s.kind == "system"] == ["malformed_call"]
    assert h.of("llm_malformed_call")


async def test_repeated_malformed_calls_trip_the_failure_guard_with_a_clear_reason(registry, settings):
    from worker.llm.base import MalformedCallError

    def always_malformed(messages, tools):
        raise MalformedCallError("Gemini produced a malformed function call")

    llm = ScriptedLLM(always_malformed)
    h = Harness(llm, registry, settings)
    state = await h.run()
    assert state.status == "needs_attention"
    summary = state.final_report["summary"]
    assert "malformed tool calls" in summary and "could not be reached" not in summary
    assert len(llm.calls) == settings.MAX_CONSECUTIVE_FAILURES  # bounded by the existing consecutive-failure guard
    assert h.of("guard_triggered")[-1]["guard"] == "failures"


async def test_a_good_turn_resets_the_malformed_streak(registry, settings):
    from worker.llm.base import MalformedCallError

    queue = [MalformedCallError(), MalformedCallError(), MalformedCallError(), reply("ok", call("read_note", topic="a")),
             MalformedCallError(), MalformedCallError(), MalformedCallError(), reply("end", finish_call("failed", "x"))]

    def responder(messages, tools):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    state = await Harness(ScriptedLLM(responder), registry, settings).run()
    assert state.status == "failed" and not queue  # 3 + 3 glitches never reach the limit of 4 in a row


# ---------------------------------------------------------------- mandatory verifier checks


def raw_verdict(**fields) -> LLMResponse:
    base = {"passed": True, "summary": "audit summary",
            "checks": [{"criterion": "The record is saved", "passed": True, "observed": "saw it"}]}
    return reply("", call("submit_verdict", **{**base, **fields}))


async def test_verdict_without_the_mandatory_checks_is_sent_back_once_then_inconclusive(registry, settings):
    llm = make_llm(worker=[reply("Done.", finish_call())], auditor=[raw_verdict(), raw_verdict()])
    state = await Harness(llm, registry, settings).run()
    audits = auditor_calls(llm)
    assert len(audits) == 2
    reprompt = audits[1]["messages"][-1]
    assert reprompt.role == "tool" and "ambiguity" in reprompt.content and "completeness" in reprompt.content
    assert audits[1]["tool_choice"] == "auto" and [t.name for t in audits[1]["tools"]][-1] == "submit_verdict"
    assert state.status == "needs_attention"
    verification = state.final_report["verification"]
    assert verification["inconclusive"] and not verification["passed"] and "ambiguity" in verification["summary"]


async def test_verdict_that_adds_the_missing_checks_after_the_reprompt_is_accepted(registry, settings):
    llm = make_llm(worker=[reply("Done.", finish_call())],
                   auditor=[raw_verdict(), raw_verdict(ambiguity=OK_CHECK, completeness=OK_CHECK)])
    state = await Harness(llm, registry, settings).run()
    assert state.status == "succeeded" and len(auditor_calls(llm)) == 2
    assert [c["criterion"] for c in state.final_report["verification"]["checks"]] == [
        "The record is saved", "Ambiguity", "Completeness"]


async def test_incomplete_mandatory_check_counts_as_missing(registry, settings):
    empty = {"passed": True, "observed": "  "}
    llm = make_llm(worker=[reply("Done.", finish_call())],
                   auditor=[raw_verdict(ambiguity=empty, completeness=OK_CHECK)] * 2)
    state = await Harness(llm, registry, settings).run()
    assert state.final_report["verification"]["inconclusive"]


async def test_failed_ambiguity_check_fails_the_verification_even_when_every_criterion_passed(registry, settings):
    competing = {"passed": False, "observed": "Company X Holdings also has an invoice dated the same day; nobody was asked"}
    llm = make_llm(worker=lambda m, t: reply("Claim.", finish_call()),
                   auditor=[raw_verdict(ambiguity=competing, completeness=OK_CHECK)] * 3)
    h = Harness(llm, registry, settings)
    state = await h.run()
    assert state.status == "failed"
    checks = h.of("verification_finished")[0]["checks"]
    assert [(c["criterion"], c["passed"]) for c in checks] == [
        ("The record is saved", True), ("Ambiguity", False), ("Completeness", True)]
    assert h.of("verification_finished")[0]["passed"] is False
    feedback = worker_calls(llm)[1]["messages"][-1].content
    assert "Ambiguity" in feedback and "Company X Holdings" in feedback


# ---------------------------------------------------------------- structured ambiguity evidence (decided by code)


def entity(name: str, checked: bool = True, candidate: str = "") -> dict:
    return {"name": name, "checked": checked, "candidate": candidate}


def ambiguity_of(*entities: dict, observed: str = "looked at the vendor list", **extra) -> dict:
    return {"matching_entities": list(entities), "observed": observed, **extra}


async def audit_ambiguity(registry, settings, *ambiguities, answered=False):
    """Run one task whose auditor submits the given ambiguity checks in turn; returns (state, llm, harness)."""
    asks = [reply("Two matches.", call("ask_user", question="Which vendor?", options=["Ltd", "Holdings"]))] if answered else []
    worker = lambda m, t: asks.pop(0) if asks else reply("Done.", finish_call())  # noqa: E731
    llm = make_llm(worker=worker, auditor=[raw_verdict(ambiguity=a, completeness=OK_CHECK) for a in ambiguities])
    h = Harness(llm, registry, settings, broker=AutoBroker(lambda kind, p: {"answer": "Holdings"}))
    return await h.run(), llm, h


def ambiguity_result(state) -> dict:
    return next(c for c in state.final_report["verification"]["checks"] if c["criterion"] == "Ambiguity")


async def test_fewer_than_two_matching_entities_pass_and_keep_the_auditors_text(registry, settings):
    for entities in ([], [entity("Company X Ltd", True, "CX-1")]):
        state, llm, _ = await audit_ambiguity(registry, settings, ambiguity_of(*entities, observed="only Ltd matches", passed=False))
        assert state.status == "succeeded" and len(auditor_calls(llm)) == 1  # the model's own passed=false is not used
        assert ambiguity_result(state) == {"criterion": "Ambiguity", "passed": True, "observed": "only Ltd matches"}


async def test_two_entities_with_candidates_and_no_question_fail_and_name_the_candidates(registry, settings):
    ambiguity = ambiguity_of(entity("Company X Ltd", True, "CX-2026-0412"), entity("Company X Holdings", True, "CXH-77"), passed=True)
    state, llm, h = await audit_ambiguity(registry, settings, *[ambiguity] * 3)
    check = ambiguity_result(state)
    assert not check["passed"] and state.status == "failed"
    assert "Company X Ltd (candidate: CX-2026-0412)" in check["observed"] and "Company X Holdings (candidate: CXH-77)" in check["observed"]
    assert "never asked" in check["observed"]
    assert "Company X Holdings" in worker_calls(llm)[1]["messages"][-1].content  # the reason reaches the worker
    assert [c["passed"] for c in h.of("verification_finished")[0]["checks"]] == [True, False, True]


async def test_two_entities_with_candidates_pass_when_the_worker_asked_and_got_an_answer(registry, settings):
    ambiguity = ambiguity_of(entity("Company X Ltd", True, "CX-2026-0412"), entity("Company X Holdings", True, "CXH-77"), passed=False)
    state, _, _ = await audit_ambiguity(registry, settings, ambiguity, answered=True)
    check = ambiguity_result(state)
    assert state.status == "succeeded" and check["passed"] and "asked the user" in check["observed"]
    assert "Company X Holdings (candidate: CXH-77)" in check["observed"]


async def test_a_question_without_an_answer_does_not_settle_the_ambiguity(registry, settings, tmp_path):
    from worker.agent.verifier import Verifier
    from worker.tools.base import ToolContext

    ambiguity = ambiguity_of(entity("A", True, "a-1"), entity("B", True, "b-1"))
    ctx = ToolContext(run_id="v1", run_dir=tmp_path, workspace=tmp_path, settings=settings)
    outcomes = {}
    for label, answer in (("unanswered", None), ("answered", "B")):
        llm = make_llm(worker=[], auditor=[raw_verdict(ambiguity=ambiguity, completeness=OK_CHECK)])
        outcomes[label] = await Verifier(llm, registry).verify(
            task="t", criteria=["The record is saved"], claim={}, facts_text="", ctx=ctx,
            questions=[{"question": "Which?", "options": [], "answer": answer}])
    assert [c["passed"] for c in outcomes["unanswered"].checks] == [True, False, True]
    assert all(c["passed"] for c in outcomes["answered"].checks)


async def test_only_one_entity_with_a_candidate_passes(registry, settings):
    ambiguity = ambiguity_of(entity("Company X Ltd", True, "CX-2026-0412"), entity("Company X Holdings", True, ""), entity("Company X GmbH", True, "none"))
    state, _, _ = await audit_ambiguity(registry, settings, ambiguity)
    check = ambiguity_result(state)
    assert state.status == "succeeded" and check["passed"] and "Only one has a candidate" in check["observed"]
    assert "Company X Holdings (no candidate)" in check["observed"] and "Company X GmbH (no candidate)" in check["observed"]


async def test_unchecked_entities_are_sent_back_once_and_the_checked_answer_is_used(registry, settings):
    first = ambiguity_of(entity("Company X Ltd", True, "CX-1"), entity("Company X Holdings", False, ""), passed=True)
    second = ambiguity_of(entity("Company X Ltd", True, "CX-1"), entity("Company X Holdings", True, ""))
    state, llm, _ = await audit_ambiguity(registry, settings, first, second)
    audits = auditor_calls(llm)
    assert len(audits) == 2 and state.status == "succeeded"
    reprompt = audits[1]["messages"][-1]
    assert reprompt.role == "tool" and "Company X Holdings" in reprompt.content and "Company X Ltd" not in reprompt.content
    assert audits[1]["tool_choice"] == "auto" and [t.name for t in audits[1]["tools"]][-1] == "submit_verdict"
    assert ambiguity_result(state)["passed"]


async def test_entities_still_unchecked_after_the_one_reprompt_fail_the_check_not_the_audit(registry, settings):
    unchecked = ambiguity_of(entity("Company X Ltd", True, "CX-1"), entity("Company X Holdings", False, ""), passed=True)
    state, llm, h = await audit_ambiguity(registry, settings, *[unchecked] * 8)
    audits = auditor_calls(llm)
    assert len(audits[1]["messages"]) > 2 and len(audits[2]["messages"]) == 2  # round 1 = verdict + one re-prompt, then a new audit
    verification = h.of("verification_finished")[0]
    assert not verification["inconclusive"] and not verification["passed"]
    check = next(c for c in verification["checks"] if c["criterion"] == "Ambiguity")
    assert not check["passed"] and "Company X Holdings" in check["observed"] and "did not look" in check["observed"]
    assert [c["passed"] for c in verification["checks"]] == [True, False, True]


async def test_the_unchecked_reprompt_is_sent_once_per_audit_round(registry, settings):
    unchecked = ambiguity_of(entity("A", False, ""), entity("B", True, ""))
    llm = make_llm(worker=lambda m, t: reply("Claim.", finish_call()), auditor=[raw_verdict(ambiguity=unchecked, completeness=OK_CHECK)] * 8)
    await Harness(llm, registry, settings).run()
    first_round = auditor_calls(llm)[:2]  # one verdict, one re-prompted verdict, then the verification result ends the round
    assert "you listed matching entities you did not check" in first_round[1]["messages"][-1].content
    assert sum("you listed matching entities you did not check" in m.content for m in first_round[1]["messages"]) == 1


async def test_structured_ambiguity_with_an_empty_observed_counts_as_missing(registry, settings):
    state, llm, _ = await audit_ambiguity(registry, settings, ambiguity_of(entity("A"), observed="  "), ambiguity_of(entity("A"), observed="  "))
    assert state.final_report["verification"]["inconclusive"] and len(auditor_calls(llm)) == 2


async def test_failed_completeness_check_fails_the_verification(registry, settings):
    missed = {"passed": False, "observed": "The mailbox holds a newer invoice the worker never looked at"}
    llm = make_llm(worker=lambda m, t: reply("Claim.", finish_call()),
                   auditor=[raw_verdict(ambiguity=OK_CHECK, completeness=missed)] * 3)
    state = await Harness(llm, registry, settings).run()
    assert state.status == "failed"
    failed = [c for c in state.final_report["verification"]["checks"] if not c["passed"]]
    assert [c["criterion"] for c in failed] == ["Completeness"]


async def test_the_workers_questions_and_answers_and_the_sources_reach_the_verifier(registry, settings):
    broker = AutoBroker(lambda kind, p: {"answer": "Holdings"})
    llm = make_llm(
        worker=[reply("Two matches.", call("ask_user", question="Which vendor?", options=["Ltd", "Holdings"])),
                reply("Done.", finish_call())],
        auditor=[verdict(True)],
    )
    await Harness(llm, registry, settings, broker=broker).run()
    briefing = auditor_calls(llm)[0]["messages"][1].content
    assert "Which vendor?" in briefing and "Ltd, Holdings" in briefing and "A: Holdings" in briefing
    assert "SYSTEMS AND SOURCES AVAILABLE" in briefing and "start page" in briefing and "read_note" in briefing


async def test_the_workers_evidence_sources_are_listed_for_the_verifier(registry, settings):
    llm = make_llm(worker=[reply("Done.", finish_call())], auditor=[verdict(True)])
    await Harness(llm, registry, settings).run()
    briefing = auditor_calls(llm)[0]["messages"][1].content
    assert "WHERE THE WORKER SAYS THE RESULT IS" in briefing
    assert "- record saved (source: save_record result)" in briefing.split("WHERE THE WORKER SAYS THE RESULT IS")[1]


async def test_the_verifier_is_told_when_the_worker_asked_nothing(registry, settings):
    llm = make_llm(worker=[reply("Done.", finish_call())], auditor=[verdict(True)])
    await Harness(llm, registry, settings).run()
    assert "the worker never asked the user anything" in auditor_calls(llm)[0]["messages"][1].content


async def test_auditor_malformed_replies_are_retried_then_end_inconclusive(registry, settings):
    from worker.llm.base import MalformedCallError

    audits = {"n": 0}

    def responder(messages, tools):
        if messages[0].content.startswith(AUDITOR_PREFIX):
            audits["n"] += 1
            if audits["n"] == 1:
                raise MalformedCallError()
            return verdict(True)
        return reply("Claim.", finish_call())

    state = await Harness(ScriptedLLM(responder), registry, settings).run()
    assert state.status == "succeeded"  # one glitch costs a step, not the audit

    def always_broken(messages, tools):
        if messages[0].content.startswith(AUDITOR_PREFIX):
            raise MalformedCallError()
        return reply("Claim.", finish_call())

    state = await Harness(ScriptedLLM(always_broken), registry, settings).run()
    assert state.status == "needs_attention" and "malformed tool calls" in state.final_report["verification"]["summary"]
