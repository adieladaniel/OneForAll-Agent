"""Resilience wrapper shared by all providers: throttling, retries, accounting, tool-arg validation."""
from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

from jsonschema import Draft202012Validator

from worker import metrics
from worker.llm.base import (
    LLMClient,
    LLMError,
    LLMResponse,
    LLMTimeoutError,
    MalformedCallError,
    Message,
    RateLimitError,
    ToolCall,
    ToolSpec,
    TransientLLMError,
    Usage,
)

log = logging.getLogger(__name__)

MAX_MALFORMED_RETRIES = 2  # re-asks after a malformed function call (a budget of its own, apart from ``max_retries``)
MALFORMED_TEMPERATURE_STEP = 0.3  # added to the temperature on each such re-ask so the sample differs
MAX_RETRY_TEMPERATURE = 1.0


class RateLimiter(Protocol):
    """Decides when a call may start. ``acquire`` returns once the caller may send its request."""

    async def acquire(self) -> None: ...


class UsageSink(Protocol):
    """Receives the token accounting of every LLM call (see ``worker/usage.py``). Must never be relied on to succeed."""

    async def record(self, *, model: str, calls: int, input_tokens: int, output_tokens: int) -> None: ...


class QuotaCheck(Protocol):
    """Called before each call; raises ``QuotaExceededError`` when the caller's budget is used up."""

    async def check(self) -> None: ...


