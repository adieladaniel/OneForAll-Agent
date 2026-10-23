"""Per-call LLM timeout and cancel-interrupts-a-model-call (fakes only, no network)."""
from __future__ import annotations

import asyncio
import itertools
import time
from types import SimpleNamespace

import pytest
from google.genai import types

from worker import metrics
from worker.agent.broker import AutoBroker
from worker.agent.runner import AgentRunner
from worker.config import Settings
from worker.llm.base import LLMClient, LLMResponse, LLMTimeoutError, Message, ToolCall, TransientLLMError, Usage
from worker.llm.factory import make_llm
from worker.llm.failover import FailoverLLM, ModelHealth
from worker.llm.gemini import GeminiClient
from worker.llm.resilient import ResilientLLM
from worker.tools.base import Risk, Tool, ToolResult
from worker.tools.control import FinishTool, UpdatePlanTool
from worker.tools.registry import ToolRegistry

_ids = itertools.count(1)
AUDITOR_PREFIX = "You are an independent auditor"


def reply(text: str = "", *calls: ToolCall) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=list(calls), usage=Usage(10, 5))


def call(name: str, **args) -> ToolCall:
    return ToolCall(id=f"call_{next(_ids)}", name=name, args=args)


def counter(model: str, outcome: str) -> float:
    return metrics.LLM_CALLS.labels(model, outcome)._value.get()


# ---------------------------------------------------------------- provider timeout


class HangingSDK:
    """Stands in for ``genai.Client`` with a server that accepts the request and never answers."""

    def __init__(self):
        self.calls = 0
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, *, model, contents, config):
        self.calls += 1
        await asyncio.Event().wait()


class AnsweringSDK:
    def __init__(self):
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, *, model, contents, config):
        part = types.Part(text="from the second model")
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(role="model", parts=[part]), finish_reason="STOP")],
            usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=5, candidates_token_count=2),
        )


async def test_gemini_call_that_never_returns_times_out_as_unavailable():
    sdk = HangingSDK()
    started = time.monotonic()
    with pytest.raises(LLMTimeoutError) as info:
        await GeminiClient("", "m", client=sdk, timeout_s=0.05).complete([Message("user", "x")])
    assert time.monotonic() - started < 2
    err = info.value
    assert isinstance(err, TransientLLMError) and err.unavailable and not err.network  # classified like a 503
    assert err.timeout_s == 0.05 and "0.05s" in str(err)


async def test_outer_cancellation_is_not_mistaken_for_a_timeout():
    task = asyncio.ensure_future(GeminiClient("", "m", client=HangingSDK(), timeout_s=30).complete([Message("user", "x")]))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_timeout_fails_over_to_the_next_model_and_only_marks_it_temporarily_unavailable():
    hanging, answering = HangingSDK(), AnsweringSDK()
    clients = {"slow": GeminiClient("", "slow", client=hanging, timeout_s=0.05),
               "fast": GeminiClient("", "fast", client=answering, timeout_s=0.05)}
    sleeps: list[float] = []

    async def sleep(s):
        sleeps.append(s)

    health = ModelHealth()
    llm = FailoverLLM(["slow", "fast"], lambda m: clients[m], health=health, sleep=sleep)
    timeouts_before = counter("slow", "timeout")
    started = time.monotonic()
    out = await asyncio.wait_for(llm.complete([Message("user", "hi")]), 5)
    assert time.monotonic() - started < 2  # one timeout of 50 ms, not a hang
    assert out.text == "from the second model" and llm.active_model == "fast"
    assert hanging.calls == 1 and sleeps == []  # no quick retry for a timeout: straight to the next model
    assert health.reason("slow") == "unavailable"  # a cooldown, not a daily quota
    assert health.exhausted(["slow", "fast"]) == [] and health.reason("fast") is None
    assert counter("slow", "timeout") - timeouts_before == 1  # the timed-out attempt is counted


async def test_resilient_llm_counts_a_timeout_that_reaches_it_and_retries_it():
    hanging = HangingSDK()
    client = GeminiClient("", "solo", client=hanging, timeout_s=0.02)
    resilient = ResilientLLM(client, min_interval_s=0.0, max_retries=2, sleep=_instant)
    before = counter("solo", "timeout")
    with pytest.raises(LLMTimeoutError):
        await asyncio.wait_for(resilient.complete([Message("user", "x")]), 5)
    assert hanging.calls == 3 and resilient.retries == 2  # first try + max_retries, as for any transient error
    assert counter("solo", "timeout") - before == 3


async def _instant(_s):
    await asyncio.sleep(0)


def test_call_timeout_setting_reaches_every_gemini_client():
    assert Settings(_env_file=None).LLM_CALL_TIMEOUT_S == 60.0
    settings = Settings(_env_file=None, LLM_PROVIDER="gemini", GEMINI_API_KEY="test-key", LLM_CALL_TIMEOUT_S=12.5)
    failover = make_llm(settings).inner
    assert failover._factory(failover.models[0]).timeout_s == 12.5


