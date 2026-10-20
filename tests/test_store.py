"""RunStore seam: one behavioural contract run against FileRunStore and PgRunStore (the latter skipped without a
database), RunState/Guards/ApprovalPolicy serialisation and resuming interrupted runs."""
from __future__ import annotations

import itertools
import json
import re
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from worker.agent.broker import AutoBroker
from worker.agent.guards import Guards
from worker.agent.policy import ApprovalPolicy
from worker.agent.prompts import RESUME_NOTE
from worker.agent.runner import AgentRunner, ResumeError
from worker.agent.state import Fact, PlanItem, RunState, Step
from worker.config import Settings
from worker.llm.base import LLMResponse, ToolCall, Usage
from worker.llm.fake import ScriptedLLM
from worker.server import create_app
from worker.store import FileRunStore
from worker.tools.base import Risk, Tool, ToolResult
from worker.tools.control import FinishTool, UpdatePlanTool
from worker.tools.interaction import AskUserTool
from worker.tools.memory_tools import RememberTool
from worker.tools.registry import ToolRegistry

AUDITOR_PREFIX = "You are an independent auditor"
_ids = itertools.count(1)


def new_id() -> str:
    """A run id valid for every store (a PostgreSQL run id must be a UUID)."""
    return uuid.uuid4().hex


# ---------------------------------------------------------------- helpers


class Crash(BaseException):
    """Simulates the process dying: not an Exception, so the runner's own error handling cannot catch it."""


