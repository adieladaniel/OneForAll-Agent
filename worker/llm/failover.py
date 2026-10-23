"""Model failover: walk a chain of models, skipping the ones whose quota is gone.

The Gemini free tier allows only a handful of requests per day *per model*, while one agent run needs dozens
of calls, so waiting is useless: when a model reports a daily quota error the run continues on the next model
(mid-run; the conversation is rebuilt from run state every turn, so nothing else has to change).

* daily quota (QuotaExhaustedError)  -> model is skipped until the next quota reset (midnight Pacific time);
* 404 / persistent 503               -> model is skipped (until reset / for a short cooldown);
* a single 503                       -> one quick retry on the same model first;
* a call that times out (LLMTimeoutError, see LLM_CALL_TIMEOUT_S) is not retried: the model is marked temporarily
  unavailable (never as out of quota) and the next model answers the call;
* a malformed function call          -> the next model answers the same call; nothing is marked unusable;
* connectivity errors (DNS, refused connection) never mark a model: they propagate untouched;
* per-minute rate limits and other errors propagate to ResilientLLM (wait and retry the same model).

The memory of unusable models lives in a ``ModelHealth`` that is shared by the whole process (the dashboard
creates a new client for every run). In database mode it is a ``PgModelHealth`` (worker/llm/health_pg.py) shared by
every process; ``refresh()`` / ``flush()`` are its async hooks and no-ops here.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Literal

from worker import metrics
from worker.llm.base import (
    LLMClient,
    LLMError,
    LLMResponse,
    LLMTimeoutError,
    MalformedCallError,
    Message,
    ModelUnavailableError,
    QuotaExhaustedError,
    ToolSpec,
    TransientLLMError,
)

log = logging.getLogger(__name__)

ALL_EXHAUSTED_MESSAGE = (
    "All configured Gemini models are out of free-tier quota for today; set GEMINI_MODELS or try tomorrow"
)
UNAVAILABLE_COOLDOWN_S = 60.0


def next_quota_reset(now: float) -> float:
    """Epoch seconds of the next daily quota reset (Google resets per-day quotas at midnight Pacific time)."""
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/Los_Angeles")
    except Exception:  # noqa: BLE001 - no tz database (common on Windows without tzdata): use fixed PST
        tz = timezone(timedelta(hours=-8))
    local = datetime.fromtimestamp(now, tz=tz)
    return (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


class ModelHealth:
    """Which models are currently unusable, and why. Entries expire on their own (the next day for quota)."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._bad: dict[str, tuple[float, str]] = {}  # model -> (usable again at, "quota" | "missing" | "unavailable")

    def mark_exhausted(self, model: str) -> None:
        self._bad[model] = (next_quota_reset(self._clock()), "quota")

    def mark_missing(self, model: str) -> None:
        self._bad[model] = (next_quota_reset(self._clock()), "missing")

    def mark_unavailable(self, model: str, cooldown_s: float = UNAVAILABLE_COOLDOWN_S) -> None:
        self._bad[model] = (self._clock() + cooldown_s, "unavailable")

    def reason(self, model: str) -> str | None:
        """Why ``model`` is skipped right now (``None`` when it may be used)."""
        entry = self._bad.get(model)
        if entry is None:
            return None
        until, why = entry
        if self._clock() >= until:
            del self._bad[model]
            return None
        return why

    def retry_in(self, model: str) -> float:
        entry = self._bad.get(model)
        return max(0.0, entry[0] - self._clock()) if entry else 0.0

    def exhausted(self, models: list[str]) -> list[str]:
        """The subset of ``models`` that are out of quota or missing (skipped for the day)."""
        return [m for m in models if self.reason(m) in ("quota", "missing")]

    def first_usable(self, models: list[str]) -> str | None:
        return next((m for m in models if self.reason(m) is None), None)

    def clear(self) -> None:
        self._bad.clear()

    async def refresh(self) -> None:
        """Bring the in-memory view up to date with the shared store (nothing to do for a process-local health)."""

    async def flush(self) -> None:
        """Wait until changes made through ``mark_*`` reached the shared store (nothing to do here)."""


DEFAULT_HEALTH = ModelHealth()  # shared by every FailoverLLM in the process unless a test passes its own


def active_model(models: list[str], health: ModelHealth | None = None) -> str:
    """The model the next call would start with (the first one not known to be unusable)."""
    health = health or DEFAULT_HEALTH
    return health.first_usable(models) or (models[0] if models else "")