class LocalThrottle:
    """The per-instance limiter: serializes calls and enforces a minimum interval between call starts."""

    def __init__(
        self,
        min_interval_s: float,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.min_interval_s = min_interval_s
        self._sleep = sleep
        self._clock = clock
        self._lock = asyncio.Lock()
        self._last_call_at: float | None = None

    async def acquire(self) -> None:
        async with self._lock:
            if self._last_call_at is not None:
                wait = self.min_interval_s - (self._clock() - self._last_call_at)
                if wait > 0:
                    await self._sleep(wait)
            self._last_call_at = self._clock()


def validate_tool_calls(calls: list[ToolCall], tools: list[ToolSpec]) -> dict[str, str]:
    """Return ``{call_id: error}`` for calls with an unknown tool name or args violating the JSON schema."""
    specs = {t.name: t for t in tools}
    problems: dict[str, str] = {}
    for call in calls:
        spec = specs.get(call.name)
        if spec is None:
            problems[call.id] = f"Unknown tool '{call.name}'. Available tools: {', '.join(sorted(specs))}."
            continue
        errors = sorted(Draft202012Validator(spec.parameters).iter_errors(call.args), key=lambda e: list(e.path))
        if errors:
            details = "; ".join(f"{'.'.join(map(str, e.path)) or '<args>'}: {e.message}" for e in errors[:5])
            problems[call.id] = f"Invalid arguments for '{call.name}': {details}"
    return problems


class ResilientLLM(LLMClient):
    """Wraps any LLMClient with a min-interval throttle, retry/backoff, usage stats and arg validation.

    Retries RateLimitError and TransientLLMError with exponential backoff plus jitter, honoring the
    server's ``retry_after`` hint (at most ``max_retries`` times). Connectivity errors
    (``TransientLLMError.network``: DNS, refused connection, ...) are different: they are retried on a *time
    budget* (``network_retry_s``, capped backoff up to ``max_network_delay_s``) so a run survives a few minutes
    without internet. ``on_retry(info)`` (sync or async) is called before each network wait; an exception it
    raises aborts the call (the runner uses this to honor cancellation). Tool calls that name an unknown tool or violate the tool's schema
    trigger one corrective re-prompt; if the second answer is still invalid it is returned as-is. A ``MalformedCallError``
    (the model's function call was broken) is re-asked at most ``max_malformed_retries`` times with a higher
    temperature each time; that budget is separate from ``max_retries``.

    Three optional seams serve the multi-user deployment (all default to today's behaviour):

    * ``limiter``: decides when a call may start. The default is a ``LocalThrottle`` (``min_interval_s`` between call
      starts of this instance); a shared limiter (``PgRateLimiter``) replaces it so parallel runs share one API key.
    * ``quota``: ``await quota.check()`` before each call; it raises ``QuotaExceededError`` when the org's budget is gone.
    * ``usage``: ``await usage.record(...)`` after every call that returned (or failed carrying a ``usage``). A failing
      sink is logged and ignored: accounting never fails a call.
    """

    def __init__(
        self,
        inner: LLMClient,
        *,
        min_interval_s: float = 4.0,
        max_retries: int = 5,
        base_delay_s: float = 2.0,
        max_delay_s: float = 90.0,
        network_retry_s: float = 300.0,
        max_network_delay_s: float = 30.0,
        max_malformed_retries: int = MAX_MALFORMED_RETRIES,
        on_retry: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        limiter: RateLimiter | None = None,
        quota: QuotaCheck | None = None,
        usage: UsageSink | None = None,
    ):
        self.inner = inner
        self.name = inner.name
        self.min_interval_s = min_interval_s
        self.max_retries = max_retries
        self.base_delay_s = base_delay_s
        self.max_delay_s = max_delay_s
        self.network_retry_s = network_retry_s
        self.max_network_delay_s = max_network_delay_s
        self.max_malformed_retries = max_malformed_retries
        self.on_retry = on_retry
        self._sleep = sleep
        self._clock = clock
        self.limiter: RateLimiter = limiter or LocalThrottle(min_interval_s, sleep=sleep, clock=clock)
        self.quota = quota
        self.usage = usage
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.retries = 0

    @property
    def active_model(self) -> str | None:
        """The model currently serving calls, when the wrapped client reports one (failover clients do)."""
        return getattr(self.inner, "active_model", None)

    @property
    def model_switches(self) -> int:
        return getattr(self.inner, "model_switches", 0)

    @property
    def stats(self) -> dict[str, Any]:
        """Cumulative usage counters (plus ``model`` / ``model_switches`` when the wrapped client has a model chain)."""
        stats: dict[str, Any] = {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "retries": self.retries,
        }
        if self.active_model is not None:
            stats["model"] = self.active_model
            stats["model_switches"] = self.model_switches
        return stats

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.2,
        tool_choice: Literal["auto", "any", "none"] = "auto",
    ) -> LLMResponse:
        response = await self._call(messages, tools, temperature, tool_choice)
        if not tools or tool_choice == "none" or not response.tool_calls:
            return response
        problems = validate_tool_calls(response.tool_calls, tools)
        if not problems:
            return response

        repair = list(messages) + [Message(role="assistant", content=response.text, tool_calls=response.tool_calls)]
        for call in response.tool_calls:
            error = problems.get(call.id, "Not executed because another call in this turn was invalid. Re-issue it.")
            repair.append(Message(role="tool", content=f"ERROR: {error}", tool_call_id=call.id, name=call.name))
        second = await self._call(repair, tools, temperature, tool_choice)
        second.usage = Usage(
            input_tokens=response.usage.input_tokens + second.usage.input_tokens,
            output_tokens=response.usage.output_tokens + second.usage.output_tokens,
        )
        return second

    async def _call(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        temperature: float,
        tool_choice: Literal["auto", "any", "none"],
    ) -> LLMResponse:
        if self.quota is not None:
            await self.quota.check()
        try:
            return await self._call_with_retries(messages, tools, temperature, tool_choice)
        except LLMError as err:
            await self._record_failed(err)
            raise

    async def _call_with_retries(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        temperature: float,
        tool_choice: Literal["auto", "any", "none"],
    ) -> LLMResponse:
        attempt = 0
        malformed = 0
        net_attempts = 0
        net_since: float | None = None  # when the current network outage was first seen
        while True:
            await self._throttle()
            attempt_started = self._clock()
            try:
                response = await self.inner.complete(messages, tools, temperature=temperature, tool_choice=tool_choice)
            except MalformedCallError:
                # The same prompt may fail the same way, so each re-ask samples hotter; the budget is its own.
                metrics.llm_call(self._model_label(), "malformed", self._clock() - attempt_started)
                if malformed >= self.max_malformed_retries:
                    raise
                malformed += 1
                self.retries += 1
                temperature = max(temperature, min(MAX_RETRY_TEMPERATURE, temperature + MALFORMED_TEMPERATURE_STEP))
                continue
            except (RateLimitError, TransientLLMError) as err:
                metrics.llm_call(self._model_label(), "rate_limited" if isinstance(err, RateLimitError)
                                 else "timeout" if isinstance(err, LLMTimeoutError)
                                 else "network" if err.network else "transient", self._clock() - attempt_started)
                if isinstance(err, TransientLLMError) and err.network:
                    now = self._clock()
                    net_since = now if net_since is None else net_since
                    waited = now - net_since
                    if waited >= self.network_retry_s:
                        raise
                    delay = min(self._network_backoff(net_attempts), self.network_retry_s - waited)
                    net_attempts += 1
                    self.retries += 1
                    await self._notify_retry({
                        "kind": "network", "attempt": net_attempts, "retry_in_s": round(delay, 1),
                        "waited_s": round(waited, 1), "budget_s": self.network_retry_s, "error": str(err),
                    })
                    await self._sleep(delay)
                    continue
                if attempt >= self.max_retries:
                    raise
                self.retries += 1
                await self._sleep(self._backoff(attempt, err.retry_after))
                attempt += 1
                continue
            except LLMError:
                metrics.llm_call(self._model_label(), "error", self._clock() - attempt_started)
                raise
            metrics.llm_call(self._model_label(), "ok", self._clock() - attempt_started)
            self.calls += 1
            self.input_tokens += response.usage.input_tokens
            self.output_tokens += response.usage.output_tokens
            await self._record(response.usage)
            return response

    async def _throttle(self) -> None:
        """Wait until the limiter lets this call start."""
        started = self._clock()
        await self.limiter.acquire()
        metrics.LLM_RATELIMIT_WAIT.observe(max(0.0, self._clock() - started))

    def _model_label(self) -> str:
        """The model name used as a metric label (the model serving calls, else the client's name)."""
        return str(self.active_model or getattr(self.inner, "model", None) or self.name)

    async def _record(self, usage: Usage) -> None:
        """Hand one call's token counts to the usage sink; a failing sink is logged, never raised."""
        metrics.llm_tokens(self._model_label(), usage.input_tokens, usage.output_tokens)
        if self.usage is None:
            return
        model = self._model_label()
        try:
            await self.usage.record(model=str(model), calls=1, input_tokens=usage.input_tokens,
                                    output_tokens=usage.output_tokens)
        except Exception as exc:  # noqa: BLE001 - accounting must never fail the call
            log.warning("could not record LLM usage (%s)", type(exc).__name__)

    async def _record_failed(self, err: LLMError) -> None:
        """A call that failed but still consumed tokens (an error that carries a ``usage``) is accounted too."""
        usage = getattr(err, "usage", None)
        if isinstance(usage, Usage) and (usage.input_tokens or usage.output_tokens):
            await self._record(usage)

    async def _notify_retry(self, info: dict[str, Any]) -> None:
        if self.on_retry is None:
            return
        result = self.on_retry(info)
        if inspect.isawaitable(result):
            await result

    def _network_backoff(self, attempt: int) -> float:
        return min(self.base_delay_s * 2 ** min(attempt, 20), self.max_network_delay_s) * random.uniform(0.5, 1.0)

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after + random.uniform(0.0, 1.0), self.max_delay_s)
        return min(self.base_delay_s * 2**attempt, self.max_delay_s) * random.uniform(0.5, 1.0)