class FakeClock:
    """A controllable monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def call(name: str, **args) -> ToolCall:
    return ToolCall(id=f"call_{next(_ids)}", name=name, args=args)


def reply(text: str = "", *calls: ToolCall) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=list(calls), usage=Usage(10, 5))


def finish_call() -> ToolCall:
    return call("finish", status="success", summary="All done.", evidence=[{"claim": "saved", "source": "save_record"}],
                outputs=[{"name": "record_id", "value": "R-1"}])


def verdict() -> LLMResponse:
    return reply("", call("submit_verdict", passed=True, summary="ok",
                          checks=[{"criterion": "Saved", "passed": True, "observed": "saw it"}],
                          ambiguity={"passed": True, "observed": "single candidate"}, completeness={"passed": True, "observed": "single candidate"}))


def scripted(clock: FakeClock, worker: list, auditor: list = (), step_s: float = 2.0) -> ScriptedLLM:
    """Scripted worker/auditor replies; every LLM call takes ``step_s`` of fake time. An exception item is raised."""
    worker_q, auditor_q = list(worker), list(auditor)

    def responder(messages, tools):
        clock.now += step_s
        item = (auditor_q if messages[0].content.startswith(AUDITOR_PREFIX) else worker_q).pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    return ScriptedLLM(responder)


class ReadNoteTool(Tool):
    name = "read_note"
    description = "Read a note."
    parameters = {"type": "object", "properties": {"topic": {"type": "string"}}}

    async def run(self, args, ctx):
        return ToolResult(True, "note: 42")


class SaveRecordTool(Tool):
    name = "save_record"
    description = "Save a record (write)."
    parameters = {"type": "object", "properties": {"value": {"type": "string"}}}
    risk = Risk.WRITE
    read_only_safe = False

    def __init__(self) -> None:
        self.saved: list[dict] = []

    async def run(self, args, ctx):
        self.saved.append(args)
        return ToolResult(True, f"Saved record R-{len(self.saved)}")


class BrokenTool(Tool):
    name = "primary_path"
    description = "Always fails."
    parameters = {"type": "object", "properties": {"n": {"type": "integer"}}}
    read_only_safe = False

    async def run(self, args, ctx):
        return ToolResult(False, "Validation failed", "validation")


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "ws").mkdir()
    return Settings(_env_file=None, RUNS_DIR=str(tmp_path / "runs"), WORKSPACE_DIR=str(tmp_path / "ws"), MAX_STEPS=30,
                    MAX_CONSECUTIVE_FAILURES=4, RUN_TIMEOUT_S=60, APPROVAL_MODE="confirm_writes")


@pytest.fixture
def save_tool():
    return SaveRecordTool()


@pytest.fixture
def registry(save_tool):
    reg = ToolRegistry()
    for tool in (ReadNoteTool(), save_tool, BrokenTool(), RememberTool(), UpdatePlanTool(), FinishTool(), AskUserTool()):
        reg.register(tool)
    return reg


def sample_state() -> RunState:
    state = RunState(run_id="s1", task="Do it", status="waiting_approval", goal_summary="Goal",
                     success_criteria=["A", "B"], plan=[PlanItem(1, "first", "done", "ok"), PlanItem(2, "second")],
                     usage={"llm_calls": 3, "input_tokens": 30, "output_tokens": 9, "model": "m"},
                     final_report={"status": "x", "summary": "s"}, finished_at="2026-01-01T00:00:00+00:00")
    state.facts["k"] = Fact("k", "v", "src", 2)
    state.add_step(Step(kind="tool", thought="t", thought_is_summary=True, tool="read_note", args={"topic": "a"},
                        risk="write", ok=False, observation="o", error="e", evidence=["screenshots/a.png"],
                        duration_ms=7, retries=1, turn=1, tool_call_id="c1"))
    state.add_step(Step(kind="approval", tool="save_record", args={"v": 1}, ok=True, turn=2))
    return state


# ---------------------------------------------------------------- serialisation


def test_run_state_round_trips_through_json():
    state = sample_state()
    restored = RunState.from_dict(json.loads(json.dumps(state.to_dict())))
    assert restored == state
    assert isinstance(restored.steps[0], Step) and isinstance(restored.facts["k"], Fact)
    assert isinstance(restored.plan[0], PlanItem) and [s.index for s in restored.steps] == [1, 2]


def test_from_dict_tolerates_missing_and_unknown_fields():
    old = {"run_id": "o1", "task": "t", "future_field": 1, "steps": [{"index": 1, "tool": "x", "unknown": True}]}
    state = RunState.from_dict(old)
    assert state.status == "pending" and state.usage["llm_calls"] == 0 and state.steps[0].tool == "x"


def test_guards_round_trip_and_the_clock_pauses():
    clock = FakeClock()
    guards = Guards(30, 4, 100, clock=clock)
    for n in range(2):
        guards.record(Step(tool="primary_path", args={"n": n}, ok=False, error="validation", observation="bad"))
    assert guards.check_progress().level == "nudge"  # second failure: soft limit reached
    clock.now += 30
    guards.pause_clock()
    clock.now += 5000
    assert guards.active_seconds == 30
    assert guards.check_limits(0, 0) is None  # the long pause does not trip the timeout
    guards.resume_clock()
    clock.now += 5

    restored = Guards.from_dict(json.loads(json.dumps(guards.to_dict())), 30, 4, 100, clock=clock)
    assert restored.to_dict() == guards.to_dict() and restored.active_seconds == 35
    assert restored.check_progress() is None  # already nudged: no second nudge for the same streak
    clock.now += 70
    assert restored.check_limits(0, 0).guard == "timeout"


def test_approval_policy_round_trip(save_tool):
    policy = ApprovalPolicy("confirm_all")
    policy.record_response(save_tool, Risk.WRITE, {"approved": True, "remember": True})
    restored = ApprovalPolicy.from_dict(json.loads(json.dumps(policy.to_dict())))
    assert restored.mode == "confirm_all" and not restored.needs_approval(save_tool, {}, Risk.WRITE)
    assert restored.needs_approval(save_tool, {}, Risk.IRREVERSIBLE)


# ---------------------------------------------------------------- the RunStore contract (file and PostgreSQL)


async def test_store_round_trips_every_part_of_a_run(store_factory):
    store, run_id = store_factory(), new_id()
    state = RunState(run_id=run_id, task="Task one", status="running")
    await store.create_run(state)
    assert await store.load_state(new_id()) is None and await store.load_runtime(run_id) is None
    assert await store.load_report_markdown(run_id) is None

    step = state.add_step(Step(kind="tool", tool="read_note", observation="x", tool_call_id="c1", turn=1))
    await store.append_step(run_id, step)
    for key, value in (("k", "one"), ("k", "two"), ("other", "v")):  # upsert: the same key overwrites
        state.facts[key] = Fact(key, value, "src", 1)
        await store.upsert_fact(run_id, state.facts[key])
    state.goal_summary, state.plan = "Goal", [PlanItem(1, "p", "done")]
    await store.save_state(state)
    runtime = {"turn": 3, "nudges": ["n"], "guards": {"failures": 1}}
    await store.save_runtime(run_id, runtime)
    assert [await store.append_event(run_id, "run_started", {"task": "Task one"}),
            await store.append_event(run_id, "thought", {"text": "hi"}),
            await store.append_event(run_id, "usage", {"llm_calls": 1})] == [1, 2, 3]

    loaded = await store.load_state(run_id)
    assert loaded == state and list(loaded.facts) == ["k", "other"] and loaded.facts["k"].value == "two"
    assert await store.load_runtime(run_id) == runtime
    events = await store.read_events(run_id)
    assert [(e["seq"], e["type"]) for e in events] == [(1, "run_started"), (2, "thought"), (3, "usage")]
    assert events[0]["payload"] == {"task": "Task one"} and events[0]["ts"]
    assert [e["seq"] for e in await store.read_events(run_id, after_seq=1)] == [2, 3]
    assert await store.read_events(run_id, after_seq=3) == []

    state.status, state.final_report = "succeeded", {"status": "succeeded", "summary": "done"}
    await store.save_state(state)
    await store.save_report(run_id, state.final_report, f"# Run {run_id}\n")
    assert await store.load_report_markdown(run_id) == f"# Run {run_id}\n"
    assert (await store.load_state(run_id)).final_report["summary"] == "done"

    # A second store instance (another process, or after a restart) sees everything and continues the event log.
    fresh = store_factory()
    assert await fresh.load_state(run_id) == state
    assert await fresh.load_runtime(run_id) == runtime
    assert await fresh.append_event(run_id, "late", {}) == 4
    assert [e["seq"] for e in await fresh.read_events(run_id)] == [1, 2, 3, 4]


async def test_store_creates_a_run_with_the_steps_and_facts_it_already_has(store_factory):
    store, state = store_factory(), sample_state()
    state.run_id = new_id()
    await store.create_run(state)
    assert await store.load_state(state.run_id) == state


async def test_store_lists_runs_newest_first_and_guards_paths(store_factory):
    store = store_factory()
    assert await store.list_runs() == []
    ids = {"old": new_id(), "new": new_id()}
    for name, started in (("old", "2026-01-01T00:00:00+00:00"), ("new", "2026-02-01T00:00:00+00:00")):
        await store.create_run(RunState(run_id=ids[name], task=f"task {name}", started_at=started))
    rows = await store.list_runs()
    assert [r["id"] for r in rows] == [ids["new"], ids["old"]] and rows[0]["task"] == "task new"
    assert rows[0]["finished_at"] is None and rows[0]["status"] == "pending" and rows[0]["started_at"] == "2026-02-01T00:00:00+00:00"
    assert [r["id"] for r in await store.list_runs(limit=1)] == [ids["new"]]

    shots = store.artifact_dir(ids["new"]) / "screenshots"
    shots.mkdir()
    (shots / "a.png").write_bytes(b"png")
    (shots.parent.parent / "secret.txt").write_text("secret")
    assert store.artifact_file(ids["new"], "screenshots/a.png") == (shots / "a.png").resolve()
    assert store.artifact_file(ids["new"], "../secret.txt") is None and store.artifact_file(ids["new"], "missing.png") is None
    with pytest.raises(ValueError):
        store.artifact_dir("../escape")


async def test_file_store_layout_on_disk(tmp_path):
    store = FileRunStore(tmp_path / "runs")
    state = RunState(run_id="a1", task="Task one", status="running", final_report={"status": "x", "summary": "done"})
    await store.create_run(state)
    await store.save_runtime("a1", {"turn": 1})
    for n in range(3):
        await store.append_event("a1", "thought", {"n": n})
    await store.save_report("a1", state.final_report, "# Run a1\n")
    run_dir = tmp_path / "runs" / "a1"
    assert {p.name for p in run_dir.iterdir()} == {"run.json", "events.jsonl", "runtime.json", "report.md"}
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["final_report"]["summary"] == "done"
    assert len((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()) == 3


# ---------------------------------------------------------------- the runner writes only through the store


async def test_runner_persists_steps_facts_events_and_runtime_through_the_store(registry, settings, store_factory, tmp_path):
    store, run_id = store_factory(), new_id()  # not settings.RUNS_DIR: proves the runner uses the given store
    clock = FakeClock()
    llm = scripted(clock, [reply("Note it.", call("remember", key="answer", value="42"), call("read_note")),
                           reply("Done.", finish_call())], [verdict()])
    state = await AgentRunner("Task", llm, registry, settings, AutoBroker.approve_all(), run_id=run_id, store=store).run()
    assert state.status == "succeeded"
    assert not (tmp_path / "runs").exists()

    loaded = await store.load_state(run_id)
    assert loaded == state and loaded.facts["answer"].value == "42"
    events = await store.read_events(run_id)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[0]["type"] == "run_started" and events[-1]["type"] == "run_finished"
    assert "Independent audit: **PASSED**" in await store.load_report_markdown(run_id)
    runtime = await store.load_runtime(run_id)
    assert runtime["turn"] == 2 and runtime["verdict"]["passed"] and runtime["claim"]["summary"] == "All done."
    assert set(runtime) == {"turn", "text_only", "force_tool", "nudges", "feedback", "verify_failures", "claim",
                            "verdict", "approvals", "unsourced_blocked", "resume_count", "guards", "policy"}
    json.dumps(runtime)


def test_generated_run_ids_are_full_uuids(registry, settings):
    runner = AgentRunner("t", ScriptedLLM([]), registry, settings, AutoBroker.approve_all())
    assert re.fullmatch(r"[0-9a-f]{32}", runner.state.run_id)


# ---------------------------------------------------------------- resume


async def test_resume_continues_an_interrupted_run_and_keeps_counters(registry, settings, save_tool, store_factory):
    clock, store, run_id = FakeClock(), store_factory(), new_id()

    def approve_slowly(kind, payload):
        clock.now += 500  # the human takes 500 s, far beyond RUN_TIMEOUT_S
        return {"approved": True, "remember": True}

    llm1 = scripted(clock, [
        reply("Save first.", call("update_plan", goal_summary="G", success_criteria=["Saved"],
                                  items=[{"text": "save", "status": "in_progress"}]), call("save_record", value="a")),
        reply("Try.", call("primary_path", n=1)),
        reply("Try again.", call("primary_path", n=2)),  # second failure: the failure guard nudges
        Crash("process killed"),
    ])
    first = AgentRunner("Save records", llm1, registry, settings, AutoBroker(approve_slowly), run_id=run_id, store=store,
                        clock=clock)
    with pytest.raises(Crash):
        await first.run()

    crashed = await store.load_state(run_id)
    assert crashed.status == "running" and len(crashed.steps) == 6
    saved = await store.load_runtime(run_id)
    assert saved["turn"] == 3 and saved["guards"]["failures"] == 2 and saved["guards"]["failure_nudged"] is True
    assert saved["guards"]["active_seconds"] == 6.0  # three LLM calls; the 500 s approval wait is not counted
    assert saved["policy"]["approved_similar"] == [["save_record", "write"]]
    assert saved["approvals"] == {"requested": 1, "approved": 1, "denied": 0} and len(saved["nudges"]) == 1

    clock.now += 10_000  # the worker was down for a long time
    broker2 = AutoBroker(lambda kind, p: pytest.fail("approve-similar must survive the restart"))
    llm2 = scripted(clock, [reply("Retry differently.", call("save_record", value="b")), reply("Done.", finish_call())],
                    [verdict()])
    events: list[tuple[str, dict]] = []

    async def sink(event_type, payload):
        events.append((event_type, payload))

    store = store_factory()  # a new process: nothing cached
    runner = await AgentRunner.resume(run_id, llm2, registry, settings, broker2, sink, store=store, clock=clock)
    assert runner.guards.to_dict()["failures"] == 2 and runner.guards.to_dict()["failure_nudged"] is True
    assert runner.guards.active_seconds == 6.0 and runner._turn == 3
    state = await runner.run()

    assert state.status == "succeeded" and [s["value"] for s in save_tool.saved] == ["a", "b"]
    assert [s.index for s in state.steps] == list(range(1, len(state.steps) + 1))  # continuous, no duplicates
    assert len({id(s) for s in state.steps}) == len(state.steps) and len(state.steps) > 6
    note = state.steps[6]
    assert note.kind == "system" and note.thought == RESUME_NOTE and note.error == "resumed"
    assert [s.turn for s in state.steps] == sorted(s.turn for s in state.steps)  # turn counter carried on
    assert state.steps[7].tool == "save_record" and state.steps[7].turn == 4
    assert state.goal_summary == "G" and state.plan[0].text == "save"

    seen = llm2.calls[0]["messages"][-1].content  # the resumed agent is told about both the restart and the nudge
    assert RESUME_NOTE in seen and "consecutive failures" in seen

    final = await store.load_runtime(run_id)
    assert final["resume_count"] == 1 and final["guards"]["active_seconds"] == 12.0  # 6 + 3 more LLM calls of 2 s
    types = [e["type"] for e in await store.read_events(run_id)]
    assert types.count("run_started") == 1 and types.count("run_resumed") == 1 and types[-1] == "run_finished"
    assert [t for t, _ in events][0] == "run_resumed"
    assert [e["seq"] for e in await store.read_events(run_id)] == list(range(1, len(types) + 1))


class DiesWhileWaiting:
    """Wraps a store; the process is killed right after the waiting state was saved."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def save_state(self, state):
        await self.inner.save_state(state)
        if state.status == "waiting_approval":
            raise Crash("killed while the approval card was open")


