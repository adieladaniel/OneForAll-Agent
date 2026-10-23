"""AgentRunner: the task-agnostic loop. Context -> LLM -> (policy/approval) -> tools -> record -> guards -> verify."""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from worker import metrics
from worker.agent.broker import InteractionBroker
from worker.agent.context import build_messages, compress_step
from worker.agent.guards import GuardEvent, Guards, counts_toward_budget
from worker.agent.interrupt import CallInterrupted, await_unless_set
from worker.agent.memory import WorkingMemory
from worker.agent.policy import INTERNAL_TOOLS, ApprovalPolicy
from worker.agent.prompts import (
    DENIED_TEMPLATE,
    FINISH_NOT_ALONE,
    MALFORMED_CALL_NOTE,
    NO_TOOL_NUDGE,
    RESUME_NOTE,
    SKIPPED_AFTER_FAILURE,
    UNSOURCED_APPROVAL_WARNING,
    UNSOURCED_VALUES_TEMPLATE,
    VERIFY_FAILED_TEMPLATE,
)
from worker.agent.provenance import SourceIndex, blocked_key, describe, find_unsourced, source_texts
from worker.agent.report import build_report, render_markdown
from worker.agent.state import (
    STORED_OBSERVATION_LIMIT,
    TERMINAL_STATUSES,
    PlanItem,
    RunState,
    Step,
    truncate,
    utcnow,
)
from worker.agent.thoughts import THOUGHT_LIMIT, clean_thought
from worker.agent.verifier import Verdict, Verifier
from worker.config import Settings
from worker.llm.base import LLMClient, LLMError, LLMResponse, MalformedCallError, QuotaExceededError
from worker.logging_setup import clear_run_redactor, log_context, register_run_redactor
from worker.redaction import RedactingStore, Redactor
from worker.store import FileRunStore, RunStore
from worker.tools.base import Risk, Tool, ToolContext, ToolResult
from worker.tools.control import normalize_finish
from worker.tools.registry import ToolRegistry
from worker.tools.retry import execute_with_retry

if TYPE_CHECKING:
    from worker.vault import VaultScope

log = logging.getLogger(__name__)

EventSink = Callable[[str, dict], Awaitable[None]]
MAX_VERIFY_ROUNDS = 2  # feedback rounds after a failed audit; a further failed audit ends the run as failed
MAX_TEXT_ONLY_REPLIES = 2
EVENT_OBSERVATION_LIMIT = 2000
ANSWER_PREFIX = "User answered: "  # how AskUserTool words a received answer (worker/tools/interaction.py)
_RETRY_NOTE = re.compile(r"after (\d+) automatic retr")
_STOP_WORDS = ("stop", "cancel", "abort", "quit", "give up")
_NOT_LOADED: Any = object()  # "the approval preview of this call has not been fetched yet"


class ResumeError(Exception):
    """The run cannot be resumed (unknown run, or it already reached a final status)."""


