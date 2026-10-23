"""Build the configured LLM client.

``make_llm(settings)`` is the v1 behaviour: a process-local throttle and process-local model health. In database mode
the caller also passes the run's identity and the database (``org_id``, ``user_id``, ``run_id``, ``sessionmaker``) and
gets the shared machinery wired in:

* the API key comes from the org's vault (credential ``gemini``) when it has one, else from ``GEMINI_API_KEY``
  (``resolve_llm_key``); everything shared is keyed by the SHA-256 of that key, never by the key itself;
* a ``PgRateLimiter`` (one token bucket per key, shared by every run and process) replaces the per-run throttle;
* a ``PgModelHealth`` (which models are out of quota, per key) replaces the process-global one;
* a ``UsageRecorder`` writes a ``usage_events`` row per call and a ``QuotaGuard`` refuses calls once the org's daily
  budget is used up.
"""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from worker.config import Settings
from worker.llm.base import LLMClient, LLMError
from worker.llm.failover import DEFAULT_HEALTH, ModelHealth
from worker.llm.fake import ScriptedLLM
from worker.llm.resilient import ResilientLLM
from worker.org_settings import model_chain
from worker.vault import LLM_KEY_CREDENTIAL

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from worker.vault import VaultScope

_local_health: dict[str, ModelHealth] = {}  # file mode with an org-specific key: one health per key hash


def key_hash_of(api_key: str) -> str:
    """The identifier of an API key in the database and in logs: its SHA-256 (hex). The key itself is never stored."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def resolve_llm_key(vault_scope: VaultScope | None, settings: Settings) -> tuple[str, str]:
    """``(api_key, key_hash)`` for a run: the org's own ``gemini`` vault credential (kind ``api_key``) if it has one, else
    the deployment's ``GEMINI_API_KEY``. Both are empty strings when no key is configured."""
    credential = vault_scope.internal(LLM_KEY_CREDENTIAL) if vault_scope is not None else None
    key = (credential.fields.get("api_key") or "").strip() if credential is not None and credential.kind == "api_key" else ""
    key = key or settings.GEMINI_API_KEY
    return key, (key_hash_of(key) if key else "")


def make_llm(
    settings: Settings,
    *,
    org_id: Any = None,
    user_id: Any = None,
    run_id: Any = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    vault_scope: VaultScope | None = None,
    org_settings: dict | None = None,
) -> LLMClient:
    """Create the provider named by ``settings.LLM_PROVIDER``, wrapped in ResilientLLM.

    The ``fake`` provider returns an empty ScriptedLLM (tests replace its script) with no throttling.

    ``sessionmaker`` (database mode) switches on the shared limiter and model health; with ``org_id`` it also records
    usage (attributed to ``user_id`` / ``run_id``) and enforces the org's daily budget. ``vault_scope`` supplies the org's
    own Gemini key; ``org_settings`` (the org's stored or effective settings) its ``gemini_models`` chain override.
    """
    hooks: dict[str, Any] = {}
    if sessionmaker is not None and org_id is not None:
        from worker.usage import UsageRecorder, guard_for

        guard = guard_for(sessionmaker, org_id, settings)
        hooks = {"quota": guard, "usage": UsageRecorder(sessionmaker, org_id=org_id, user_id=user_id, run_id=run_id,
                                                        guard=guard)}
    provider = settings.LLM_PROVIDER
    limiter = None
    if provider == "gemini":
        from worker.llm.failover import FailoverLLM
        from worker.llm.gemini import GeminiClient

        key, key_hash = resolve_llm_key(vault_scope, settings)
        if not key:
            raise LLMError("GEMINI_API_KEY is not set (get a free key at https://aistudio.google.com/apikey)")
        health: ModelHealth | None
        if sessionmaker is not None:
            from worker.llm.health_pg import shared_health
            from worker.llm.limiter import PgRateLimiter

            health = shared_health(sessionmaker, key_hash, settings.LLM_HEALTH_CACHE_S)
            if settings.llm_refill_per_s > 0:
                limiter = PgRateLimiter(sessionmaker, key_hash, capacity=settings.LLM_BUCKET_CAPACITY,
                                        rate_per_s=settings.llm_refill_per_s)
        else:  # file mode: the process-global health, unless this run uses a key of its own
            health = None if key == settings.GEMINI_API_KEY else _local_health.setdefault(key_hash, ModelHealth())
        inner: LLMClient = FailoverLLM(
            model_chain(org_settings, settings),
            lambda model: GeminiClient(key, model, timeout_s=settings.LLM_CALL_TIMEOUT_S), health=health)
    elif provider == "ollama":
        from worker.llm.ollama import OllamaClient

        inner = OllamaClient(settings.OLLAMA_URL, settings.OLLAMA_MODEL)
    elif provider == "fake":
        return ResilientLLM(ScriptedLLM([]), min_interval_s=0.0, **hooks)
    elif provider == "policy":
        # The load-test stand-in for a model. It sits behind the same machinery as Gemini: in database mode every run of every
        # process shares ONE token bucket (like one API key) at LLM_REFILL_PER_S calls per second, and usage is recorded.
        from worker.llm.policy import PolicyLLM

        if sessionmaker is not None and settings.llm_refill_per_s > 0:
            from worker.llm.limiter import PgRateLimiter

            limiter = PgRateLimiter(sessionmaker, key_hash_of("policy-llm"), capacity=settings.LLM_BUCKET_CAPACITY,
                                    rate_per_s=settings.llm_refill_per_s)
        return ResilientLLM(PolicyLLM(think_ms=settings.POLICY_THINK_MS), min_interval_s=0.0, limiter=limiter, **hooks)
    else:
        raise LLMError(f"Unknown LLM_PROVIDER '{provider}'")
    return ResilientLLM(
        inner, min_interval_s=settings.LLM_MIN_INTERVAL_S, network_retry_s=settings.LLM_NETWORK_RETRY_S,
        limiter=limiter, **hooks
    )


async def exhausted_models(
    settings: Settings, chain: list[str], sessionmaker: async_sessionmaker[AsyncSession] | None = None
) -> list[str]:
    """The models of ``chain`` that are out of quota right now, as the deployment's own key sees them: the shared view in
    database mode, the process-local one otherwise."""
    if sessionmaker is None or not settings.GEMINI_API_KEY:
        return DEFAULT_HEALTH.exhausted(chain)
    from worker.llm.health_pg import shared_health

    health = shared_health(sessionmaker, key_hash_of(settings.GEMINI_API_KEY), settings.LLM_HEALTH_CACHE_S)
    await health.refresh()
    return health.exhausted(chain)