async def test_resume_after_a_crash_while_waiting_for_approval(registry, settings, save_tool, store_factory):
    run_id = new_id()

    llm1 = scripted(FakeClock(), [reply("Save.", call("save_record", value="a"))])
    runner1 = AgentRunner("T", llm1, registry, settings, AutoBroker.approve_all(), run_id=run_id,
                          store=DiesWhileWaiting(store_factory()))
    with pytest.raises(Crash):
        await runner1.run()
    store = store_factory()
    assert (await store.load_state(run_id)).status == "waiting_approval" and save_tool.saved == []

    llm2 = scripted(FakeClock(), [reply("Save again.", call("save_record", value="a")), reply("End.", finish_call())],
                    [verdict()])
    runner = await AgentRunner.resume(run_id, llm2, registry, settings, AutoBroker.approve_all(), store=store)
    state = await runner.run()
    assert state.status == "succeeded" and save_tool.saved == [{"value": "a"}]  # the write ran once, after approval
    assert state.final_report["stats"]["approvals"] == 1
    assert state.steps[0].kind == "system" and state.steps[0].error == "resumed"


async def test_finished_or_unknown_runs_cannot_be_resumed(registry, settings, store_factory):
    store, clock, done_id = store_factory(), FakeClock(), new_id()
    finished = AgentRunner("T", scripted(clock, [reply("x", call("finish", status="failed", summary="no"))]), registry,
                           settings, AutoBroker.approve_all(), run_id=done_id, store=store)
    assert (await finished.run()).status == "failed"
    with pytest.raises(ResumeError, match="already ended as 'failed'"):
        await AgentRunner.resume(done_id, ScriptedLLM([]), registry, settings, AutoBroker.approve_all(), store=store)
    with pytest.raises(ResumeError, match="Unknown run"):
        await AgentRunner.resume(new_id(), ScriptedLLM([]), registry, settings, AutoBroker.approve_all(), store=store)


