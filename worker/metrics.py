"""Prometheus metrics for the API and the worker processes (``prometheus-client``), in one registry per process.

Every process (an API copy, a ``python -m worker.jobs`` worker, the file-mode server that does both) keeps its own
numbers and exposes them: the API on ``GET /metrics`` (protected, see ``worker/system_api.py``), a worker on
``--metrics-port`` (loopback). Scrape every process and aggregate in Prometheus; in database mode the run, tool, LLM and
approval series live in the workers, the HTTP, WebSocket and queue-depth series in the API.

Metrics (``oneforall_`` prefix; histograms add ``_bucket``/``_sum``/``_count``):

=================================  ==============================  ==============================================
runs_total                         counter {status}                finished runs by final status
run_duration_seconds               histogram                       active time of a run (approval waits excluded)
runs_active                        gauge                           runs executing in this process
queue_depth                        gauge {org}                     queued runs per org slug (API samples the DB)
queue_wait_seconds                 histogram                       queued_at -> claimed (first claims only)
tool_calls_total                   counter {tool, ok}              tool executions
tool_duration_seconds              histogram {tool}                tool execution time (retries included)
llm_calls_total                    counter {model, outcome}        per attempt: ok, rate_limited, network, timeout, transient, malformed, error
llm_tokens_total                   counter {model, direction}      input / output tokens
llm_latency_seconds                histogram {model}               time inside the provider call
llm_ratelimit_wait_seconds         histogram                       time spent waiting for the rate limiter
approvals_total                    counter {kind, outcome}         approved, denied, answered, expired, cancelled, ...
approval_wait_seconds              histogram                       time a run waited for a human
worker_leases_lost_total           counter                         runs this worker lost to another worker
runs_requeued_total                counter {reason}                worker_lost, shutdown
runs_resumed_total                 counter                         runs continued from a checkpoint
browser_contexts_active            gauge                           open isolated browser contexts
browser_relaunches_total           counter                         Chromium relaunches after a crash
ws_connections                     gauge                           open WebSockets
http_requests_total                counter {route, method, status} route = path template, never the raw path
http_request_duration_seconds      histogram {route}               request time
=================================  ==============================  ==============================================

``RunObserver`` is the per-run helper the agent runner uses; everything else is a plain module-level metric.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from prometheus_client import (  # noqa: F401 - re-exported for docs
    GC_COLLECTOR,
    PLATFORM_COLLECTOR,
    PROCESS_COLLECTOR,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
)

REGISTRY = CollectorRegistry(auto_describe=True)
for _collector in (GC_COLLECTOR, PLATFORM_COLLECTOR, PROCESS_COLLECTOR):
    try:
        REGISTRY.register(_collector)
    except ValueError:  # already registered (module reloaded)
        pass

FINAL_STATUSES = ("succeeded", "failed", "needs_attention", "cancelled")
MAX_ORG_LABELS = 50  # org label values on the queue-depth gauge; the rest is summed under "_other"
OTHER_ORG = "_other"

_SECONDS_SLOW = (1, 5, 10, 30, 60, 120, 300, 600, 900, 1800, 3600)
_SECONDS_FAST = (0.005, 0.025, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)

RUNS = Counter("oneforall_runs_total", "Finished runs by final status.", ["status"], registry=REGISTRY)
RUN_DURATION = Histogram("oneforall_run_duration_seconds", "Active time of a run (approval waits excluded).",
                         buckets=_SECONDS_SLOW, registry=REGISTRY)
RUNS_ACTIVE = Gauge("oneforall_runs_active", "Runs executing in this process.", registry=REGISTRY)
QUEUE_DEPTH = Gauge("oneforall_queue_depth", "Queued runs per organization (slug).", ["org"], registry=REGISTRY)
QUEUE_WAIT = Histogram("oneforall_queue_wait_seconds", "Time from queueing to the first claim by a worker.",
                       buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800), registry=REGISTRY)

TOOL_CALLS = Counter("oneforall_tool_calls_total", "Tool executions.", ["tool", "ok"], registry=REGISTRY)
TOOL_DURATION = Histogram("oneforall_tool_duration_seconds", "Tool execution time (automatic retries included).",
                          ["tool"], buckets=_SECONDS_FAST, registry=REGISTRY)

LLM_CALLS = Counter("oneforall_llm_calls_total", "LLM call attempts by outcome.", ["model", "outcome"], registry=REGISTRY)
LLM_TOKENS = Counter("oneforall_llm_tokens_total", "LLM tokens.", ["model", "direction"], registry=REGISTRY)
LLM_LATENCY = Histogram("oneforall_llm_latency_seconds", "Time inside the provider call.", ["model"],
                        buckets=(0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120), registry=REGISTRY)
LLM_RATELIMIT_WAIT = Histogram("oneforall_llm_ratelimit_wait_seconds", "Time spent waiting for the LLM rate limiter.",
                               buckets=(0.001, 0.01, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 300), registry=REGISTRY)

APPROVALS = Counter("oneforall_approvals_total", "Approvals and questions by outcome.", ["kind", "outcome"], registry=REGISTRY)
APPROVAL_WAIT = Histogram("oneforall_approval_wait_seconds", "Time a run waited for a human answer.",
                          buckets=(1, 5, 15, 30, 60, 120, 300, 900, 1800, 3600, 14400), registry=REGISTRY)

LEASES_LOST = Counter("oneforall_worker_leases_lost_total", "Runs whose lease this worker lost.", registry=REGISTRY)
REQUEUED = Counter("oneforall_runs_requeued_total", "Runs put back in the queue.", ["reason"], registry=REGISTRY)
RESUMED = Counter("oneforall_runs_resumed_total", "Runs continued from a checkpoint.", registry=REGISTRY)

BROWSER_CONTEXTS = Gauge("oneforall_browser_contexts_active", "Open isolated browser contexts.", registry=REGISTRY)
BROWSER_RELAUNCHES = Counter("oneforall_browser_relaunches_total", "Chromium relaunches after a crash.", registry=REGISTRY)

WS_CONNECTIONS = Gauge("oneforall_ws_connections", "Open WebSocket connections.", registry=REGISTRY)
HTTP_REQUESTS = Counter("oneforall_http_requests_total", "HTTP requests.", ["route", "method", "status"], registry=REGISTRY)
HTTP_DURATION = Histogram("oneforall_http_request_duration_seconds", "HTTP request time.", ["route"],
                          buckets=(0.005, 0.025, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30), registry=REGISTRY)

for _status in FINAL_STATUSES:  # series exist from the start, so rate() works before the first run
    RUNS.labels(_status)
for _reason in ("worker_lost", "shutdown"):
    REQUEUED.labels(_reason)


# --------------------------------------------------------------------------- helpers


def tool_call(tool: str, ok: bool, seconds: float) -> None:
    TOOL_CALLS.labels(tool, "true" if ok else "false").inc()
    TOOL_DURATION.labels(tool).observe(max(0.0, seconds))


def approval_resolved(kind: str, outcome: str, waited_s: float) -> None:
    APPROVALS.labels(kind, outcome).inc()
    APPROVAL_WAIT.observe(max(0.0, waited_s))


def llm_call(model: str, outcome: str, seconds: float | None = None) -> None:
    LLM_CALLS.labels(model, outcome).inc()
    if seconds is not None:
        LLM_LATENCY.labels(model).observe(max(0.0, seconds))


def llm_tokens(model: str, input_tokens: int, output_tokens: int) -> None:
    if input_tokens:
        LLM_TOKENS.labels(model, "input").inc(input_tokens)
    if output_tokens:
        LLM_TOKENS.labels(model, "output").inc(output_tokens)


def run_finished_unrun(status: str) -> None:
    """A run that ended without a runner (could not start, cancelled in the queue, worker lost too often)."""
    RUNS.labels(status).inc()


class RunObserver:
    """Per-run bookkeeping for the agent runner: the active gauge, the active-time histogram, final status, human waits.

    ``begin`` / ``close`` bracket the run (``close`` is safe to call twice); ``finished`` records the final status once;
    ``wait_started`` / ``wait_ended`` time a wait for a human, which is subtracted from the run's duration."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._began: float | None = None
        self._waited = 0.0
        self._wait_since: float | None = None
        self._active = False
        self._recorded = False

    def begin(self, *, resumed: bool = False) -> None:
        if self._active:
            return
        self._active, self._began = True, self._clock()
        RUNS_ACTIVE.inc()
        if resumed:
            RESUMED.inc()

    def wait_started(self) -> None:
        self._wait_since = self._clock()

    def wait_ended(self) -> float:
        """Seconds waited since ``wait_started`` (0 if none was open)."""
        if self._wait_since is None:
            return 0.0
        waited = max(0.0, self._clock() - self._wait_since)
        self._wait_since = None
        self._waited += waited
        return waited

    def finished(self, status: str) -> None:
        if self._recorded:
            return
        self._recorded = True
        self.wait_ended()  # a wait cut short by cancellation is still a wait, not active time
        RUNS.labels(status).inc()
        if self._began is not None:
            RUN_DURATION.observe(max(0.0, self._clock() - self._began - self._waited))

    def close(self) -> None:
        if self._active:
            self._active = False
            RUNS_ACTIVE.dec()