class FailoverLLM(LLMClient):
    """Tries ``models`` in order; each model gets its own client from ``factory`` (so signatures stay per model)."""

    name = "gemini"

    def __init__(
        self,
        models: list[str],
        factory: Callable[[str], LLMClient],
        *,
        health: ModelHealth | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        quick_retry_s: float = 1.5,
        unavailable_cooldown_s: float = UNAVAILABLE_COOLDOWN_S,
    ):
        if not models:
            raise LLMError("No Gemini models configured (set GEMINI_MODELS)")
        self.models = list(models)
        self._factory = factory
        self.health = health or DEFAULT_HEALTH
        self._sleep = sleep
        self.quick_retry_s = quick_retry_s
        self.unavailable_cooldown_s = unavailable_cooldown_s
        self._clients: dict[str, LLMClient] = {}
        self._used: str | None = None
        self.model_switches = 0

    @property
    def active_model(self) -> str:
        """The model that served the latest call, or the one the next call will start with."""
        return self._used or active_model(self.models, self.health)

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        temperature: float = 0.2,
        tool_choice: Literal["auto", "any", "none"] = "auto",
    ) -> LLMResponse:
        await self.health.refresh()
        malformed: MalformedCallError | None = None
        for model in self.models:
            if self.health.reason(model) is not None:
                continue
            client = self._clients.get(model) or self._clients.setdefault(model, self._factory(model))
            try:
                response = await self._attempt(client, model, messages, tools, temperature, tool_choice)
            except MalformedCallError as err:
                # A broken reply is a per-response glitch, not an outage: ask the next model, leave this one usable.
                log.warning("model %s returned a malformed function call, trying the next model", model)
                malformed = malformed or err
                continue
            except QuotaExhaustedError as err:
                log.warning("model %s: daily quota exhausted, failing over", model)
                self.health.mark_exhausted(model)
                await self.health.flush()
                err.model = err.model or model
                continue
            except ModelUnavailableError as err:
                log.warning("model %s unavailable (%s), failing over", model, err)
                if err.temporary:
                    self.health.mark_unavailable(model, self.unavailable_cooldown_s)
                else:
                    self.health.mark_missing(model)
                await self.health.flush()
                continue
            self._note_used(model)
            return response
        if malformed is not None:  # every usable model glitched: let ResilientLLM re-ask (the models are all still healthy)
            raise malformed
        raise self._nothing_usable()

    async def _attempt(self, client, model, messages, tools, temperature, tool_choice) -> LLMResponse:
        """One call; a 503 gets a single quick retry and then counts as the model being unavailable. A call that timed out
        is not retried (it already cost the whole timeout): the model is unavailable at once."""
        try:
            return await client.complete(messages, tools, temperature=temperature, tool_choice=tool_choice)
        except TransientLLMError as err:
            if not err.unavailable or err.network:
                raise
            if isinstance(err, LLMTimeoutError):
                self._note_timeout(model, err)
                raise ModelUnavailableError(f"{model} did not answer in time: {err}", model=model, temporary=True) from err
            await self._sleep(self.quick_retry_s)
        try:
            return await client.complete(messages, tools, temperature=temperature, tool_choice=tool_choice)
        except TransientLLMError as err:
            if not err.unavailable or err.network:
                raise
            self._note_timeout(model, err)  # the retry itself may have hung
            raise ModelUnavailableError(f"{model} is overloaded: {err}", model=model, temporary=True) from err

    @staticmethod
    def _note_timeout(model: str, err: TransientLLMError) -> None:
        """A timed-out attempt never reaches ResilientLLM (the next model answers instead), so it is counted here."""
        if isinstance(err, LLMTimeoutError):
            log.warning("model %s did not answer in time (%s)", model, err)
            metrics.llm_call(model, "timeout", err.timeout_s)

    def _note_used(self, model: str) -> None:
        if self._used is not None and self._used != model:
            self.model_switches += 1
            log.warning("active model switched: %s -> %s", self._used, model)
        elif self._used is None:
            log.info("active model: %s", model)
        self._used = model

    def _nothing_usable(self) -> LLMError:
        cooling = [m for m in self.models if self.health.reason(m) == "unavailable"]
        if cooling:  # only temporarily overloaded: ResilientLLM may back off and come back
            wait = min(self.health.retry_in(m) for m in cooling)
            return TransientLLMError(
                f"No Gemini model is available right now (overloaded: {', '.join(cooling)})", retry_after=wait
            )
        gone = self.health.exhausted(self.models)
        return QuotaExhaustedError(f"{ALL_EXHAUSTED_MESSAGE} (tried: {', '.join(gone or self.models)})")