class _Stop(Exception):
    """Internal control flow: end the run with this status."""

    def __init__(self, status: str, reason: str):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class AgentRunner:
    """Runs one task to completion. Usage: ``state = await AgentRunner(task, llm, registry, settings, broker).run()``.

    Everything is persisted through ``store`` (default: ``FileRunStore(settings.RUNS_DIR)``). Events are appended to the
    store and pushed to ``event_sink(event_type, payload)``; every payload carries ``run_id``. An interrupted run is
    continued with ``await AgentRunner.resume(run_id, ...)`` followed by ``.run()``.

    ``clock`` is the monotonic time source of the wall-clock guard (injectable for tests).

    ``precreated``: the run already exists in the store (a queued run claimed by a worker, ``state`` loaded from it), so
    ``run`` updates it instead of creating it.

    ``vault_scope`` (see ``worker/vault.py``) gives the run credentials it can use through the secret tools without ever
    seeing them. When it holds any, a ``Redactor`` built from its secret values scrubs everything that is stored (the
    store is wrapped in a ``RedactingStore``), emitted, shown to approvers or sent to the model.
    """

    def __init__(
        self,
        task: str,
        llm: LLMClient,
        registry: ToolRegistry,
        settings: Settings,
        broker: InteractionBroker,
        run_id: str | None = None,
        event_sink: EventSink | None = None,
        store: RunStore | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        state: RunState | None = None,
        vault_scope: VaultScope | None = None,
        precreated: bool = False,
    ):
        self.llm, self.registry, self.settings, self.broker = llm, registry, settings, broker
        self.store: RunStore = store or FileRunStore(settings.RUNS_DIR)
        self.vault_scope = vault_scope
        self.redactor: Redactor | None = (Redactor.from_scope(vault_scope) or None) if vault_scope else None
        self._vault_public = vault_scope.public() if vault_scope else []
        if self.redactor:
            self.store = RedactingStore(self.store, self.redactor)
        self._clock = clock
        self._precreated = precreated
        self.state = state or RunState(run_id=run_id or uuid.uuid4().hex, task=task)  # ``state``: see ``resume``
        if self.redactor:
            self.state.task = self.redactor.redact(self.state.task)
        self.memory = WorkingMemory(self.state.facts)
        self.policy = ApprovalPolicy(settings.APPROVAL_MODE)
        self.guards = self._new_guards()
        self.run_dir = self.store.artifact_dir(self.state.run_id)
        self.ctx = ToolContext(
            run_id=self.state.run_id,
            run_dir=self.run_dir,
            workspace=Path(settings.WORKSPACE_DIR),
            settings=settings,
            memory=self.memory,
            emit=self._emit,
            extras={"interact": self._interact},
        )
        if vault_scope is not None:
            self.ctx.extras["vault_scope"] = vault_scope
        if self.redactor:
            self.ctx.extras["redactor"] = self.redactor
        self._sink = event_sink
        self._cancelled = asyncio.Event()
        self._turn = 0
        self._text_only = 0
        self._force_tool = False
        self._resume_count = 0
        self._knowledge: list[dict] = []  # organization memories recalled for this run (see ``_recall_org_memory``)
        self._nudges: list[str] = []
        self._feedback = ""
        self._verify_failures = 0
        self._claim: dict | None = None
        self._verdict: Verdict | None = None
        self._approvals = {"requested": 0, "approved": 0, "denied": 0}
        self._unsourced_blocked: set[tuple[str, str]] = set()  # values already rejected once for lacking a source
        self._obs = metrics.RunObserver()  # active gauge, run duration, human waits (worker/metrics.py)
        if hasattr(llm, "on_retry"):  # ResilientLLM: surface network waits in the UI and let cancel interrupt them
            llm.on_retry = self._on_llm_retry

    def _new_guards(self, data: dict | None = None) -> Guards:
        args = (self.settings.MAX_STEPS, self.settings.MAX_CONSECUTIVE_FAILURES, self.settings.RUN_TIMEOUT_S)
        return Guards.from_dict(data, *args, clock=self._clock) if data else Guards(*args, clock=self._clock)

    @classmethod
    async def resume(
        cls,
        run_id: str,
        llm: LLMClient,
        registry: ToolRegistry,
        settings: Settings,
        broker: InteractionBroker,
        event_sink: EventSink | None = None,
        store: RunStore | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        vault_scope: VaultScope | None = None,
    ) -> AgentRunner:
        """Rebuild the runner of an interrupted run from the store; ``await .run()`` then continues where it stopped.

        The state and the hidden runtime state (counters, guards, approval cache) are loaded, a system note telling
        the agent to re-check the world is added, and ``resume_count`` is incremented. The approval mode the run
        started with is kept; limits come from ``settings``. Raises ``ResumeError`` for unknown or finished runs.
        """
        store = store or FileRunStore(settings.RUNS_DIR)
        state = await store.load_state(run_id)
        if state is None:
            raise ResumeError(f"Unknown run {run_id}.")
        if state.status in TERMINAL_STATUSES:
            raise ResumeError(f"Run {run_id} already ended as '{state.status}' and cannot be resumed.")
        runner = cls(state.task, llm, registry, settings, broker, event_sink=event_sink, store=store, clock=clock,
                     state=state, vault_scope=vault_scope)
        runner._restore(await store.load_runtime(run_id) or {})
        runner._resume_count += 1
        runner._nudges.append(RESUME_NOTE)
        await runner._add_step(Step(kind="system", thought=RESUME_NOTE, error="resumed", turn=runner._turn))
        return runner

    async def _on_llm_retry(self, info: dict) -> None:
        """Called by ResilientLLM before each wait during a network outage."""
        if self._cancelled.is_set():
            raise _Stop("cancelled", "Cancelled by the user.")
        budget_min = max(1, round(info.get("budget_s", 0) / 60))
        message = f"Network unavailable, retrying the language model for up to {budget_min} min…"
        await self._emit("llm_waiting", {**info, "message": message})

    def cancel(self) -> None:
        """Ask the run to stop at the next safe point (also releases a pending approval/question and abandons a model
        call in flight; a tool call that may be writing always finishes first)."""
        self._cancelled.set()

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> RunState:
        """Execute the task (or continue a resumed one) and return the final state, persisted in the store.

        While it runs, log lines carry the run id, and the run's vault secrets are masked in every log record."""
        with log_context(run_id=self.state.run_id):
            register_run_redactor(self.state.run_id, self.redactor)
            self._obs.begin(resumed=bool(self._resume_count))
            try:
                return await self._run()
            finally:
                self._obs.close()
                clear_run_redactor(self.state.run_id)

    async def _run(self) -> RunState:
        self.state.status = "running"
        if self._resume_count:
            await self._checkpoint()
            await self._emit("run_resumed", {"resume_count": self._resume_count, "message": RESUME_NOTE})
        else:
            if self._precreated:
                await self.store.save_state(self.state)
            else:
                await self.store.create_run(self.state)
            await self.store.save_runtime(self.state.run_id, self._runtime())
            await self._emit("run_started", {"task": self.state.task, "approval_mode": self.policy.mode})
        await self._recall_org_memory()
        status, reason = "failed", ""
        try:
            await self._loop()
        except _Stop as stop:
            status, reason = stop.status, stop.reason
        except asyncio.CancelledError:
            await self._finish_run("cancelled", "The run task was cancelled.")
            raise
        except Exception as exc:  # noqa: BLE001 - never lose the partial report to a bug
            log.exception("agent run crashed")
            status, reason = "failed", f"Internal error: {type(exc).__name__}: {exc}"
        await self._finish_run(status, reason)
        return self.state

    async def _recall_org_memory(self) -> None:
        """Before the first model call: fetch the organization knowledge that matches the task (once per run, and again
        after a resume) and keep it for the state block. Memory is an optional extra (``ctx.extras["org_memory"]``, an
        object with an async ``recall``; absent in file mode or when the org turned it off): a failure never stops the run."""
        memory, limit = self.ctx.extras.get("org_memory"), int(self.ctx.extras.get("org_memory_limit") or 0)
        self._knowledge = []
        if memory is None or limit <= 0:
            return
        try:
            self._knowledge = self._redact(await memory.recall(self.state.task, limit=limit))
        except Exception:  # noqa: BLE001
            log.exception("recalling organization memory failed")
            return
        if self._knowledge:
            await self._emit("memory_recalled", {"keys": [item["key"] for item in self._knowledge]})

    async def _loop(self) -> None:
        tool_names = self.registry.names()
        specs = self.registry.specs()
        while True:
            await self._check_stop()
            messages = build_messages(
                self.state, self.settings, tool_names, nudges=self._nudges, feedback=self._feedback,
                vault_credentials=self._vault_public, knowledge=self._knowledge,
            )
            if self.redactor:  # last line of defense: whatever got into the state, the model never receives a secret
                messages = self.redactor.redact_messages(messages)
            self._nudges = []
            try:
                # A model call only reads, so a cancel request abandons it at once instead of waiting for the provider.
                response = await await_unless_set(
                    self.llm.complete(messages, specs, tool_choice="any" if self._force_tool else "auto"), self._cancelled)
            except CallInterrupted as err:
                raise _Stop("cancelled", "Cancelled by the user.") from err
            except QuotaExceededError as err:  # the org's own daily budget, not a provider problem: say so plainly
                raise _Stop("needs_attention", str(err)) from err
            except MalformedCallError as err:  # the model's reply was unusable: a failed turn, not a dead model
                await self._handle_malformed(err)
                continue
            except LLMError as err:
                raise _Stop("needs_attention", f"The language model could not be reached: {err}") from err
            await self._account(response)
            self._turn += 1
            thought, is_summary = self._thought_of(response)
            if thought:
                await self._emit("thought", {"turn": self._turn, "text": thought})

            if not response.tool_calls:
                await self._handle_text_only(response.text)
                continue
            self._text_only, self._force_tool = 0, False
            await self._handle_calls(response, thought, is_summary)
            event = self.guards.check_progress()
            if event is not None:
                await self._apply_guard(event)

    async def _handle_malformed(self, err: MalformedCallError) -> None:
        """The model kept returning a broken tool call even after ResilientLLM's re-asks (and the other models, if any).

        Record it as a failed turn, tell the model to keep its next call simple, and count it as a failure for the
        consecutive-failure guard, so a model that can only produce broken calls still ends the run (needs_attention)."""
        step = Step(kind="system", thought=str(err), ok=False, error="malformed_call", turn=self._turn,
                    observation="The model's tool call was malformed and was discarded.")
        await self._add_step(step)
        self.guards.record(step)
        await self._emit("llm_malformed_call", {"turn": self._turn, "message": str(err)})
        event = self.guards.check_progress()
        if event is not None and event.level != "nudge":
            await self._emit("guard_triggered", {"guard": event.guard, "level": event.level, "message": event.message})
            raise _Stop("needs_attention", "The language model kept producing malformed tool calls "
                                           f"({event.message.rstrip('.')}); stopped with partial progress.")
        self._nudges.append(MALFORMED_CALL_NOTE)

    async def _check_stop(self) -> None:
        if self._cancelled.is_set():
            raise _Stop("cancelled", "Cancelled by the user.")
        work = sum(1 for s in self.state.steps if counts_toward_budget(s))
        event = self.guards.check_limits(work, len(self.state.steps))
        if event is not None:
            await self._emit("guard_triggered", {"guard": event.guard, "level": event.level, "message": event.message})
            raise _Stop("needs_attention", f"{event.message} Stopped with partial progress.")

    # ------------------------------------------------------------------ turn handling

    async def _handle_text_only(self, text: str) -> None:
        """The model answered without a tool call: nudge once (forcing a tool call), then give up honestly."""
        self._text_only += 1
        self._force_tool = True
        giving_up = self._text_only >= MAX_TEXT_ONLY_REPLIES
        if giving_up:
            self._claim = self._redact({"summary": text, "outputs": {}, "evidence": []})
        else:
            self._nudges.append(NO_TOOL_NUDGE)
        await self._add_step(Step(kind="llm", thought=text, ok=False, error="no_tool_call", turn=self._turn))
        if giving_up:
            raise _Stop("needs_attention", "The agent stopped calling tools without finishing; its last message is the summary.")

    @staticmethod
    def _thought_of(response: LLMResponse) -> tuple[str, bool]:
        """What to show as the step's thought: the model's own text if any, else its condensed reasoning summary."""
        if response.text.strip():
            return response.text.strip(), False
        return clean_thought(getattr(response, "thought", ""), THOUGHT_LIMIT), True

    async def _handle_calls(self, response: LLMResponse, thought: str, is_summary: bool) -> None:
        calls, broken = response.tool_calls, False
        for call in calls:
            step = Step(thought=thought, thought_is_summary=is_summary and bool(thought), tool=call.name, args=call.args,
                        turn=self._turn, tool_call_id=call.id)
            if call.name == "finish" and len(calls) > 1:
                await self._finish_step(self._fail(step, "finish_not_alone", FINISH_NOT_ALONE))
            elif broken:
                await self._finish_step(self._fail(step, "skipped", SKIPPED_AFTER_FAILURE))
            elif call.name == "finish":
                await self._handle_finish(step)
            else:
                broken = not (await self._execute(step)).ok

    async def _execute(self, step: Step) -> Step:
        """classify -> approval gate -> run with retries -> record."""
        tool = self.registry.get(step.tool)
        if tool is None:
            known = ", ".join(self.registry.names())
            return await self._finish_step(self._fail(step, "unknown_tool", f"Unknown tool '{step.tool}'. Available: {known}"))
        risk = self._classify(tool, step.args)
        step.risk = risk.value
        preview, unsourced = _NOT_LOADED, []
        if self.settings.PROVENANCE_CHECK and risk is not Risk.READ and tool.name not in INTERNAL_TOOLS:
            preview = await self._preview(tool, step)
            unsourced = self._unsourced_values(tool, step, preview)
            if unsourced and not all(blocked_key(item) in self._unsourced_blocked for item in unsourced):
                self._unsourced_blocked.update(blocked_key(item) for item in unsourced)  # a second time it goes to a human
                note = UNSOURCED_VALUES_TEMPLATE.format(values=describe(unsourced))
                return await self._finish_step(self._fail(step, "unsourced_values", note))
        if unsourced or self.policy.needs_approval(tool, step.args, risk):  # unsourced again: a person decides, in any mode
            approved, note = await self._seek_approval(tool, step, risk, preview, unsourced)
            if not approved:
                text = DENIED_TEMPLATE.format(note=note or "no reason given")
                return await self._finish_step(self._fail(step, "denied", text))
        step.kind = "ask_user" if tool.name == "ask_user" else "tool"
        self.memory.step = len(self.state.steps) + 1
        await self._emit("tool_started", {"turn": self._turn, "tool": tool.name, "args": step.args, "risk": step.risk,
                                          "action": tool.describe_action(step.args, self.ctx)})
        started = time.monotonic()
        result = await execute_with_retry(tool, step.args, self.ctx)
        step.duration_ms = int((time.monotonic() - started) * 1000)
        metrics.tool_call(tool.name, result.ok, step.duration_ms / 1000)
        step.ok, step.error, step.evidence = result.ok, result.error, list(result.evidence)
        step.observation = truncate(result.observation, STORED_OBSERVATION_LIMIT)
        match = _RETRY_NOTE.search(result.observation)
        step.retries = int(match.group(1)) if match else 0
        if tool.name == "update_plan" and result.ok:
            await self._apply_plan(result.data or {})
        return await self._finish_step(step)

    def _classify(self, tool: Tool, args: dict) -> Risk:
        try:
            return tool.classify(args, self.ctx)
        except Exception:  # noqa: BLE001 - malformed args: assume the tool's declared risk
            return tool.risk

    async def _apply_plan(self, data: dict) -> None:
        data = self._redact(data)
        self.state.plan = [PlanItem(**item) for item in data.get("plan", [])]
        self.state.goal_summary = data.get("goal_summary", self.state.goal_summary)
        self.state.success_criteria = data.get("success_criteria", self.state.success_criteria)
        await self._emit("plan_updated", {"plan": data.get("plan", []), "goal_summary": self.state.goal_summary,
                                          "success_criteria": self.state.success_criteria})

    # ------------------------------------------------------------------ approval and questions

    async def _preview(self, tool: Tool, step: Step) -> list[dict[str, str]] | None:
        """What a human is about to approve, e.g. the filled form fields; never blocks the call."""
        try:
            return await asyncio.wait_for(tool.approval_preview(step.args, self.ctx), timeout=5)
        except Exception:  # noqa: BLE001 - a preview is a convenience only
            return None

    def _unsourced_values(self, tool: Tool, step: Step, preview: list[dict[str, str]] | None) -> list[dict[str, str]]:
        """The values this write submits (the approval preview rows and what the tool adds) that appear in nothing the
        run has read. The check is a safety net: any failure in it lets the call through."""
        try:
            rows = [*(preview or []), *tool.submitted_values(step.args, self.ctx)]
            return find_unsourced(rows, SourceIndex(source_texts(self.state))) if rows else []
        except Exception:  # noqa: BLE001
            log.exception("value provenance check failed")
            return []

    async def _seek_approval(self, tool: Tool, step: Step, risk: Risk, preview: Any = _NOT_LOADED,
                             unsourced: list[dict[str, str]] | None = None) -> tuple[bool, str]:
        action = tool.describe_action(step.args, self.ctx)
        context: dict[str, Any] = {"reason": step.thought, "facts": self.memory.render()}
        if preview is _NOT_LOADED:
            preview = await self._preview(tool, step)
        if preview:
            context["form_values"] = preview
        payload = {"tool": tool.name, "action": action, "args": step.args, "risk": risk.value, "context": context}
        if unsourced:  # values with no source in the run: the card warns the approver
            context["warning"] = UNSOURCED_APPROVAL_WARNING.format(values=describe(unsourced))
            context["unsourced_values"] = unsourced
            payload["unsourced_values"] = unsourced
        self._approvals["requested"] += 1
        response = await self._interact("approval", payload)
        response = response if isinstance(response, dict) else {}
        approved = bool(response.get("approved"))
        no_answer = not response
        note = str(response.get("note") or (self._no_response_reason() if no_answer else ""))
        if not unsourced:  # "approve similar" never covers a call that carried unsourced values
            self.policy.record_response(tool, risk, response)
        self._approvals["approved" if approved else "denied"] += 1
        by = response.get("by") if isinstance(response.get("by"), dict) else {}  # who answered (multi-user mode)
        who = f" by {by.get('name') or by.get('email')}" if by.get("name") or by.get("email") else ""
        await self._add_step(Step(kind="approval", tool=tool.name, args=step.args, risk=risk.value, ok=approved,
                            thought=note, turn=self._turn,
                            observation=f"{'APPROVED' if approved else 'DENIED'}{who}: {action}"))
        if no_answer and getattr(self.broker, "last_expired", False):  # nobody answered in time: stop, do not guess
            raise _Stop("needs_attention", f"The approval for '{action}' got no answer ({note}). "
                                           "Stopped with partial progress.")
        return approved, note

    def _no_response_reason(self) -> str:
        """Why a request got no answer, as far as the broker knows (timeout, expiry, cancellation)."""
        return getattr(self.broker, "no_response_reason", "") or "no response received"

    async def _interact(self, kind: str, payload: dict) -> Any:
        """Ask the human via the broker (kind: "approval" | "question"). Returns None on timeout or cancellation."""
        payload = self._redact(payload)
        approval = kind == "approval"
        self.guards.pause_clock()  # waiting for a human does not count against the run's time limit
        self._obs.wait_started()
        self.state.status = "waiting_approval" if approval else "waiting_user"
        await self._checkpoint()

        async def announce(request_id: str) -> None:
            await self._emit("approval_requested" if approval else "question_asked", {"request_id": request_id, **payload})

        request = asyncio.ensure_future(self.broker.request(kind, payload, on_created=announce))
        cancelled = asyncio.ensure_future(self._cancelled.wait())
        response: Any = None
        try:
            await asyncio.wait({request, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if request.done() and not request.cancelled() and request.exception() is None:
                response = request.result()
        finally:
            for pending in (request, cancelled):
                pending.cancel()
            if self.state.status in ("waiting_approval", "waiting_user"):
                self.state.status = "running"
            self.guards.resume_clock()
        metrics.approval_resolved(kind, self._wait_outcome(kind, response), self._obs.wait_ended())
        resolved: dict[str, Any] = {"response": response, "answered": response is not None}
        if response is None and getattr(self.broker, "no_response_reason", ""):
            resolved["reason"] = self.broker.no_response_reason
        await self._emit("approval_resolved" if approval else "question_answered", resolved)
        return response

    def _wait_outcome(self, kind: str, response: Any) -> str:
        """How a request for a human ended, as a metric label."""
        if isinstance(response, dict) and response:
            return ("approved" if response.get("approved") else "denied") if kind == "approval" else "answered"
        if getattr(self.broker, "last_expired", False):
            return "expired"
        return "cancelled" if self._cancelled.is_set() else "no_response"

    # ------------------------------------------------------------------ finish and verification

    async def _handle_finish(self, step: Step) -> None:
        claim = self._redact(normalize_finish(step.args))
        self._claim = claim
        if claim["status"] != "success":
            await self._finish_step(self._ok(step, "Finish recorded."))
            honest = "failed" if claim["status"] == "failed" else "needs_attention"
            raise _Stop(honest, f"The agent finished with status '{claim['status']}'; no verification was run.")

        verdict = await self._verify(claim)
        self._verdict = verdict
        if verdict.passed:
            await self._finish_step(self._ok(step, "Independent verification passed."))
            raise _Stop("succeeded", "")
        if verdict.inconclusive:
            await self._finish_step(self._fail(step, "verification_failed", f"Verification inconclusive: {verdict.summary}"))
            raise _Stop("needs_attention", f"The result could not be independently verified: {verdict.summary}")
        problems = "\n".join(f"- {c['criterion']}: {c['observed']}" for c in verdict.failed_checks) or "- (no details)"
        feedback = VERIFY_FAILED_TEMPLATE.format(problems=problems, summary=verdict.summary)
        self._verify_failures += 1
        self._feedback = f"{problems}\n(Auditor summary: {verdict.summary})"
        await self._finish_step(self._fail(step, "verification_failed", feedback))  # checkpoints the two lines above
        if self._verify_failures > MAX_VERIFY_ROUNDS:
            raise _Stop("failed", f"Independent verification failed after {MAX_VERIFY_ROUNDS} correction rounds.")

    async def _verify(self, claim: dict) -> Verdict:
        self.state.status = "verifying"
        await self._checkpoint()
        await self._emit("verification_started", {"criteria": self.state.success_criteria})
        try:
            verdict = await Verifier(self.llm, self.registry, cancelled=self._cancelled).verify(
                task=self.state.task, criteria=self.state.success_criteria, claim=claim,
                facts_text=self.memory.render(), ctx=self.ctx, on_step=self._record_verify_step,
                questions=self._asked_questions(), sources=self._audit_sources(),
            )
        except CallInterrupted as err:  # cancelled while the auditor waited for its model
            raise _Stop("cancelled", "Cancelled by the user.") from err
        self.state.usage["llm_calls"] += verdict.llm_calls
        self.state.usage["input_tokens"] += verdict.input_tokens
        self.state.usage["output_tokens"] += verdict.output_tokens
        self.state.status = "running"
        await self._emit("verification_finished", {"passed": verdict.passed, "inconclusive": verdict.inconclusive,
                                                   "checks": verdict.checks, "summary": verdict.summary})
        return verdict

    def _asked_questions(self) -> list[dict]:
        """Every question the worker put to the user, with the answer (``None`` when none came): the auditor needs it to
        judge whether an ambiguity was settled with the user."""
        asked = []
        for step in self.state.steps:
            if step.kind != "ask_user":
                continue
            answered = step.ok and step.observation.startswith(ANSWER_PREFIX)
            asked.append({"question": str(step.args.get("question", "")), "options": list(step.args.get("options") or []),
                          "answer": step.observation[len(ANSWER_PREFIX):] if answered else None})
        return asked

    def _audit_sources(self) -> str:
        """What the auditor may look at, from what the run already knows: the start page and the vault credentials."""
        s = self.settings
        lines = [f"- Company intranet start page: {s.SIM_ENTRY_URL or s.SIM_BASE_URL}"]
        if self._vault_public:
            names = ", ".join(c["name"] + (f" ({c['description']})" if c.get("description") else "") for c in self._vault_public)
            lines.append(f"- Credentials in the vault (usually one per system, for list_credentials + browser_fill_secret): {names}")
        return "\n".join(lines)

    async def _record_verify_step(self, tool: str, args: dict, result: ToolResult, duration_ms: int) -> None:
        metrics.tool_call(tool, result.ok, duration_ms / 1000)
        step = Step(kind="verify", tool=tool, args=args, ok=result.ok, error=result.error, evidence=list(result.evidence),
                    observation=truncate(result.observation, STORED_OBSERVATION_LIMIT), duration_ms=duration_ms)
        await self._finish_step(step, phase="verify", feed_guards=False)

    # ------------------------------------------------------------------ guards

    async def _apply_guard(self, event: GuardEvent) -> None:
        await self._emit("guard_triggered", {"guard": event.guard, "level": event.level, "message": event.message})
        if event.level == "nudge":
            self._nudges.append(event.message)
        await self._add_step(Step(kind="system", thought=event.message, ok=False, error=event.guard, turn=self._turn))
        if event.level == "nudge":
            return
        recent = next((compress_step(s) for s in reversed(self.state.steps) if s.kind == "tool" and not s.ok), "")
        question = f"I'm stuck: {event.message} Last problem: {recent or 'none recorded'}. How should I proceed?"
        response = await self._interact("question", {"question": question, "options": ["Keep trying", "Stop the task"]})
        answer = str(response.get("answer", "")).strip() if isinstance(response, dict) else ""
        if not answer or answer.lower().startswith(_STOP_WORDS):
            raise _Stop("needs_attention", f"Stopped: {event.message} No guidance was given to continue.")
        self._nudges.append(f"The user answered your request for help: {answer}")
        self.guards.reset_after_guidance()
        await self._checkpoint()

    # ------------------------------------------------------------------ recording helpers

    @staticmethod
    def _ok(step: Step, observation: str) -> Step:
        step.ok, step.observation = True, observation
        return step

    @staticmethod
    def _fail(step: Step, error: str, observation: str) -> Step:
        step.ok, step.error, step.observation = False, error, observation
        return step

    def _redact(self, obj: Any) -> Any:
        """``obj`` with vault secrets scrubbed (unchanged when the run has no vault credentials)."""
        return self.redactor.redact_obj(obj) if self.redactor else obj

    async def _add_step(self, step: Step) -> Step:
        if self.redactor:
            self.redactor.redact_step(step)
        self.state.add_step(step)
        await self.store.append_step(self.state.run_id, step)
        await self._checkpoint()
        return step

    async def _finish_step(self, step: Step, *, phase: str = "work", feed_guards: bool = True) -> Step:
        """Store the step, feed the guards, persist, and tell the UI."""
        await self._add_step(step)
        if feed_guards:
            self.guards.record(step)
        payload = asdict(step)
        payload["observation"] = truncate(step.observation, EVENT_OBSERVATION_LIMIT)
        await self._emit("tool_finished", {**payload, "phase": phase})
        return step

    async def _checkpoint(self) -> None:
        """Persist the run header and the runtime state (what a resumed run needs besides the steps)."""
        await self.store.save_state(self.state)
        await self.store.save_runtime(self.state.run_id, self._runtime())

    def _runtime(self) -> dict:
        """The runner's hidden in-memory state as a JSON-serializable dict (the inverse of ``_restore``)."""
        return {
            "turn": self._turn,
            "text_only": self._text_only,
            "force_tool": self._force_tool,
            "nudges": list(self._nudges),
            "feedback": self._feedback,
            "verify_failures": self._verify_failures,
            "claim": self._claim,
            "verdict": asdict(self._verdict) if self._verdict else None,
            "approvals": dict(self._approvals),
            "unsourced_blocked": sorted([label, value] for label, value in self._unsourced_blocked),
            "resume_count": self._resume_count,
            "guards": self.guards.to_dict(),
            "policy": self.policy.to_dict(),
        }

    def _restore(self, runtime: dict) -> None:
        """Load a ``_runtime`` dict. Missing keys keep their defaults (a run interrupted before its first checkpoint)."""
        self._turn = runtime.get("turn", 0)
        self._text_only = runtime.get("text_only", 0)
        self._force_tool = runtime.get("force_tool", False)
        self._nudges = list(runtime.get("nudges", []))
        self._feedback = runtime.get("feedback", "")
        self._verify_failures = runtime.get("verify_failures", 0)
        self._claim = runtime.get("claim")
        self._verdict = Verdict(**runtime["verdict"]) if runtime.get("verdict") else None
        self._approvals = {**self._approvals, **runtime.get("approvals", {})}
        self._unsourced_blocked = {(label, value) for label, value in runtime.get("unsourced_blocked", [])}
        self._resume_count = runtime.get("resume_count", 0)
        self.guards = self._new_guards(runtime.get("guards"))
        if runtime.get("policy"):
            self.policy = ApprovalPolicy.from_dict(runtime["policy"])

    async def _emit(self, event_type: str, payload: dict) -> None:
        payload = {"run_id": self.state.run_id, **payload}
        if event_type == "memory_updated":
            fact = self.state.facts[payload["key"]]
            if self.redactor:
                self.redactor.redact_fact(fact)
            await self.store.upsert_fact(self.state.run_id, fact)
        payload = self._redact(payload)
        try:
            await self.store.append_event(self.state.run_id, event_type, payload)
        except Exception:  # noqa: BLE001 - the event log is observability; losing an entry must not kill the run
            log.exception("could not store event %s", event_type)
        if self._sink is None:
            return
        try:
            await self._sink(event_type, payload)
        except Exception:  # noqa: BLE001 - a broken UI must not kill the run
            log.exception("event sink failed for %s", event_type)

    async def _account(self, response: LLMResponse) -> None:
        usage = self.state.usage
        usage["llm_calls"] += 1
        usage["input_tokens"] += response.usage.input_tokens
        usage["output_tokens"] += response.usage.output_tokens
        model = getattr(self.llm, "active_model", None)
        extra: dict[str, Any] = {}
        if model:
            switches = getattr(self.llm, "model_switches", 0)
            if model != usage.get("model") and usage.get("model"):
                await self._emit("model_switched", {"from": usage["model"], "to": model})
            usage["model"], usage["model_switches"] = model, switches
            extra = {"model": model, "model_switches": switches}
        await self._emit("usage", {**usage, **extra, "llm_retries": getattr(self.llm, "retries", 0)})

    # ------------------------------------------------------------------ wrap-up

    async def _finish_run(self, status: str, reason: str) -> None:
        assert status in TERMINAL_STATUSES
        await self._close_browser()
        self._obs.finished(status)
        report = self._redact(build_report(self.state, status=status, claim=self._claim, verdict=self._verdict,
                                           reason=reason, approvals=self._approvals, run_dir=self.run_dir))
        self.state.status, self.state.final_report, self.state.finished_at = status, report, utcnow()
        await self._checkpoint()
        await self.store.save_report(self.state.run_id, report, render_markdown(self.state, report))
        await self._emit("run_finished", {"status": status, "report": report})

    async def _close_browser(self) -> None:
        try:
            from worker.tools.browser import close_browser
        except ImportError:
            return
        try:
            await close_browser(self.ctx)
        except Exception:  # noqa: BLE001 - cleanup must not mask the run result
            log.exception("closing the browser failed")