# --------------------------------------------------------------------------- queue depth (API side)


def set_queue_depth(depths: dict[str, int]) -> None:
    """Replace the queue-depth gauge with ``{org slug: queued runs}``: at most ``MAX_ORG_LABELS`` series, the smaller
    orgs summed under ``_other``. An org whose queue emptied since the last call is reported as 0 once, then dropped."""
    ranked = sorted(depths.items(), key=lambda kv: (-kv[1], kv[0]))
    shown = dict(ranked[:MAX_ORG_LABELS])
    rest = sum(n for _slug, n in ranked[MAX_ORG_LABELS:])
    if rest:
        shown[OTHER_ORG] = rest
    for org in _known_orgs - set(shown):
        if org in _zeroed_orgs:
            QUEUE_DEPTH.remove(org)
            _known_orgs.discard(org)
            _zeroed_orgs.discard(org)
        else:
            QUEUE_DEPTH.labels(org).set(0)
            _zeroed_orgs.add(org)
    for org, n in shown.items():
        QUEUE_DEPTH.labels(org).set(n)
        _known_orgs.add(org)
        _zeroed_orgs.discard(org)


_known_orgs: set[str] = set()  # org label values currently on the gauge
_zeroed_orgs: set[str] = set()  # ... of which already reported as 0 once


def serve_metrics(port: int, addr: str = "127.0.0.1") -> Callable[[], None]:
    """Serve this process's metrics on ``addr:port`` (a worker's ``--metrics-port``; loopback by default) and return the
    function that stops the server."""
    from prometheus_client import start_http_server

    server, _thread = start_http_server(port, addr=addr, registry=REGISTRY)

    def stop() -> None:
        server.shutdown()
        server.server_close()

    return stop


def sample(name: str, **labels: str) -> float:
    """The current value of one series in the registry (0.0 if it does not exist). Handy in tests and scripts."""
    return REGISTRY.get_sample_value(name, labels) or 0.0
