"""Shared LLM limits (M7): the PostgreSQL rate limiter and model health, usage accounting, per-org budgets and keys,
and the usage endpoints. Database tests run against ``oneforall_test`` with fake LLM clients (no network)."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from dbapp import blocked, call, make_settings, new_client, reply, sign_in, small_registry
from sqlalchemy import select, text

from worker.agent.broker import AutoBroker
from worker.agent.runner import AgentRunner
from worker.config import Settings
from worker.db.models import ModelHealth as ModelHealthRow
from worker.db.models import Org, UsageEvent
from worker.db.session import make_sessionmaker
from worker.llm.base import (
    LLMError,
    LLMResponse,
    Message,
    QuotaExceededError,
    QuotaExhaustedError,
    Usage,
)
from worker.llm.factory import exhausted_models, key_hash_of, make_llm, resolve_llm_key
from worker.llm.failover import DEFAULT_HEALTH, FailoverLLM, ModelHealth, next_quota_reset
from worker.llm.fake import ScriptedLLM
from worker.llm.health_pg import PgModelHealth, shared_health
from worker.llm.limiter import PgRateLimiter
from worker.llm.resilient import LocalThrottle, ResilientLLM
from worker.store_pg import PgRunStore
from worker.usage import QuotaGuard, UsageRecorder, usage_report, utc_day_start
from worker.vault import Vault, VaultScope, generate_key
from worker.vault_pg import PgVaultBackend

ORG_KEY = "AIzaSyORG-secret-key-0123456789"
GLOBAL_KEY = "AIzaSyGLOBAL-key-9876543210"


def resp(text_: str = "ok", inp: int = 10, out: int = 5) -> LLMResponse:
    return LLMResponse(text=text_, tool_calls=[], usage=Usage(inp, out))


async def no_sleep(_s: float) -> None:
    return None


# ---------------------------------------------------------------- the seams of ResilientLLM (no database)


class CountingLimiter:
    def __init__(self) -> None:
        self.acquired = 0

    async def acquire(self) -> None:
        self.acquired += 1


async def test_a_custom_limiter_replaces_the_local_throttle_and_is_used_for_every_attempt():
    from worker.llm.base import RateLimitError

    class Flaky(ScriptedLLM):
        errors = [RateLimitError("429", retry_after=0.0)]

        async def complete(self, messages, tools=None, **kw):
            if self.errors:
                raise self.errors.pop(0)
            return await super().complete(messages, tools, **kw)

    limiter = CountingLimiter()
    llm = ResilientLLM(Flaky([resp()]), min_interval_s=999.0, limiter=limiter, sleep=no_sleep)
    await llm.complete([Message("user", "hi")])
    assert limiter.acquired == 2  # the failed attempt and the retry each wait their turn; min_interval_s is ignored


async def test_the_default_limiter_is_the_local_throttle():
    llm = ResilientLLM(ScriptedLLM([]), min_interval_s=4.0)
    assert isinstance(llm.limiter, LocalThrottle) and llm.quota is None and llm.usage is None


async def test_quota_check_runs_before_the_call_and_stops_it():
    class Over:
        async def check(self) -> None:
            raise QuotaExceededError("over budget")

    inner = ScriptedLLM([resp()])
    llm = ResilientLLM(inner, min_interval_s=0.0, quota=Over())
    with pytest.raises(QuotaExceededError, match="over budget"):
        await llm.complete([Message("user", "hi")])
    assert inner.calls == [] and llm.calls == 0


async def test_usage_sink_receives_each_call_and_its_failure_never_breaks_the_call(caplog):
    seen: list[dict] = []

    class Sink:
        async def record(self, **kw) -> None:
            seen.append(kw)
            if len(seen) == 2:
                raise RuntimeError("database is down")

    llm = ResilientLLM(ScriptedLLM([resp(inp=7, out=3), resp(inp=1, out=1), resp()]), min_interval_s=0.0, usage=Sink())
    with caplog.at_level(logging.WARNING, logger="worker.llm.resilient"):
        for _ in range(3):
            assert (await llm.complete([Message("user", "x")])).text == "ok"
    assert seen[0] == {"model": "fake", "calls": 1, "input_tokens": 7, "output_tokens": 3}
    assert len(seen) == 3 and llm.calls == 3
    assert "could not record LLM usage" in caplog.text


async def test_a_failed_call_that_carries_usage_is_accounted():
    class Costly(LLMError):
        usage = Usage(40, 2)

    class Failing(ScriptedLLM):
        async def complete(self, messages, tools=None, **kw):
            raise Costly("cut off")

    seen: list[dict] = []

    class Sink:
        async def record(self, **kw) -> None:
            seen.append(kw)

    llm = ResilientLLM(Failing([]), min_interval_s=0.0, usage=Sink())
    with pytest.raises(Costly):
        await llm.complete([Message("user", "x")])
    assert seen == [{"model": "fake", "calls": 1, "input_tokens": 40, "output_tokens": 2}]


# ---------------------------------------------------------------- the shared rate limiter


async def test_five_runs_sharing_a_key_respect_the_bucket_rate(pg_sessionmaker):
    capacity, rate = 2, 20.0  # a burst of 2, then one call per 50 ms
    starts: list[float] = []

    async def one_run() -> None:
        limiter = PgRateLimiter(pg_sessionmaker, "key-shared", capacity=capacity, rate_per_s=rate)  # one per "run"
        await limiter.acquire()
        starts.append(time.monotonic())

    await asyncio.gather(*(one_run() for _ in range(5)))
    starts.sort()
    for i in range(capacity, 5):
        earliest = starts[0] + (i - capacity + 1) / rate
        assert starts[i] >= earliest - 0.03, f"call {i} started {earliest - starts[i]:.3f}s too early"
    assert starts[-1] - starts[0] < 1.5  # and nobody waits unreasonably long


async def test_limiters_of_different_keys_do_not_block_each_other(pg_sessionmaker):
    slow = PgRateLimiter(pg_sessionmaker, "key-a", capacity=1, rate_per_s=2.0)
    other = PgRateLimiter(pg_sessionmaker, "key-b", capacity=1, rate_per_s=2.0)
    await slow.acquire()  # key-a is now empty: the next call on it waits about 0.5 s
    waiting = asyncio.create_task(slow.acquire())
    started = time.monotonic()
    await other.acquire()
    assert time.monotonic() - started < 0.25
    assert not waiting.done()
    await waiting


async def test_two_limiter_instances_share_one_bucket_like_two_processes(pg_required_engine, pg_sessionmaker):
    process_a = PgRateLimiter(pg_sessionmaker, "key-x", capacity=1, rate_per_s=10.0)
    process_b = PgRateLimiter(make_sessionmaker(pg_required_engine), "key-x", capacity=1, rate_per_s=10.0)
    await process_a.acquire()
    started = time.monotonic()
    await process_b.acquire()  # the token A took is not available to B
    assert time.monotonic() - started >= 0.07


async def test_the_bucket_refills_by_the_database_clock_and_caps_at_capacity(pg_sessionmaker):
    limiter = PgRateLimiter(pg_sessionmaker, "key-clock", capacity=2, rate_per_s=10.0)
    await limiter.acquire()
    await limiter.acquire()
    async with pg_sessionmaker() as session:
        row = (await session.execute(text("select tokens, clock_timestamp() - updated_at < interval '5 seconds' "
                                          "from llm_limits where key_hash = 'key-clock'"))).one()
    assert row[0] < 1.0 and row[1]  # updated_at was written by the database, not by the process
    await asyncio.sleep(0.5)  # 5 tokens' worth of time: the bucket may hold only 2
    started = time.monotonic()
    await limiter.acquire()
    await limiter.acquire()
    assert time.monotonic() - started < 0.1  # a full burst is available again
    third = time.monotonic()
    await limiter.acquire()
    assert time.monotonic() - third >= 0.05  # but not more than the capacity


async def test_the_limiter_degrades_to_a_local_throttle_when_the_database_fails(caplog):
    def broken():
        raise ConnectionError("db down")

    sleeps: list[float] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    limiter = PgRateLimiter(broken, "k", capacity=2, rate_per_s=0.5, sleep=sleep)
    with caplog.at_level(logging.WARNING, logger="worker.llm.limiter"):
        await limiter.acquire()  # first call: no wait
        await limiter.acquire()  # second: the local fallback enforces 1/rate seconds
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 2.0
    assert "shared rate limiter unavailable" in caplog.text


# ---------------------------------------------------------------- shared model health


class StubModel:
    name = "stub"

    def __init__(self, model: str, items=()):
        self.model, self.items, self.calls = model, list(items), 0

    async def complete(self, messages, tools=None, **kwargs):
        self.calls += 1
        item = self.items.pop(0) if self.items else resp(f"from {self.model}")
        if isinstance(item, Exception):
            raise item
        return item


def failover(health, plan: dict) -> tuple[FailoverLLM, dict[str, StubModel]]:
    clients = {m: StubModel(m, items) for m, items in plan.items()}
    return FailoverLLM(list(plan), lambda m: clients[m], health=health, sleep=no_sleep), clients


class Ticker:
    """A controllable clock (used both as wall clock and as monotonic cache clock)."""

    def __init__(self, start: float) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


async def test_an_exhausted_model_is_seen_by_the_other_process_within_the_cache_window(pg_required_engine, pg_sessionmaker):
    wall = Ticker(1_800_000_000.0)
    cache_a, cache_b = Ticker(0.0), Ticker(0.0)
    process_a = PgModelHealth(pg_sessionmaker, "key-h", clock=wall, cache_ttl_s=3.0, monotonic=cache_a)
    process_b = PgModelHealth(make_sessionmaker(pg_required_engine), "key-h", clock=wall, cache_ttl_s=3.0, monotonic=cache_b)

    llm_b, clients_b = failover(process_b, {"a": [], "b": []})
    await llm_b.complete([Message("user", "warm up")])  # B reads the (empty) table: its cache is now fresh
    assert clients_b["a"].calls == 1

    llm_a, clients_a = failover(process_a, {"a": [QuotaExhaustedError("20/day", model="a")], "b": []})
    assert (await llm_a.complete([Message("user", "x")])).text == "from b"  # A fails over and writes through
    async with pg_sessionmaker() as session:
        row = (await session.scalars(select(ModelHealthRow).where(ModelHealthRow.model == "a"))).one()
    assert row.status == "quota" and row.key_hash == "key-h"

    cache_b.now += 1.0  # still inside B's cache window: B has not looked again
    await llm_b.complete([Message("user", "y")])
    assert clients_b["a"].calls == 2

    cache_b.now += 3.0  # the window passed: B re-reads the table and skips the exhausted model
    assert (await llm_b.complete([Message("user", "z")])).text == "from b"
    assert clients_b["a"].calls == 2
    assert process_b.reason("a") == "quota" and process_b.exhausted(["a", "b"]) == ["a"]


async def test_exhaustion_resets_at_the_next_pacific_midnight(pg_sessionmaker):
    start = 1_800_000_000.0
    wall = Ticker(start)
    writer = PgModelHealth(pg_sessionmaker, "key-reset", clock=wall, cache_ttl_s=0.0)
    writer.mark_exhausted("a")
    writer.mark_unavailable("b", 60.0)
    await writer.flush()

    wall.now = start + 3 * 3600
    reader = PgModelHealth(pg_sessionmaker, "key-reset", clock=wall, cache_ttl_s=0.0)
    await reader.refresh()
    assert reader.reason("a") == "quota" and reader.reason("b") is None  # the 60 s cooldown of b is over
    assert reader.retry_in("a") == pytest.approx(next_quota_reset(start) - wall.now)

    wall.now = next_quota_reset(start) + 1
    fresh = PgModelHealth(pg_sessionmaker, "key-reset", clock=wall, cache_ttl_s=0.0)
    await fresh.refresh()
    assert fresh.reason("a") is None
    llm, clients = failover(fresh, {"a": [], "b": []})
    assert (await llm.complete([Message("user", "hi")])).text == "from a"  # the strongest model is back in front


async def test_health_is_per_key_and_clear_removes_it(pg_sessionmaker):
    one = PgModelHealth(pg_sessionmaker, "key-1", cache_ttl_s=0.0)
    two = PgModelHealth(pg_sessionmaker, "key-2", cache_ttl_s=0.0)
    one.mark_missing("ghost")
    await one.flush()
    await two.refresh()
    assert two.reason("ghost") is None  # another key's quota is another quota
    again = PgModelHealth(pg_sessionmaker, "key-1", cache_ttl_s=0.0)
    await again.refresh()
    assert again.reason("ghost") == "missing"
    one.clear()
    await one.flush()
    await again.refresh()
    assert again.reason("ghost") is None


async def test_health_survives_database_trouble_and_shares_one_instance_per_key(pg_sessionmaker, caplog):
    def broken():
        raise ConnectionError("db down")

    health = PgModelHealth(broken, "k")
    health.mark_exhausted("a")  # cached at once
    with caplog.at_level(logging.WARNING, logger="worker.llm.health_pg"):
        await health.flush()
        await health.refresh()
    assert health.reason("a") == "quota"  # the process keeps its own view
    assert "could not write the shared model health" in caplog.text and "could not read" in caplog.text
    assert shared_health(pg_sessionmaker, "k1") is shared_health(pg_sessionmaker, "k1")
    assert shared_health(pg_sessionmaker, "k1") is not shared_health(pg_sessionmaker, "k2")


async def test_unflushed_local_changes_survive_a_refresh(pg_sessionmaker):
    health = PgModelHealth(pg_sessionmaker, "key-dirty", cache_ttl_s=0.0)
    health._pending.append(("set", "a", "quota", time.time() + 3600))  # a change whose write is still in flight
    health._bad["a"] = (time.time() + 3600, "quota")
    await health.refresh()
    assert health.reason("a") == "quota"
    await health.flush()
    assert health._pending == []


# ---------------------------------------------------------------- usage accounting


async def seed_org(sm, slug: str = "acme", settings: dict | None = None):
    from worker.db.models import User

    async with sm() as session, session.begin():
        org = Org(slug=slug, name=slug.title(), settings=settings or {})
        user = User(email=f"u@{slug}.test", password_hash="x")
        session.add_all([org, user])
    return org.id, user.id


async def new_run(sm, org_id, user_id) -> str:
    from worker.agent.state import RunState

    run_id = uuid.uuid4().hex
    await PgRunStore(sm, org_id, user_id, artifacts_root="runs-test").create_run(RunState(run_id=run_id, task="t"))
    return run_id


async def all_usage(sm) -> list[UsageEvent]:
    async with sm() as session:
        return list((await session.scalars(select(UsageEvent).order_by(UsageEvent.id))).all())


async def test_every_call_writes_a_usage_row_with_org_user_run_and_model(pg_sessionmaker):
    org_id, user_id = await seed_org(pg_sessionmaker)
    run_id = await new_run(pg_sessionmaker, org_id, user_id)
    recorder = UsageRecorder(pg_sessionmaker, org_id=org_id, user_id=user_id, run_id=run_id)
    inner = FailoverLLM(["m1", "m2"], lambda m: StubModel(m, [resp(inp=11, out=4), resp(inp=2, out=1)]),
                        health=ModelHealth(), sleep=no_sleep)
    llm = ResilientLLM(inner, min_interval_s=0.0, usage=recorder)
    await llm.complete([Message("user", "a")])
    await llm.complete([Message("user", "b")])
    rows = await all_usage(pg_sessionmaker)
    assert [(r.org_id, r.user_id, str(r.run_id).replace("-", ""), r.model, r.calls, r.input_tokens, r.output_tokens)
            for r in rows] == [(org_id, user_id, run_id, "m1", 1, 11, 4), (org_id, user_id, run_id, "m1", 1, 2, 1)]


async def test_a_run_that_is_not_stored_still_counts_for_the_org(pg_sessionmaker):
    org_id, user_id = await seed_org(pg_sessionmaker)
    recorder = UsageRecorder(pg_sessionmaker, org_id=org_id, user_id=user_id, run_id=uuid.uuid4())  # no such run row
    await recorder.record(model="m", calls=1, input_tokens=5, output_tokens=5)
    (row,) = await all_usage(pg_sessionmaker)
    assert row.org_id == org_id and row.run_id is None and row.input_tokens == 5


async def test_accounting_failures_do_not_break_the_llm_call(caplog):
    def broken():
        raise ConnectionError("db down")

    recorder = UsageRecorder(broken, org_id=uuid.uuid4(), user_id=uuid.uuid4(), run_id=uuid.uuid4())
    llm = ResilientLLM(ScriptedLLM([resp("fine")]), min_interval_s=0.0, usage=recorder)
    with caplog.at_level(logging.WARNING, logger="worker.llm.resilient"):
        assert (await llm.complete([Message("user", "x")])).text == "fine"
    assert "could not record LLM usage (ConnectionError)" in caplog.text


# ---------------------------------------------------------------- per-org daily budget


async def add_usage(sm, org_id, user_id=None, *, tokens=0, calls=1, ts=None, model="m") -> None:
    async with sm() as session, session.begin():
        session.add(UsageEvent(org_id=org_id, user_id=user_id, model=model, calls=calls, input_tokens=tokens,
                               output_tokens=0, **({"ts": ts} if ts else {})))


async def test_the_guard_raises_a_clear_error_when_the_token_budget_is_used_up(pg_sessionmaker):
    org_id, user_id = await seed_org(pg_sessionmaker, settings={"max_tokens_per_day": 1000})
    other_org, _ = await seed_org(pg_sessionmaker, "globex")
    guard = QuotaGuard(pg_sessionmaker, org_id, ttl_s=0.0)
    await guard.check()  # nothing used yet
    await add_usage(pg_sessionmaker, org_id, user_id, tokens=600)
    await add_usage(pg_sessionmaker, other_org, tokens=5000)  # another org's spending is not ours
    await add_usage(pg_sessionmaker, org_id, tokens=5000, ts=utc_day_start() - timedelta(hours=1))  # yesterday's neither
    await guard.check()
    await add_usage(pg_sessionmaker, org_id, user_id, tokens=400)
    with pytest.raises(QuotaExceededError) as err:
        await guard.check()
    assert str(err.value) == "Organization daily LLM budget reached (1,000 of 1,000 tokens); resets at 00:00 UTC"


async def test_the_guard_enforces_the_call_budget_and_zero_means_unlimited(pg_sessionmaker):
    org_id, _ = await seed_org(pg_sessionmaker, settings={"max_llm_calls_per_day": 3, "max_tokens_per_day": 0})
    guard = QuotaGuard(pg_sessionmaker, org_id, ttl_s=0.0)
    await add_usage(pg_sessionmaker, org_id, tokens=10**9, calls=2)  # a billion tokens: unlimited tokens
    await guard.check()
    await add_usage(pg_sessionmaker, org_id, calls=1)
    with pytest.raises(QuotaExceededError, match=r"\(3 of 3 LLM calls\)"):
        await guard.check()


async def test_the_guard_caches_totals_and_counts_its_own_calls_at_once(pg_sessionmaker):
    org_id, user_id = await seed_org(pg_sessionmaker, settings={"max_llm_calls_per_day": 2})
    clock = Ticker(0.0)
    guard = QuotaGuard(pg_sessionmaker, org_id, ttl_s=5.0, monotonic=clock)
    await guard.check()  # loads: 0 of 2
    await add_usage(pg_sessionmaker, org_id, calls=2)  # another process spends the budget
    await guard.check()  # cached: not seen yet
    clock.now += 6
    with pytest.raises(QuotaExceededError):
        await guard.check()  # refreshed

    other, _ = await seed_org(pg_sessionmaker, "globex", settings={"max_llm_calls_per_day": 2})
    guard2 = QuotaGuard(pg_sessionmaker, other, ttl_s=999.0)
    recorder = UsageRecorder(pg_sessionmaker, org_id=other, guard=guard2)
    await guard2.check()
    await recorder.record(model="m", calls=1, input_tokens=1, output_tokens=1)
    await recorder.record(model="m", calls=1, input_tokens=1, output_tokens=1)
    with pytest.raises(QuotaExceededError):  # this process's own calls count without a refresh
        await guard2.check()


async def test_the_guard_lets_calls_through_when_usage_cannot_be_read(caplog):
    def broken():
        raise ConnectionError("db down")

    guard = QuotaGuard(broken, uuid.uuid4())
    with caplog.at_level(logging.WARNING, logger="worker.usage"):
        await guard.check()
    assert "not enforcing the budget" in caplog.text


async def test_a_run_ends_needs_attention_with_the_budget_message(pg_sessionmaker, tmp_path):
    org_id, user_id = await seed_org(pg_sessionmaker, settings={"max_llm_calls_per_day": 1})
    run_id = uuid.uuid4().hex
    guard = QuotaGuard(pg_sessionmaker, org_id, ttl_s=0.0)
    recorder = UsageRecorder(pg_sessionmaker, org_id=org_id, user_id=user_id, run_id=run_id, guard=guard)
    llm = ResilientLLM(ScriptedLLM([reply("Looking.", call("list_files")), blocked()]), min_interval_s=0.0,
                       quota=guard, usage=recorder)
    settings = make_settings(tmp_path)
    store = PgRunStore(pg_sessionmaker, org_id, user_id, artifacts_root=tmp_path / "runs")
    state = await AgentRunner("List files", llm, small_registry(settings), settings, AutoBroker.approve_all(),
                              run_id=run_id, store=store).run()
    assert state.status == "needs_attention"
    summary = state.final_report["summary"]
    assert "Organization daily LLM budget reached (1 of 1 LLM calls); resets at 00:00 UTC" in summary
    assert "could not be reached" not in summary
    rows = await all_usage(pg_sessionmaker)
    assert len(rows) == 1 and str(rows[0].run_id).replace("-", "") == run_id  # the call before the limit was accounted
    loaded = await store.load_state(run_id)
    assert loaded.status == "needs_attention"


# ---------------------------------------------------------------- per-org Gemini key


@pytest.fixture
def vault(pg_sessionmaker) -> Vault:
    return Vault(PgVaultBackend(pg_sessionmaker), generate_key())


async def test_resolve_llm_key_prefers_the_orgs_vault_credential(vault):
    deployment = Settings(_env_file=None, GEMINI_API_KEY=GLOBAL_KEY)
    assert resolve_llm_key(None, deployment) == (GLOBAL_KEY, key_hash_of(GLOBAL_KEY))
    assert resolve_llm_key(VaultScope("o", []), deployment)[0] == GLOBAL_KEY
    await vault.put("o", "gemini", "api_key", {"api_key": f"  {ORG_KEY}  "})
    scope = await VaultScope.load(vault, "o")
    key, key_hash = resolve_llm_key(scope, deployment)
    assert key == ORG_KEY and key_hash == key_hash_of(ORG_KEY) and len(key_hash) == 64
    assert ORG_KEY not in key_hash
    assert resolve_llm_key(None, Settings(_env_file=None)) == ("", "")
    # a credential of the wrong kind is not a key
    await vault.put("p", "gemini", "login", {"username": "u", "password": "not-an-api-key"})
    assert resolve_llm_key(await VaultScope.load(vault, "p"), deployment)[0] == GLOBAL_KEY


async def test_the_orgs_key_drives_the_client_the_limiter_and_the_health_but_the_agent_never_sees_it(
        pg_sessionmaker, vault, monkeypatch):
    org_id, user_id = await seed_org(pg_sessionmaker)
    await vault.put(str(org_id), "gemini", "api_key", {"api_key": ORG_KEY})
    await vault.put(str(org_id), "erp", "login", {"username": "clerk", "password": "erp-password-77"})
    scope = await VaultScope.load(vault, str(org_id))
    assert scope.names() == ["erp"] and scope.get("gemini") is None  # not offered to the agent
    assert scope.internal("gemini") is not None and "gemini.api_key" in scope.secret_values()  # but redacted

    built: list[str] = []

    class FakeGemini:
        name = "gemini"

        def __init__(self, api_key, model, timeout_s=0.0):
            built.append(api_key)

    monkeypatch.setattr("worker.llm.gemini.GeminiClient", FakeGemini)
    deployment = Settings(_env_file=None, GEMINI_API_KEY=GLOBAL_KEY, LLM_PROVIDER="gemini", LLM_MIN_INTERVAL_S=4.0)
    llm = make_llm(deployment, org_id=org_id, user_id=user_id, sessionmaker=pg_sessionmaker, vault_scope=scope,
                   org_settings={"gemini_models": ["custom-1", "custom-2"]})
    assert isinstance(llm, ResilientLLM) and llm.inner.models == ["custom-1", "custom-2"]
    llm.inner._factory("custom-1")
    assert built == [ORG_KEY]
    assert llm.limiter.key_hash == key_hash_of(ORG_KEY) and llm.inner.health.key_hash == key_hash_of(ORG_KEY)
    assert llm.limiter.rate_per_s == pytest.approx(0.25) and llm.limiter.capacity == 2.0

    # another org without its own key: the deployment key and the default chain
    plain = make_llm(deployment, org_id=uuid.uuid4(), sessionmaker=pg_sessionmaker)
    plain.inner._factory("m")
    assert built[-1] == GLOBAL_KEY and plain.limiter.key_hash == key_hash_of(GLOBAL_KEY)
    assert plain.inner.models == deployment.gemini_models


async def test_the_orgs_key_never_reaches_stored_steps_events_or_later_prompts(pg_sessionmaker, vault, tmp_path):
    org_id, user_id = await seed_org(pg_sessionmaker)
    await vault.put(str(org_id), "gemini", "api_key", {"api_key": ORG_KEY})
    scope = await VaultScope.load(vault, str(org_id))
    run_id = uuid.uuid4().hex
    llm = ScriptedLLM([reply(f"Odd: my key is {ORG_KEY}.", call("list_files", path=f"notes-{ORG_KEY}")), blocked()])
    settings = make_settings(tmp_path)
    store = PgRunStore(pg_sessionmaker, org_id, user_id, artifacts_root=tmp_path / "runs")
    state = await AgentRunner("List the files", llm, small_registry(settings), settings, AutoBroker.approve_all(),
                              run_id=run_id, store=store, vault_scope=scope).run()
    assert state.status in ("needs_attention", "failed", "succeeded")
    async with pg_sessionmaker() as session:
        dump = {t: (await session.execute(text(f"select to_jsonb(x)::text from {t} x"))).scalars().all()
                for t in ("runs", "run_steps", "run_events", "run_facts")}
    blob = json.dumps(dump)
    assert ORG_KEY not in blob and "[REDACTED:gemini.api_key]" in blob
    sent = json.dumps([[m.content for m in c["messages"]] for c in llm.calls])
    assert ORG_KEY not in sent and "gemini" not in [c["name"] for c in scope.public()]


# ---------------------------------------------------------------- make_llm wiring

async def test_make_llm_with_a_database_records_usage_and_enforces_the_budget(pg_sessionmaker):
    org_id, user_id = await seed_org(pg_sessionmaker, settings={"max_llm_calls_per_day": 2})
    run_id = await new_run(pg_sessionmaker, org_id, user_id)
    llm = make_llm(Settings(_env_file=None, LLM_PROVIDER="fake"), org_id=org_id, user_id=user_id, run_id=run_id,
                   sessionmaker=pg_sessionmaker)
    llm.inner._script = [resp(inp=3, out=2), resp(inp=4, out=1)]
    await llm.complete([Message("user", "1")])
    await llm.complete([Message("user", "2")])
    with pytest.raises(QuotaExceededError):
        await llm.complete([Message("user", "3")])
    rows = await all_usage(pg_sessionmaker)
    assert [(r.input_tokens, r.output_tokens, r.user_id) for r in rows] == [(3, 2, user_id), (4, 1, user_id)]


async def test_exhausted_models_for_the_health_endpoint_reads_the_shared_view(pg_sessionmaker):
    deployment = Settings(_env_file=None, GEMINI_API_KEY=GLOBAL_KEY)
    other_process = PgModelHealth(pg_sessionmaker, key_hash_of(GLOBAL_KEY))
    other_process.mark_exhausted("a")
    await other_process.flush()
    assert await exhausted_models(deployment, ["a", "b"], pg_sessionmaker) == ["a"]


# ---------------------------------------------------------------- file mode is unchanged (no database)


def test_file_mode_make_llm_is_the_v1_wiring(monkeypatch):
    built: list[str] = []

    class FakeGemini:
        name = "gemini"

        def __init__(self, api_key, model, timeout_s=0.0):
            built.append(api_key)

    monkeypatch.setattr("worker.llm.gemini.GeminiClient", FakeGemini)
    settings = Settings(_env_file=None, LLM_PROVIDER="gemini", GEMINI_API_KEY=GLOBAL_KEY, LLM_MIN_INTERVAL_S=4.0)
    llm = make_llm(settings)
    assert isinstance(llm.limiter, LocalThrottle) and llm.limiter.min_interval_s == 4.0
    assert llm.quota is None and llm.usage is None
    assert llm.inner.health is DEFAULT_HEALTH and llm.inner.models == settings.gemini_models
    llm.inner._factory("m")
    assert built == [GLOBAL_KEY]
    with pytest.raises(LLMError, match="GEMINI_API_KEY is not set"):
        make_llm(Settings(_env_file=None, LLM_PROVIDER="gemini", GEMINI_API_KEY=""))
    fake = make_llm(Settings(_env_file=None, LLM_PROVIDER="fake"))
    assert isinstance(fake.inner, ScriptedLLM) and fake.quota is None and fake.usage is None


async def test_file_mode_ignores_database_arguments_without_a_sessionmaker():
    settings = Settings(_env_file=None, LLM_PROVIDER="fake")
    llm = make_llm(settings, org_id=uuid.uuid4(), user_id=uuid.uuid4(), run_id=uuid.uuid4().hex)
    assert llm.quota is None and llm.usage is None  # no sessionmaker: nothing is recorded


async def test_file_mode_with_an_own_key_does_not_share_the_global_health(monkeypatch):
    class Scope:
        def internal(self, name):
            from worker.vault import Credential

            return Credential("o", "gemini", "api_key", {"api_key": ORG_KEY})

    monkeypatch.setattr("worker.llm.gemini.GeminiClient", lambda key, model: StubModel(model))
    llm = make_llm(Settings(_env_file=None, LLM_PROVIDER="gemini", GEMINI_API_KEY=GLOBAL_KEY), vault_scope=Scope())
    assert llm.inner.health is not DEFAULT_HEALTH and isinstance(llm.inner.health, ModelHealth)


# ---------------------------------------------------------------- org settings and the usage endpoints


async def test_org_settings_for_budgets_and_models_are_validated(world, db_app):
    app, _ = db_app()
    admin = await sign_in(app, "admin@acme.test")
    current = (await admin.get("/api/admin/settings")).json()["settings"]
    assert (current["max_tokens_per_day"], current["max_llm_calls_per_day"], current["gemini_models"]) == (2_000_000, 500, [])
    ok = await admin.put("/api/admin/settings", json={"max_tokens_per_day": 5000, "max_llm_calls_per_day": 0,
                                                      "gemini_models": [" gemini-x ", "gemini-y", "gemini-x"]})
    assert ok.status_code == 200 and ok.json()["settings"]["gemini_models"] == ["gemini-x", "gemini-y"]
    assert ok.json()["settings"]["max_llm_calls_per_day"] == 0
    for bad in ({"max_tokens_per_day": -1}, {"max_tokens_per_day": "9"}, {"max_llm_calls_per_day": 1.5},
                {"gemini_models": "gemini-x"}, {"gemini_models": [1]}, {"gemini_models": ["has space"]},
                {"gemini_models": [f"m{i}" for i in range(13)]}):
        assert (await admin.put("/api/admin/settings", json=bad)).status_code == 422, bad
    assert (await admin.get("/api/admin/settings")).json()["settings"]["max_tokens_per_day"] == 5000


async def seed_usage_world(world) -> None:
    acme, globex = world.org_ids["acme"], world.org_ids["globex"]
    omar, olga = world.user_ids["operator@acme.test"], world.user_ids["operator2@acme.test"]
    gina = world.user_ids["operator@globex.test"]
    now = datetime.now(timezone.utc)
    today = utc_day_start(now) + timedelta(seconds=1)
    two_days_ago = today - timedelta(days=2)
    for org, user, tokens, calls, ts, model in (
            (acme, omar, 300, 3, today, "gemini-a"), (acme, olga, 100, 1, today, "gemini-b"),
            (acme, omar, 50, 1, two_days_ago, "gemini-a"), (acme, omar, 9999, 9, today - timedelta(days=30), "gemini-a"),
            (globex, gina, 777, 7, today, "gemini-g")):
        await add_usage(world.sm, org, user, tokens=tokens, calls=calls, ts=ts, model=model)


async def test_the_admin_usage_endpoint_reports_the_orgs_days_users_and_models(world, db_app):
    await seed_usage_world(world)
    app, _ = db_app()
    admin = await sign_in(app, "admin@acme.test")
    report = (await admin.get("/api/admin/usage?days=7")).json()
    assert report["days"] == 7 and len(report["daily"]) == 7 and report["daily"][-1] == report["today"]
    assert (report["today"]["calls"], report["today"]["tokens"]) == (4, 400)
    assert report["daily"][4]["tokens"] == 50  # two days ago; the 30-day-old row is outside the window
    assert report["limits"] == {"max_tokens_per_day": 2_000_000, "max_llm_calls_per_day": 500}
    assert report["resets_at"].endswith("T00:00:00+00:00")
    assert [(u["name"], u["calls"], u["tokens"]) for u in report["by_user"]] == [
        ("Omar Operator", 4, 350), ("Olga Operator", 1, 100)]
    assert [(m["model"], m["tokens"]) for m in report["by_model"]] == [("gemini-a", 350), ("gemini-b", 100)]
    assert "gemini-g" not in json.dumps(report) and "Gina" not in json.dumps(report)  # org-scoped

    month = (await admin.get("/api/admin/usage?days=60")).json()
    assert sum(d["tokens"] for d in month["daily"]) == 450 + 9999

    globex_admin = await sign_in(app, "admin@globex.test")
    assert (await globex_admin.get("/api/admin/usage")).json()["today"]["tokens"] == 777


async def test_the_admin_usage_endpoint_is_role_gated_and_validated(world, db_app):
    app, _ = db_app()
    assert (await new_client(app).get("/api/admin/usage")).status_code == 401
    for local in ("operator", "approver", "viewer"):
        client = await sign_in(app, f"{local}@acme.test")
        assert (await client.get("/api/admin/usage")).status_code == 403, local
    admin = await sign_in(app, "admin@acme.test")
    for bad in ("days=0", "days=91", "days=abc"):
        assert (await admin.get(f"/api/admin/usage?{bad}")).status_code == 422, bad
    empty = (await admin.get("/api/admin/usage")).json()
    assert empty["today"]["tokens"] == 0 and empty["by_user"] == [] and len(empty["daily"]) == 7


async def test_my_usage_shows_only_the_callers_usage_in_the_active_org(world, db_app):
    await seed_usage_world(world)
    app, _ = db_app()
    omar = await sign_in(app, "operator@acme.test")
    mine = (await omar.get("/api/usage/me")).json()
    assert (mine["calls"], mine["tokens"], mine["input_tokens"]) == (3, 300, 300)
    assert mine["limits"]["max_llm_calls_per_day"] == 500
    olga = await sign_in(app, "operator2@acme.test")
    assert (await olga.get("/api/usage/me")).json()["tokens"] == 100
    viewer = await sign_in(app, "viewer@acme.test")
    assert (await viewer.get("/api/usage/me")).json()["tokens"] == 0  # any member may ask
    assert (await new_client(app).get("/api/usage/me")).status_code == 401
    # a member of two orgs sees the usage of the org that is active for the session only
    assert (await (await sign_in(app, "multi@acme.test")).get("/api/usage/me")).json()["tokens"] == 0


async def test_usage_report_helper_fills_empty_days(world):
    report = await usage_report(world.sm, world.org_ids["acme"], days=3)
    assert [d["tokens"] for d in report["daily"]] == [0, 0, 0] and report["by_model"] == []
    assert len({d["day"] for d in report["daily"]}) == 3