# ---------------------------------------------------------------- cancel interrupts a model call


class HangingLLM(LLMClient):
    """A model call that never returns; remembers when it was entered and whether it was aborted."""

    name = "hang"

    def __init__(self, hang_when=lambda messages: True, answers=()):
        self.entered, self.aborted, self.calls = asyncio.Event(), False, 0
        self._hang_when, self._answers = hang_when, list(answers)

    async def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        if not self._hang_when(messages):
            return self._answers.pop(0)
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.aborted = True
            raise


class SaveTool(Tool):
    """A write that takes a while: ``release`` lets it finish, ``started`` says it is under way."""

    name = "save_record"
    description = "Save a record (write)."
    parameters = {"type": "object", "properties": {"value": {"type": "string"}}}
    risk = Risk.WRITE
    read_only_safe = False

    def __init__(self):
        self.started, self.release, self.saved = asyncio.Event(), asyncio.Event(), []

    async def run(self, args, ctx):
        self.started.set()
        await self.release.wait()
        self.saved.append(args)
        return ToolResult(True, "Saved record R-1")


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "ws").mkdir()
    return Settings(_env_file=None, RUNS_DIR=str(tmp_path / "runs"), WORKSPACE_DIR=str(tmp_path / "ws"),
                    APPROVAL_MODE="auto", PROVENANCE_CHECK=False)


def make_runner(llm, settings, *tools):
    registry = ToolRegistry()
    for tool in (*tools, UpdatePlanTool(), FinishTool()):
        registry.register(tool)
    return AgentRunner("Do the task", llm, registry, settings, AutoBroker.approve_all(), run_id="t1")


async def test_cancel_abandons_a_hanging_model_call_and_ends_the_run_quickly(settings):
    llm = HangingLLM()
    runner = make_runner(llm, settings)
    task = asyncio.ensure_future(runner.run())
    await asyncio.wait_for(llm.entered.wait(), 5)
    runner.cancel()
    started = time.monotonic()
    state = await asyncio.wait_for(task, 5)
    assert time.monotonic() - started < 2
    assert state.status == "cancelled" and state.final_report["status"] == "cancelled"
    assert llm.aborted  # the awaiting call was cancelled, not left running in the background


async def test_cancel_through_the_resilient_failover_stack_aborts_the_call(settings):
    inner = HangingLLM()
    llm = ResilientLLM(FailoverLLM(["a", "b"], lambda m: inner, health=ModelHealth()), min_interval_s=0.0)
    runner = make_runner(llm, settings)
    task = asyncio.ensure_future(runner.run())
    await asyncio.wait_for(inner.entered.wait(), 5)
    runner.cancel()
    assert (await asyncio.wait_for(task, 5)).status == "cancelled" and inner.aborted


async def test_cancel_before_the_first_model_call_never_calls_the_model(settings):
    llm = HangingLLM()
    runner = make_runner(llm, settings)
    runner.cancel()
    state = await asyncio.wait_for(runner.run(), 5)
    assert state.status == "cancelled" and llm.calls == 0


async def test_cancel_abandons_a_hanging_auditor_call(settings):
    finish = call("finish", status="success", summary="done", evidence=[{"claim": "c", "source": "s"}],
                  outputs=[{"name": "n", "value": "v"}])
    llm = HangingLLM(hang_when=lambda messages: messages[0].content.startswith(AUDITOR_PREFIX),
                     answers=[reply("Done.", finish)])
    runner = make_runner(llm, settings)
    task = asyncio.ensure_future(runner.run())
    await asyncio.wait_for(llm.entered.wait(), 5)
    assert runner.state.status == "verifying"
    runner.cancel()
    state = await asyncio.wait_for(task, 5)
    assert state.status == "cancelled" and llm.aborted and llm.calls == 2


async def test_cancel_during_a_write_lets_the_write_finish_before_stopping(settings):
    save = SaveTool()
    llm = HangingLLM(hang_when=lambda messages: False, answers=[reply("Saving.", call("save_record", value="42"))])
    runner = make_runner(llm, settings, save)
    task = asyncio.ensure_future(runner.run())
    await asyncio.wait_for(save.started.wait(), 5)
    runner.cancel()
    await asyncio.sleep(0.1)
    assert not task.done() and save.saved == []  # the write is not interrupted, and the run waits for it
    save.release.set()
    state = await asyncio.wait_for(task, 5)
    assert save.saved == [{"value": "42"}]  # the write completed atomically ...
    assert state.status == "cancelled" and llm.calls == 1  # ... and then the run stopped without another model call
    assert [s.tool for s in state.steps if s.tool == "save_record" and s.ok] == ["save_record"]