# ---------------------------------------------------------------- the server reads through the store


def test_server_uses_the_given_store_and_full_uuid_run_ids(tmp_path):
    (tmp_path / "ws").mkdir()
    settings = Settings(LLM_PROVIDER="fake", RUNS_DIR=str(tmp_path / "unused"), WORKSPACE_DIR=str(tmp_path / "ws"),
                        SIM_BASE_URL="http://127.0.0.1:9", LLM_MIN_INTERVAL_S=0.0)
    store = FileRunStore(tmp_path / "store")

    def registry_factory(_settings):
        reg = ToolRegistry()
        reg.register(FinishTool())
        return reg

    llm = ScriptedLLM([reply("", call("finish", status="blocked", summary="Nothing to do."))])
    with TestClient(create_app(settings, llm_factory=lambda s: llm, registry_factory=registry_factory, store=store)) as client:
        run_id = client.post("/api/runs", json={"task": "Idle"}).json()["run_id"]
        assert re.fullmatch(r"[0-9a-f]{32}", run_id)
        deadline = time.monotonic() + 10
        while client.get("/api/runs").json()[0]["status"] != "needs_attention" and time.monotonic() < deadline:
            time.sleep(0.05)
        detail = client.get(f"/api/runs/{run_id}").json()
        assert detail["report"]["status"] == "needs_attention" and detail["report_url"].endswith("report.md")
        assert "Nothing to do." in client.get(detail["report_url"]).text
        with client.websocket_connect(f"/api/runs/{run_id}/events") as ws:
            types = []
            while (event := ws.receive_json())["type"] != "replay_done":
                types.append(event["type"])
        assert types[0] == "run_started" and types[-1] == "run_finished"
    assert (tmp_path / "store" / run_id / "run.json").is_file() and not (tmp_path / "unused").exists()
