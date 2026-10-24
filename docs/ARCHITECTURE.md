# Architecture

This document describes how the worker is built, in enough detail to review or extend it. Everything here reflects the code in this repository; file links are relative. For setup and the rationale behind the major decisions, see the [README](../README.md); for operating the multi-user mode see [MULTI_USER.md](MULTI_USER.md), and for measured behaviour [SCALING.md](SCALING.md).

The system has two modes that share one agent runtime. **Single-user mode** (no `DATABASE_URL`) keeps runs in files and executes them inside the API process, one at a time. **Multi-user mode** (`DATABASE_URL` set) stores everything in PostgreSQL, queues runs, and executes them in separate worker processes. Sections 1 to 8 are about the platform around the agent (mostly multi-user mode); sections 9 to 17 are about the agent runtime (both modes); 18 to 20 cover the simulator and the API; the rest are extension points, deployment and observability.

Contents: [1 Components and processes](#1-components-and-processes) | [2 Data model](#2-data-model) | [3 Run lifecycle](#3-run-lifecycle) | [4 Job queue](#4-job-queue) | [5 Approvals](#5-approvals) | [6 Events and live streaming](#6-events-and-live-streaming) | [7 Multi-tenancy and identity](#7-multi-tenancy-and-identity) | [8 Credential vault and redaction](#8-credential-vault-and-redaction) | [9 Context construction](#9-context-construction) | [10 State model](#10-state-model) | [11 Browser observation and the browser pool](#11-browser-observation-and-the-browser-pool) | [12 Risk classification and approval policy](#12-risk-classification-and-approval-policy) | [13 Verifier](#13-verifier) | [14 Guards](#14-guards) | [15 Reliability matrix](#15-reliability-matrix) | [16 LLM layer](#16-llm-layer) | [17 Organization memory](#17-organization-memory) | [18 Simulated company](#18-simulated-company) | [19 HTTP API](#19-http-api) | [20 Extension points](#20-extension-points) | [Deployment topology](#deployment-topology-optional-opt-in) | [Observability](#observability)

---

## 1. Components and processes

```
  +-------------------+       +----------------------------+
  | Dashboard (JS)    |       | CLI: python -m worker.agent|
  | worker/ui/        |       | worker/agent/cli.py        |
  +---------+---------+       +-------------+--------------+
            | HTTPS + WebSocket             | AutoBroker, FileRunStore, in-process
            v                               |
  +-------------------------------+         |
  | API  worker/server.py :8000   |         |
  |  auth, org scoping, runs_api, |         |
  |  admin_api, memory_api, ...   |         |
  +---------------+---------------+         |
                  | SQL, LISTEN/NOTIFY      |
                  v                         |
  +-------------------------------------+   |
  | PostgreSQL                          |   |
  |  runs, run_steps, run_facts,        |   |
  |  run_events, approvals, users,      |   |
  |  orgs, memberships, auth_sessions,  |   |
  |  org_memory, credentials,           |   |
  |  usage_events, llm_limits,          |   |
  |  model_health, audit_log, ...       |   |
  +------^------------------------------+   |
         | claim (SKIP LOCKED), lease,      |
         | heartbeat, events, NOTIFY        |
  +------+---------------------------+      |
  | Worker  python -m worker.jobs     |<-----+  (the CLI embeds the same runner)
  |  K runs at once, one Chromium,    |
  |  one browser context per run      |
  |  +-----------------------------+  |
  |  | AgentRunner (one per run)   |  |
  |  |  context -> LLM -> policy   |  |
  |  |  -> broker -> tools ->      |  |
  |  |  store, guards, verifier    |  |
  |  +-----------------------------+  |
  +----+-----------------------------+
       | Chromium (DOM snapshots), httpx, files
       v
  +------------------------------------------------------------+
  | company_sim (FastAPI :8001): /mail /portal /erp /admin /t/ |
  | one isolated SQLite dataset per organization (tenant)      |
  +------------------------------------------------------------+
```

| Process | Entry point | Holds | Notes |
|---|---|---|---|
| API and dashboard | `python -m worker.server`, or `uvicorn worker.server:app` | no run state | any number of copies; one LISTEN connection and one pool each; in single-user mode it also executes the run |
| Worker | `python -m worker.jobs` | up to K runs, one Chromium | any number, any machine that reaches the database; `--metrics-port` for its own `/metrics` |
| Simulated company | `python -m company_sim` | tenant data, in-memory sessions and chaos flags | a single process by design |
| `run.py` | `python run.py [--workers N]` | nothing | development launcher: simulator, API and (in database mode) N workers |

Module responsibilities:

| Module | Responsibility |
|---|---|
| [worker/agent/runner.py](../worker/agent/runner.py) | The loop: context, LLM call, per-call classify/approve/execute/record, guards, finish and verification, checkpoint and resume, final report. Contains no task knowledge. |
| [worker/agent/state.py](../worker/agent/state.py) | Serializable `RunState`, `Step`, `Fact`, `PlanItem`. |
| [worker/agent/context.py](../worker/agent/context.py) | Builds the LLM message list from state each turn. |
| [worker/agent/prompts.py](../worker/agent/prompts.py) | Task-agnostic system prompt, verifier prompt, canned feedback messages. |
| [worker/agent/policy.py](../worker/agent/policy.py) | Approval policy (`auto`, `confirm_writes`, `confirm_all`) and the "approve similar" cache. |
| [worker/agent/guards.py](../worker/agent/guards.py) | Budget, timeout (human waits excluded), failure streak and repetition detection. |
| [worker/agent/verifier.py](../worker/agent/verifier.py) | Independent audit and `Verdict`. |
| [worker/agent/broker.py](../worker/agent/broker.py), [pg_broker.py](../worker/agent/pg_broker.py) | Request/response channel between runner and human: in-memory (`InteractionBroker`, `AutoBroker`) and durable (`PgBroker`). |
| [worker/store.py](../worker/store.py), [store_pg.py](../worker/store_pg.py) | The `RunStore` protocol and its two implementations: `FileRunStore`, and `PgRunStore` scoped to one organization. |
| [worker/jobqueue.py](../worker/jobqueue.py), [jobs.py](../worker/jobs.py) | The SQL of the queue (enqueue, claim, lease, reap, cancel) and the worker process. |
| [worker/approvals_pg.py](../worker/approvals_pg.py) | Every state change of an approval row. |
| [worker/live.py](../worker/live.py), [pg_listen.py](../worker/pg_listen.py) | Event fan-out to WebSockets; the LISTEN connection with reconnect. |
| [worker/auth.py](../worker/auth.py), [org_settings.py](../worker/org_settings.py), [admin_api.py](../worker/admin_api.py) | Sign-in, sessions, roles, `OrgContext`, CSRF guard; per-org settings and approval rights; members, settings, credentials, environment, audit. |
| [worker/runs_api.py](../worker/runs_api.py) | Database-mode run, respond, inbox and event-stream endpoints. |
| [worker/vault.py](../worker/vault.py), [vault_pg.py](../worker/vault_pg.py), [redaction.py](../worker/redaction.py), [tools/secrets.py](../worker/tools/secrets.py) | Credential vault and the redaction layer. |
| [worker/provisioning.py](../worker/provisioning.py), [run_context.py](../worker/run_context.py) | Create/reset an org's simulated company; what starting a run needs (settings, environment, LLM). |
| [worker/memory_store.py](../worker/memory_store.py), [memory_api.py](../worker/memory_api.py) | Organization memory: screening, write gate, recall, governance. |
| [worker/usage.py](../worker/usage.py), [llm/limiter.py](../worker/llm/limiter.py), [llm/health_pg.py](../worker/llm/health_pg.py) | Usage ledger and budgets; shared rate limiter; shared model health. |
| [worker/metrics.py](../worker/metrics.py), [logging_setup.py](../worker/logging_setup.py), [observability.py](../worker/observability.py), [health.py](../worker/health.py), [system_api.py](../worker/system_api.py) | Metrics, logs, request middleware, readiness, `/metrics` and the System summary. |
| [worker/tools/](../worker/tools) | Tool framework (`Tool`, `ToolResult`, `Risk`, registry, retry) and the concrete tools, including the browser pool. |
| [worker/llm/](../worker/llm) | Neutral message types, provider adapters, resilience wrapper, failover, the policy stand-in. |
| [worker/server.py](../worker/server.py) | App factory: wires the routers, the single-user run endpoints, scenario presets, the simulator proxy. |

## 2. Data model

PostgreSQL, managed by Alembic ([worker/db/models.py](../worker/db/models.py), five migrations in [worker/db/migrations/versions/](../worker/db/migrations/versions)). Conventions: UUID primary keys (event tables use a bigint identity); every timestamp is `timestamptz` with a server default, so any process may insert without a clock of its own; flexible structure is JSONB; closed vocabularies are `text` plus a CHECK constraint; every tenant-owned table carries `org_id` (directly, or through its run).

| Table | Holds |
|---|---|
| `orgs` | An organization (tenant): slug, name, `settings` JSONB (only what an admin changed), `sim_tenant_id` (its simulated company) |
| `users` | A person: unique case-insensitive e-mail, argon2 password hash, display name, active flag, last login |
| `memberships` | (org, user, role) with a CHECK on the five roles, plus a per-org `is_active` flag |
| `auth_sessions` | Server-side sessions: id = SHA-256 of the cookie token, user, active org, sliding `expires_at`, fixed `absolute_expires_at`, IP, user agent, `revoked_at` |
| `login_attempts` | Failed-login counters per throttle key (hash of the e-mail, or the address) and fixed window |
| `runs` | The run header and queue bookkeeping: task, status (CHECK), approval mode, plan, goal, criteria, usage, `final_report`, `report_markdown`, **`runtime`** (the runner's checkpoint), `queued_at/started_at/finished_at`, **lease** (`lease_owner`, `lease_expires_at`, `heartbeat_at`), `attempts`, `resume_count`, `resume_requested`, `cancel_requested`, `priority`, `options`, `event_seq` |
| `run_steps` | One finished step (tool call, approval, question, verifier action, system note): thought, tool, args, risk, ok, observation, error, evidence, duration, retries, turn, tool-call id. Append-only, keyed (run, idx) |
| `run_facts` | The run's working memory with provenance; `durable` and `scope` mark candidates for organization memory |
| `run_events` | The ordered event log of a run, keyed (run, seq), seq gapless |
| `approvals` | Approval and question requests: kind, payload, status, `requested_at`, `expires_at`, `responded_by`, `response`, `consumed_at` |
| `org_memory` | Organization memory notes: key (unique per org, case-insensitive), content, scope, provenance, `verified`, use count, last used, expiry, and a generated `tsvector` with a GIN index |
| `credentials` | The vault: (owner = org id, name), kind, non-secret metadata, and the Fernet `ciphertext` |
| `usage_events` | LLM usage: org, user, run, model, calls, tokens |
| `llm_limits` | The shared token bucket, one row per API-key hash |
| `model_health` | Which models are exhausted or failing, per key hash and model, with an expiry |
| `audit_log` | Append-only security trail: org, user, action, target, details |

Migrations: `0001` the initial schema; `0002` per-org member deactivation and absolute session expiry; `0003` run options, the resume flag, `approvals.consumed_at` and the partial index for the per-org concurrency count; `0004` the usage index for budget checks; `0005` memory governance (unique key, `updated_at`, durable facts). All are additive. Notable indexes: a partial index over queued runs ordered by priority and age (the queue), a partial index over active runs per org (the concurrency check), a partial index on `lease_expires_at` (the reaper), `(org_id, ts)` on the ledger and the audit log.

The run state in the database is the same `RunState` the file store keeps in `run.json`; `PgRunStore.load_state` rebuilds it from the header, steps and facts. Artifacts (screenshots, downloads) stay in a local directory, `RUNS_DIR/<run_id>/` (section 20).

## 3. Run lifecycle

### 3.1 States

```
 POST /api/runs ---> queued ----claim----> running <---------------------------+
                      ^  |                   |  |                              |
   requeue + resume   |  | cancel            |  +--> waiting_approval --answer, expiry--+
   (lease expired,    |  v                   |  +--> waiting_user    --answer-----------+
    worker stopped)   |  cancelled           |
                      |                      +--> verifying --failed audit--> running (max 2 rounds)
                      +----- worker lost ----+
                                             v
                         succeeded | failed | needs_attention | cancelled
```

| Status | Set by | Meaning |
|---|---|---|
| `pending` | single-user mode | a run object that has not started (database mode creates runs as `queued`) |
| `queued` | API (enqueue), reaper, worker shutdown | waiting for a worker with a free slot; `resume_requested` says whether it has a checkpoint |
| `running` | claim, runner | executing |
| `waiting_approval`, `waiting_user` | runner | blocked on a person; checkpointed first, so a crash here loses nothing |
| `verifying` | runner | the independent audit is running |
| `succeeded` | runner | the claim passed the audit |
| `failed`, `needs_attention`, `cancelled` | runner, or `jobqueue.finish_unrun` for runs no runner executes | `needs_attention` is the honest "stopped, a human should look": blocked, guard stop, unreachable model, inconclusive audit, expired approval, exhausted budget, worker lost too often |

`jobqueue.finish_unrun` gives a run that no runner is executing a report and a `run_finished` event: a queued run that was cancelled, a run the reaper gave up on, an org without an environment, a worker-side error. It also cancels the run's open approvals.

### 3.2 The agent loop

```
User        Dashboard/CLI     Runner                 LLM            Policy/Broker        Tools            Verifier
 | task          |               |                     |                 |                 |                 |
 |-------------->| POST /api/runs|                     |                 |                 |                 |
 |               |  (queued; a worker claims it)       |                 |                 |                 |
 |               |               | run_started         |                 |                 |                 |
 |               |   LOOP        | build_messages(state)                                                     |
 |               |               |-------------------->| tool calls      |                 |                 |
 |               |               | (turn 1 normally: update_plan with goal, success criteria, checklist)     |
 |               |               | for each call:                                                            |
 |               |               |   classify(risk) ---------------------->| needs approval? |                 |
 |               |<-------- approval_requested (exact action + memory) ----| yes             |                 |
 |  approve/deny |-------------->| POST /respond ------------------------->| resolves        |                 |
 |               |               |   denied -> step(error=denied) -> observation to the LLM                  |
 |               |               |   approved -> tool_started ---------------------------->| run + retry     |
 |               |               |   record Step, emit tool_finished, feed guards                            |
 |               |               | guards.check_progress -> nudge | ask the human | stop                     |
 |               |               |                                                                           |
 |               |               | finish(status=success, summary, outputs, evidence)                        |
 |               |               | status = verifying ------------------------------------------------------>|
 |               |               |                                      re-observes real system, read-only   |
 |               |               |<------------------------------------------------ submit_verdict ---------|
 |               |               | passed        -> succeeded                                                |
 |               |               | failed        -> feedback into next turn (max 2 correction rounds), else failed
 |               |               | inconclusive  -> needs_attention                                          |
 |               |<-- run_finished(status, report) + report.md                                               |
```

Step by step, as implemented in `AgentRunner._loop`:

1. **Start.** `run()` creates the run in the store (or, for a claimed run, updates the one the API queued: `precreated`), sets status `running`, saves the first checkpoint, emits `run_started`. A resumed run emits `run_resumed` instead. Then it recalls organization memory once (section 17).
2. **Context.** `build_messages` assembles the prompt from `RunState` (section 9).
3. **LLM call** through `ResilientLLM` (`tool_choice="auto"`; `"any"` after a text-only reply). `QuotaExceededError` (the org's own budget) or any `LLMError` after retries ends the run as `needs_attention`. Usage is added to the run and a `usage` event is emitted. Any text is emitted as `thought`.
4. **No tool call.** The model answered in prose: the step is recorded as `no_tool_call`, a nudge is added, and the next call forces a tool. A second consecutive text-only reply ends the run as `needs_attention` using the text as the summary.
5. **Per call** (`_handle_calls`):
   - `finish` mixed with other calls in one turn is rejected (`finish_not_alone`).
   - After a failure earlier in the same turn, remaining calls are recorded as `skipped` (the situation has changed).
   - Otherwise `_execute`: look up the tool (unknown tool is an error observation listing valid names), classify the risk from the tool, its arguments and this run's own page, ask `ApprovalPolicy.needs_approval`, and if needed block on a human via the broker (status `waiting_approval`; the wall-clock guard is paused). A denial produces a `denied` step and a `DENIED` observation; the tool is never run. If nobody answers before the approval expires, the run stops as `needs_attention`.
   - Approved or ungated calls emit `tool_started`, run through `execute_with_retry`, and become a `Step` with duration, evidence paths and the retry count parsed from the observation. `update_plan` results are applied to `RunState` and emitted as `plan_updated`.
6. **Checkpoint.** Every step is appended to the store and the run header plus the runner's hidden state are saved (section 3.3).
7. **Guards.** After each turn `check_progress` may return a nudge (appended to the next prompt) or an escalation (the runner asks the human "Keep trying / Stop the task"). Before each loop iteration `check_limits` enforces the step budget and timeout, and a cancel request stops the run.
8. **Finish.** `finish` with status `failed` or `blocked` ends the run immediately (`failed` / `needs_attention`) without an audit. `success` starts verification (status `verifying`, `verification_started`).
9. **Verification outcome.** Passed: step marked OK, run `succeeded`. Failed: the auditor's findings become `VERIFIER FEEDBACK (unresolved)` in the state block and the loop continues; after more than 2 failed audits the run ends `failed`. Inconclusive (auditor ran out of steps or its LLM failed): `needs_attention`.
10. **Wrap-up.** The run's browser context is released, `build_report` assembles the report (redacted), the header, `report_markdown` and `run_finished` are stored. In a worker, organization memory is committed afterwards, only for `succeeded` (section 17). An unexpected exception inside the loop still produces a report (status `failed`, reason recorded).

Final statuses: `succeeded` (verified), `failed`, `needs_attention`, `cancelled`. Intermediate: `pending`, `queued`, `running`, `waiting_approval`, `waiting_user`, `verifying`.

In single-user mode the artifacts of a run are `runs/<run_id>/`: `run.json` (full state, saved after every step), `events.jsonl`, `runtime.json`, `report.md`, `screenshots/NNN_label.png`, `downloads/`. In multi-user mode the state lives in the database, and only screenshots and downloads are files.

### 3.3 Checkpoint and resume

After every step (and before every wait for a human) the runner saves, besides the steps and facts, a small JSON blob, `runs.runtime`, with its hidden in-memory state: the turn counter, the text-only streak and `force_tool` flag, pending nudges, the verifier feedback, the failed-audit count, the finish claim and last verdict, approval counts, the resume count, the guards' counters (failure streak, nudge flags, the repetition window, and the **active time** so far) and the approval policy including its "approve similar" cache.

`AgentRunner.resume(run_id, ...)` loads the state and that blob, refuses runs that are already final (`ResumeError`), appends a system step ("Run resumed after an interruption; the browser was restarted, so re-open and re-check the current state before acting; do not repeat a write without first checking whether it already took effect"), increments `resume_count`, and continues the loop. Because the prompt is rebuilt from state every turn, nothing else is needed. The new worker gets a fresh browser context with the organization's tenant cookie pre-seeded again, so the agent logs in again and checks whether its earlier write already happened. The approval mode the run started with is kept; limits come from the current settings.

## 4. Job queue

All of it is SQL in [worker/jobqueue.py](../worker/jobqueue.py), so any number of API and worker processes agree. Statuses that count as "a worker holds the run" are `running`, `waiting_approval`, `waiting_user`, `verifying`.

**Enqueue.** One transaction: if the org has a daily limit, take `pg_advisory_xact_lock(hashtextextended('enqueue:<org>', 0))`, count the org's runs with `queued_at` since UTC midnight, raise `QuotaExceeded` (HTTP 429 with `Retry-After`) at the limit; insert the `runs` row (`queued`, `event_seq = 1`) and a `run_queued` event; `pg_notify` on `run_events` and `runs_queued`. The lock makes the check-and-insert atomic per org, so concurrent requests cannot both squeeze under the limit.

**Claim.** A worker with a free slot (`len(handles) < concurrency`) runs `claim_next`, one transaction:

```sql
-- 1. the head of each org's queue, for orgs that still have a free slot
select id, org_id from (
    select distinct on (r.org_id) r.id, r.org_id, r.priority, r.queued_at
    from runs r join orgs o on o.id = r.org_id
    where r.status = 'queued'
      and (select count(*) from runs a where a.org_id = r.org_id and a.status = any(:active))
          < coalesce((o.settings->>'max_concurrent_runs')::int, :default_limit)
    order by r.org_id, r.priority desc, r.queued_at, r.id
) heads order by priority desc, queued_at limit 20;

-- 2. for each candidate org, in order: take a per-org advisory lock, re-check the slot, then
select pg_try_advisory_xact_lock(hashtextextended('claim:<org>', 0));   -- skip the org if another claimer holds it
update runs set status = 'running', lease_owner = :worker, heartbeat_at = now(),
                lease_expires_at = now() + make_interval(secs => :lease),
                attempts = attempts + 1, started_at = coalesce(started_at, now()), resume_requested = false
where id = (select id from runs where org_id = :org and status = 'queued'
            order by priority desc, queued_at, id for update skip locked limit 1)
returning ...;
```

`FOR UPDATE SKIP LOCKED` means two workers never take the same run; the per-org advisory lock makes the concurrency check and the claim one atomic step per org, so `max_concurrent_runs` is never exceeded and orgs are independent (one org's full slots never block another's runs). The claim returns the previous `resume_requested` so the worker knows whether to resume, and the time the run waited in the queue (a metric). The claim loop wakes on `NOTIFY runs_queued` and polls every `JOB_POLL_S` as a fallback; when a claimer skipped an org because its lock was held it retries after 0.1 s.

**Lease and heartbeat.** A claimed run belongs to its worker until `lease_expires_at` (database clock). Each run has a heartbeat task that every `JOB_LEASE_S / 3` executes `update runs set lease_expires_at = now() + lease, heartbeat_at = now() where id = :id and lease_owner = :worker and status in (active)` and reads `cancel_requested`. The result has three cases: the flag (extended), `None` (the lease is no longer ours: re-queued, given up on, or another owner), and `False` for a run that simply *ended* with this worker's lease still on it (the runner saves the final status a moment before the worker stops the heartbeat; mistaking that for a lost lease was a real bug the load test found, see SCALING.md). If the database cannot be reached for a whole lease, the worker assumes the lease is gone.

**Reaper.** Every `JOB_REAPER_INTERVAL_S` every worker runs:

```sql
with expired as (
    select id, lease_owner as old_owner from runs
    where status = any(:active) and lease_expires_at < now() order by lease_expires_at limit 50 for update skip locked)
update runs r set status = case when r.attempts < :max_attempts then 'queued' else 'needs_attention' end,
                  resume_requested = (r.runtime is not null), lease_owner = null, lease_expires_at = null, ...
from expired where r.id = expired.id returning ...;
```

Re-queued runs get a `run_requeued` event (reason `worker_lost`) and a NOTIFY, and the next claim resumes them. Runs that used all `JOB_MAX_ATTEMPTS` end `needs_attention` ("The worker running this run was lost too many times") with a report, via `finish_unrun`. The same sweep calls `expire_overdue` for approvals whose waiting worker died. Many reapers at once are safe (`SKIP LOCKED`).

**Fencing.** When a heartbeat finds the lease gone, the worker marks the run *lost*: its store is wrapped in `FencedStore`, which silently drops every write (`append_event`, `save_state`, `append_step`, `upsert_fact`, `save_runtime`, `save_report`, `create_run`) while reads still work, its broker keeps open approvals alive, the runner is cancelled and the task cancelled, so it can unwind and close its browser without disturbing the worker that took over. The same fence is used when a worker hands a run back on shutdown, so the last checkpoint is exactly what the next worker resumes from. **Limit:** the fence is in-process. SQL statements carry no per-claim token, so a stalled-but-alive worker could still write in the short window before its next heartbeat notices; event numbers (assigned under the run's row lock) and step keys (unique per run) bound the damage. A database-enforced token is listed as future work.

**Cancel.** For a `queued` run the API updates it to `cancelled` at once and finishes it with a report. For a claimed run it sets `cancel_requested` and sends `NOTIFY run_control '<run>:cancel'`; the holding worker cancels the runner immediately, and the heartbeat re-reads the flag as a fallback. The runner stops at the next safe point and releases open approvals.

**Shutdown.** The first SIGINT/SIGTERM/Ctrl+Break stops claiming. By default every held run is *handed back*: fenced, then `update runs set status = 'queued', resume_requested = (runtime is not null), lease_owner = null, attempts = greatest(attempts - 1, 0)`, so the claim is not counted and another worker resumes it. With `--drain` the worker waits for its runs instead. A second signal stops immediately.

**Fairness and back-pressure.** `max_concurrent_runs` (per org) bounds slots; `max_runs_per_day`, `max_tokens_per_day` and `max_llm_calls_per_day` bound spending; queue position is reported per org. A worker's own `WORKER_CONCURRENCY` and the browser pool's `BROWSER_MAX_CONTEXTS` bound its memory.

## 5. Approvals

An approval or a question to the user is one row of `approvals` ([worker/approvals_pg.py](../worker/approvals_pg.py)):

```
pending --respond--> approved | denied | answered    first writer wins; a second answer gets 409
pending --timeout--> expired                          marked by the waiting broker, or by any reaper
pending --cancel---> cancelled                        the run was cancelled or ended
```

Every transition commits together with a `pg_notify`: `approvals` when a request is raised, `approval_responses` for all other transitions (payload `<org>:<approval id>`).

**PgBroker** ([worker/agent/pg_broker.py](../worker/agent/pg_broker.py)) is the runner's `InteractionBroker` in a worker. `request()` inserts the row (`expires_at = now() + approval_timeout_s`, the org setting) and the runner announces it with an `approval_requested` event; then it waits on a per-process `asyncio.Event` set by the worker's shared listener, re-reading the row on each wake-up and at least every `APPROVAL_POLL_S`, so a missed NOTIFY delays by seconds and never loses an answer. Outcomes: the stored response (`consume` marks `consumed_at`), or `None` for an expired or cancelled request, with `last_expired` and a reason ("approval expired after N min") that the runner turns into a `needs_attention` ending instead of guessing.

**The answer path** (`POST /api/runs/{id}/respond`, any API copy): check the run belongs to the caller's org (else 404), load the approval, compute the caller's rights with `answer_rights` (role, separation of duties, kind), then `UPDATE approvals SET status = ..., response = ..., responded_by = ... WHERE id = :id AND status = 'pending' AND (expires_at IS NULL OR expires_at > now())`. No row updated means someone else answered first or the deadline passed (409). The response carries `by` (the answering user), which ends up in the `approval_resolved` event, the run's step ("APPROVED by <name>: ...") and the audit log. NOTIFY wakes the waiting worker.

**Expiry** is enforced at three points: the waiting broker compares with the database clock and marks the row; `respond` refuses an answer after the deadline; and each worker's reaper sweeps pending approvals whose deadline passed more than 10 s ago (for the case that the waiting worker died).

**Re-attach on resume.** Each request has a fingerprint (kind, tool, arguments, question). When a resumed run asks for something, `find_reusable` looks for an earlier request of the same run with the same fingerprint that is still pending, or was answered while no worker was waiting (`consumed_at` null). If found, the broker re-attaches to it (the card the approver already sees stays valid) and cancels any other leftover requests of the run as "superseded after the run resumed"; otherwise leftovers are cancelled and a new request is created. When a worker hands a run back or loses its lease it sets `keep_pending`, so its open requests survive the cancellation.

**Who may answer.** `answer_rights` in [worker/org_settings.py](../worker/org_settings.py) is the single decision, used both by `respond` and by the inbox:

| Kind | Rule |
|---|---|
| approval | approver, admin and owner; operator only when the org sets `operators_can_approve`; never a viewer |
| approval, separation of duties | the person who started the run may not *approve* its irreversible actions (and its writes with `sod_for_writes`); they may still deny |
| question | approver and above (operators included) |

**The inbox.** `GET /api/approvals` lists the org's requests across runs with `can_respond`, `can_approve` and a `reason` per item, computed for the caller. `WS /api/approvals/stream` pushes `{"type": "changed"}` whenever an approval of the org is raised or resolved (the hub's `watch_org`); the page refetches. No data travels over the socket.

In single-user mode the broker is the in-memory `InteractionBroker` (a future per request, no timeout, lost on restart); tests, evals and the CLI use `AutoBroker`.

## 6. Events and live streaming

Runner events are `(type, payload)`; every payload carries `run_id`. Each is stored as a row of `run_events` with a gapless per-run `seq`: `append_event` increments `runs.event_seq` under the run's row lock and inserts in one transaction together with `pg_notify('run_events', '<run>:<seq>')`, which is delivered when the transaction commits. Single-user mode appends to `events.jsonl` instead.

| Event | Payload (main fields) | Meaning |
|---|---|---|
| `run_queued` | `task`, `approval_mode`, `priority` | the API queued the run (multi-user mode) |
| `run_started` | `task`, `approval_mode` | the runner began |
| `run_requeued` | `reason` (`worker_lost`, `worker shutting down`), `worker`, `attempts` | the run went back to the queue |
| `run_resumed` | `resume_count`, `message` | a worker continued the run from its checkpoint |
| `thought` | `turn`, `text` | model's reasoning text (or condensed summary) for a turn |
| `plan_updated` | `plan`, `goal_summary`, `success_criteria` | `update_plan` applied |
| `tool_started` | `turn`, `tool`, `args`, `risk`, `action` | about to execute (after any approval) |
| `tool_finished` | all `Step` fields plus `phase` (`work` or `verify`) | step recorded, including denied, skipped, failed and verifier steps |
| `memory_updated` | `key`, `value`, `source`, `step`, `durable`, `scope` | `remember` stored a fact |
| `memory_recalled` | `keys` | organization notes injected for this run |
| `memory_saved`, `memory_rejected` | `keys` / `rejected` with reasons | the write gate's outcome after a verified run |
| `approval_requested` | `request_id`, `tool`, `action`, `args`, `risk`, `context {reason, facts, form_values}` | a person must approve or deny |
| `approval_resolved` | `response` (includes `by`), `answered`, `reason` | decision arrived, or none (expired, cancelled) |
| `question_asked`, `question_answered` | `request_id`, `question`, `options` / `response` | `ask_user` or guard escalation |
| `guard_triggered` | `guard` (`failures`, `repetition`, `budget`, `timeout`), `level` (`nudge`, `escalate`, `stop`), `message` | a guard fired |
| `verification_started`, `verification_finished` | `criteria` / `passed`, `inconclusive`, `checks`, `summary` | the audit |
| `usage` | `llm_calls`, `input_tokens`, `output_tokens`, `llm_retries`, `model`, `model_switches` | cumulative accounting |
| `model_switched` | `from`, `to` | the failover chain moved to another model |
| `llm_waiting` | `message`, wait budget | the model is unreachable (network); retrying |
| `run_finished` | `status`, `report` | terminal event |

Server-only: `replay_done` (`live` flag) and `error` (unknown run on the WebSocket). For runs that have no stored events (for example started from the CLI before events were logged) the server synthesizes them from the stored state (`worker/event_log.py`).

**Fan-out.** Every API process keeps **one** listening connection (`EventHub`) and serves the WebSockets watching runs:

1. a socket *subscribes first*: the hub starts a feed for the run and records the newest stored `seq` as its cursor;
2. then it reads the stored events (the replay) and sends them, then `replay_done`;
3. then it streams what the hub delivers, skipping any `seq` it already sent. No gap and no duplicate, whatever the timing;
4. on a NOTIFY the hub reads the run's new events from the database (`seq > cursor`) and queues them for its subscribers; it never trusts the payload beyond "something happened to this run". `run_finished` closes the stream.

A slow poll (`LIVE_FALLBACK_POLL_S`) and a re-read after every reconnect of the listening connection cover a lost NOTIFY, so a database restart only pauses the streams. Subscribing requires the run to belong to the caller's org; a foreign run is refused like an unknown one. In single-user mode the in-process `RunSession` fans out to its subscribers and replays from the file store with the same subscribe-then-replay-then-dedupe order.

## 7. Multi-tenancy and identity

**Identity.** Sign-in is e-mail and password (argon2id; the check also runs for unknown and deactivated accounts against a dummy hash). A session is a row in `auth_sessions`; the cookie `ofa_session` carries a random 256-bit token (HttpOnly, SameSite=Lax, Secure in `DEPLOY_MODE`) whose SHA-256 is the row id, so a database leak cannot be replayed. The idle expiry slides (extended at most once a minute); the absolute limit does not. Failed sign-ins are counted per e-mail hash and per address in `login_attempts` (fixed windows); over the limit the answer is 429 with `Retry-After`, and each failure is answered after an exponentially growing pause. On every request `AuthService.resolve` turns the cookie into an `OrgContext(org_id, user_id, email, role, ...)`: the role is read from the membership each time, and if the session's org is no longer available to the user, the session moves to their first active org.

**Org scoping, in three layers.**

1. *Authorization:* `require_role("operator")` and friends on every endpoint; roles are ordered viewer < approver < operator < admin < owner. The approval right is a separate capability (`can_approve`), because an operator may or may not approve depending on the org.
2. *Scoped stores:* `PgRunStore`, `ApprovalStore` and `OrgMemoryStore` are constructed for one organization and put `org_id` into every statement. A run id that exists in another org behaves exactly like one that does not exist: **404, never 403**, with an identical body. `PgRunStore` additionally checks run ownership before any child-table write. File serving looks the run up through the store first, so nothing on disk is touched for a foreign run.
3. *The worker:* it builds the store, the vault scope, the browser cookie, the LLM key and the memory handle from the **claimed run's** organization; nothing is taken from process-wide state.

The audit log (`AuthService.audit`) records sign-in, sign-out (and failed attempts), org switches, run start and cancel, approvals and answers, member and setting changes, credential changes, environment provision and reset, and memory changes.

**CSRF.** SameSite=Lax cookies plus a guard (`CsrfGuard`) refusing state-changing requests that the browser marks `Sec-Fetch-Site: cross-site`; requests without the header (scripts, tests) carry no ambient cookie and pass. WebSocket handshakes check that `Origin` matches the host.

**The simulated company as a tenant** ([company_sim/tenants.py](../company_sim/tenants.py), [tenant_web.py](../company_sim/tenant_web.py)). The simulator stands in for each customer's own external systems, so each organization has a private copy: its own SQLite database and invoice PDFs under `DATA_DIR/tenants/<id>/`, login sessions, chaos flags, generated passwords and ERP API key. A small index database maps tenant id to its secret entry token, API key, credentials and flags. A contextvar, `current_tenant`, selects the tenant for `db.connect()`, the PDF directory, sessions, chaos flags and credentials, set per request by `TenantMiddleware`. The tenant is resolved from, in order: the `sim_tenant` cookie (whose value is the entry token), the `X-API-Key` header on `/erp/api`, otherwise the `default` tenant (the original single company, which keeps the v1 paths and credentials). A tenant id is **never accepted from a client**, and `/admin` is never resolved from a cookie or key. The entry route `/t/<token>/` sets the cookie and redirects. Unknown or stale cookies are ignored.

**Provisioning and pinning** ([worker/provisioning.py](../worker/provisioning.py)). `POST /admin/tenants {tenant_id: "org-<slug>"}` creates (or finds) the tenant. The platform then stores its logins in the org's vault as `mail`, `portal`, `erp` and `erp_api`, and the entry token as the hidden credential `sim_entry`, and records `orgs.sim_tenant_id`. When a worker starts a run, `pin_environment` puts the `sim_tenant` cookie **into the run's browser context before the first page** (`ctx.extras["browser_init_cookies"]`, re-applied after a browser restart), and the agent is told the plain simulator URL, so it starts inside its own tenant and cannot reach another one even if it never uses an entry link. The ERP REST API needs no cookie: the tenant's API key (an `erp_api` credential used through `http_request(auth_credential="erp_api")`) identifies it. `SIM_ADMIN_TOKEN` can lock the simulator's `/admin/*`; the platform sends it as `X-Admin-Token` and never logs it.

## 8. Credential vault and redaction

**Vault** ([worker/vault.py](../worker/vault.py), [vault_pg.py](../worker/vault_pg.py)). `Vault` encrypts the field values of a credential into one Fernet token (`VAULT_KEY`) and hands an opaque record to a backend: a JSON file (single-user mode) or the `credentials` table. Only non-secret metadata (name, kind, description, URL and host hint, field *names*) is stored in clear. Credentials are owned by an organization id (or `default`). Kinds: `login` (username and password), `api_key` (sent as a named header, `X-API-Key` by default), `header`. A credential can carry a host; `allows_host` permits that host and its subdomains only.

**VaultScope.** At the start of a run the worker decrypts the org's credentials into an in-memory `VaultScope`, placed in `ctx.extras["vault_scope"]`. The agent-visible API of the scope (`names`, `public`, `get`) hides internal credentials (`sim_entry`, and the org's own LLM key `gemini`), which are reachable only through `internal()` for server code; their values are still part of the redactor's secret set.

**Using a secret without seeing it.**

- `list_credentials` returns names, kinds, descriptions, hosts and field names.
- `browser_fill_secret(ref, credential, field)` fills a text, e-mail, search, tel, url or password `<input>` (never a textarea) with the value, after checking the page host against the credential's host. The observation says `•••• (credential erp.password)`.
- `http_request(auth_credential=name)` adds the credential's authentication header at execution time, replacing any header the model set, only for an allowed host.

**Redaction choke points.** The `Redactor` ([worker/redaction.py](../worker/redaction.py)) is built from the scope's secret values (including the Basic-auth form of a login) and replaces each value and its common encodings (URL, HTML, JSON-escaped, base64) with `[REDACTED:<credential>.<field>]`, longest match first, in one pass. It is applied at:

1. every tool result (inside `execute_with_retry`, so the runner and the verifier get clean observations);
2. the runner: steps (in place), event payloads and the sink, approval payloads, the plan, facts, the report;
3. the complete message list before each LLM call, in the runner and in the verifier;
4. every write to the run store, through `RedactingStore`;
5. the task text when a run is queued, and log records (a log filter also scrubs database URLs, API-key and cookie headers, bearer tokens and the process's own secrets).

**Limits.** It matches exact values only (and the listed encodings; values shorter than 4 characters are skipped). It cannot catch a secret the model transforms (reversed, split, spelled out), or one rendered in an image: screenshots are saved as taken, and a page that displays a secret in plain text would put it in the picture.

## 9. Context construction

The prompt is rebuilt from state on every turn instead of appending to a chat transcript ([context.py](../worker/agent/context.py)).

```
[system]  SYSTEM_PROMPT (task-agnostic behaviour rules)
[user]    Task from the user
          Today's date / Environment (tool names, workspace folder, intranet start page, vault credential names)
          History so far (older steps, one line each, max 60 lines, 150 chars per line)
[assistant tool calls] \  the most recent whole turns, covering at least 6 steps,
[tool results]         /  properly paired by tool_call_id; older turns' results cut at 2000 chars,
...                       the latest turn's results shown in full
          + appended to the newest message:
          === CURRENT STATE (action N of at most MAX_STEPS) ===
          Goal / Success criteria / Plan (checklist with marks) / Working memory (with provenance)
          Organization knowledge (from past verified runs; verify before relying on it)   (only when recalled)
          VERIFIER FEEDBACK (unresolved)   (only after a failed audit)
          NOTICE: ...                      (guard nudges, text-only nudge, resume note)
```

Properties this buys:

- **Bounded cost.** Prompt size depends on the window and caps, not on run length.
- **Valid transcripts.** The window is cut on turn boundaries, so a tool result is never separated from its call (a provider error otherwise). Steps that are not tied to a tool call (approvals, system notes, verifier steps) never enter the transcript. `tests/test_agent_context.py` checks the pairing invariant over many histories.
- **Durable knowledge.** Anything the agent needs after its observation scrolls away must be in working memory or the plan; the system prompt instructs it to `remember` facts with sources as soon as it learns them.
- **Resumable.** The prompt is a pure function of the stored state plus the saved runtime blob, which is what lets another worker continue a run (section 3.3).
- **Persisted observations** are capped at 8,000 characters per step, and event payloads at 2,000.

## 10. State model

All in [state.py](../worker/agent/state.py); `RunState.to_dict()` is what `run.json` and the API return; `RunState.from_dict` is its inverse (tolerant of missing and unknown keys).

```
RunState
  run_id, task, status, goal_summary
  success_criteria: list[str]
  plan:   list[PlanItem {id, text, status: pending|in_progress|done|blocked|skipped, note}]
  facts:  dict[key -> Fact {key, value, source, step, durable, scope}]
  steps:  list[Step]
  usage:  {llm_calls, input_tokens, output_tokens, model, model_switches}
  final_report: dict | None
  started_at, finished_at

Step
  index, kind: llm | tool | approval | ask_user | verify | system
  thought, thought_is_summary, tool, args, risk (read|write|irreversible)
  ok, observation, error, evidence: list[path], duration_ms, retries
  timestamp, turn (which LLM reply produced it), tool_call_id (links to the model's call)
```

Step `error` values that are outcomes of policy or flow rather than tool malfunctions (`denied`, `skipped`, `verification_failed`) are excluded from failure counting by the guards and the report statistics. The runner's hidden state is not part of `RunState`; it is the `runtime` blob (section 3.3).

The `RunStore` protocol ([store.py](../worker/store.py)) is the only way the runner persists anything: `create_run`, `save_state` (header), `append_step`, `upsert_fact`, `append_event`/`read_events`, `save_runtime`/`load_runtime`, `save_report`/`load_report_markdown`, `load_state`, `list_runs`, `artifact_dir`/`artifact_file`. `FileRunStore` and `PgRunStore` pass the same contract tests. Steps and events are appended as rows; the old behaviour of rewriting the whole document on every step exists only in the file store.

## 11. Browser observation and the browser pool

The agent never receives pixels. [snapshot.js](../worker/tools/snapshot.js) runs in the page, tags each visible interactive element with `data-agent-ref="N"`, and returns text (headings marked, tables as `|`-separated rows, links and buttons annotated with their ref) plus one record per element. [snapshot.py](../worker/tools/snapshot.py) formats it. Limits per view: 3,500 characters of text and 80 elements; longer pages are split into parts reachable with `browser_scroll`, or searched with `browser_read_page(find=...)`.

Real output from the simulated vendor portal, page 1 of the invoice list:

```
URL: http://localhost:8001/portal/invoices
Title: Invoices - Supplier Billing Portal
--- Page text ---
# Invoices
Vendor
Filter[4]
Invoice # | Vendor | Issue date | Status
GLX-88213[5] | Globex Corporation | 2026-09-19 | Open
INI-20417[6] | Initech Supplies | 2026-08-02 | Paid
CX-2026-0388[7] | Company X Ltd | 2026-07-20 | Paid
UMB-5531[8] | Umbrella Logistics | 2026-09-26 | Open
STK-1207[9] | Stark Components | 2026-08-31 | Paid
Page 1 of 3
Next[10]
(14 invoices)
--- Interactive elements ---
[1] link "Invoices" -> /portal/invoices
[2] button "Sign out"
[3] combobox "Vendor" selected="All vendors" options=[All vendors, Company X Ltd, Globex Corporation, Initech Supplies, Stark Components, Umbrella Logistics]
[4] button "Filter"
[5] link "GLX-88213" -> /portal/invoices/GLX-88213
[6] link "INI-20417" -> /portal/invoices/INI-20417
[7] link "CX-2026-0388" -> /portal/invoices/CX-2026-0388
[8] link "UMB-5531" -> /portal/invoices/UMB-5531
[9] link "STK-1207" -> /portal/invoices/STK-1207
[10] link "Next" -> /portal/invoices?page=2
```

And the ERP bill form (note the select options and format hints):

```
--- Interactive elements ---
[4] combobox "Vendor" selected="-- Select vendor --" options=[-- Select vendor --, Company X Holdings, Company X Ltd, Globex Corporation, Initech Supplies, Stark Components, Umbrella Logistics]
[5] textbox "Invoice number"
[6] textbox "Amount"
[7] combobox "Currency" selected="USD" options=[USD, EUR, GBP]
[8] textbox "Issue date" placeholder="YYYY-MM-DD"
[9] textbox "Due date" placeholder="YYYY-MM-DD"
[10] textbox "Notes"
[11] button "Save bill"
[12] link "Cancel" -> /erp/bills
```

Notes that matter for correctness:

- The list shows only issue date and status. Amount and due date require opening the detail page or the PDF, which the agent must download (`browser_download` then `read_file`, which extracts PDF text with pypdf).
- Refs are re-assigned on every snapshot. A ref that is not on the current page raises a `StaleRefError` whose message tells the agent to re-read the page.
- Invalid fields appear as `INVALID: <message>`; page alerts appear in an `Alerts:` header line; a 4xx/5xx navigation status is shown in the header.
- Side effects are reported as notes in the next observation: dialogs are auto-accepted and noted, new tabs are followed, downloads are saved to `runs/<id>/downloads/` and reported as `Downloaded: downloads/<file>`.
- Screenshots are written to `runs/<id>/screenshots/` after clicks, selects, submits and `browser_screenshot`, and attached to the step as evidence. They are for humans, not for the model.
- Not supported: iframes (skipped by the snapshot), file upload inputs, canvas-only content. The browser is Chromium at 1280x800; headed mode adds a 150 ms slow-motion delay for demos (headed is for local development; a deploy-mode server is always headless).

Browser tools: `browser_navigate`, `browser_click`, `browser_type`, `browser_select`, `browser_press`, `browser_scroll`, `browser_back`, `browser_read_page`, `browser_screenshot`, `browser_download`, plus `browser_fill_secret` (section 8).

**The browser pool** ([browser_pool.py](../worker/tools/browser_pool.py)). Launching Chromium costs about a second and a few hundred MB, so a process shares one: `BrowserPool` starts the Playwright driver and one Chromium per headless mode lazily, and every run's `BrowserSession` takes its own `BrowserContext` (separate cookies, storage, pages, downloads) and releases only that context when it ends. The session is stored per run in `ctx.extras["browser"]`; the tool instances are stateless and shared by all runs. `BROWSER_MAX_CONTEXTS` (default 8) caps open contexts per process; a run waits up to `BROWSER_ACQUIRE_TIMEOUT_S` and then fails with a clear error. A crashed Chromium is detected on the next acquire and relaunched (counted in a metric); contexts of the dead browser are gone, and their sessions tell the agent the browser was restarted. A run's tenant cookie is added to its fresh context before any page exists. Playwright objects belong to the event loop that made them, so there is one pool per running loop.

## 12. Risk classification and approval policy

`Risk` has three levels: `read`, `write`, `irreversible`. A tool declares a default; `Tool.classify(args, ctx)` can refine it per call and reads the **calling run's** state from `ctx`, never from the shared tool instance (tool instances serve every run in the process).

| Tool / action | Classification |
|---|---|
| `list_files`, `read_file`, `list_credentials`, `recall`, `browser_navigate`, `browser_scroll`, `browser_back`, `browser_read_page`, `browser_screenshot`, `browser_download` | read |
| `browser_fill_secret` | read (filling commits nothing; the submit click is classified) |
| `write_file` | write |
| `http_request` GET, HEAD | read |
| `http_request` POST, PUT, PATCH | write |
| `http_request` DELETE | irreversible |
| `browser_click` on a link | read, unless the label is an action word: write for submit/confirm, irreversible for delete/remove/pay/approve/transfer |
| `browser_click` on a text box, checkbox, radio, select, tab, slider | read |
| `browser_click` on a button whose name matches delete/remove/pay/payment/approve/transfer | irreversible |
| `browser_click` on a button named like sign in/log in/log out/search/filter/find | read |
| `browser_click` on a button named submit/save/create/confirm/send/post/sign/reply/place order/cancel order | write |
| `browser_click` on a form-submitting button with any other name | write, unless named next/previous/back/apply/reset/clear/show/view/go/load more |
| `browser_press` Enter or Space, `browser_type` with `submit=true` | the highest risk among the submit buttons of the focused form (read if a textarea or no submit button) |
| `browser_type`, `browser_select` without submit | read |
| `remember`, `update_plan`, `finish`, `ask_user` | internal; never gated |

Classification of a click uses the element's role and accessible name from the latest snapshot of the same run (`classify_element` in [browser.py](../worker/tools/browser.py)); an unknown ref is a write: unknown is never downgraded to read.

Approval policy ([policy.py](../worker/agent/policy.py)):

| Mode | Asks for |
|---|---|
| `auto` | irreversible only |
| `confirm_writes` (default) | write and irreversible |
| `confirm_all` | everything except internal tools and reads that are `read_only_safe` |

- In multi-user mode the mode is the organization's setting; an operator may request a stricter one for a run, never a looser one.
- The runner applies the policy before executing; the model has no way to bypass it, and it does not need to request approval (there is deliberately no `request_approval` tool).
- The approval card carries `action` (a one-line description with exact values, with password input masked), `args`, `risk`, and context: the model's stated reason, the current working memory, and for a form submit the field values about to be sent (`form_values`, redacted).
- The human can approve, deny with a note, or approve with "remember" so similar calls (same tool and risk) skip the prompt for the rest of the run. Irreversible calls are always asked individually. The cache is part of the checkpoint.
- Denial returns `User DENIED this action. Note: ...` as the observation, and the prompt instructs the agent to adapt rather than retry unchanged.
- Who may answer, expiry and separation of duties are covered in section 5. Single-user dashboards have no approval timeout; the CLI's `AutoBroker` asks at the terminal; in evals a scripted responder approves or denies.

## 13. Verifier

Goal: catch "the agent says it is done but the system disagrees".

- **Trigger:** `finish(status="success")`. Other statuses skip the audit and are reported as `failed` or `needs_attention`.
- **Fresh context:** the auditor gets a new two-message conversation: its own system prompt and a briefing with the original task, the success criteria, the worker's claim (summary, outputs, evidence) and the worker's memory facts, all labelled unverified. It does not see the worker's step history or reasoning.
- **Read-only tools:** `registry.subset(read_only=True)` keeps the tools with `read_only_safe=True` plus those flagged `verifier_allowed` (a flag on `Tool`; today only `browser_fill_secret`). That excludes `write_file`, `browser_type`, `browser_select`, `browser_press`, `remember`, `update_plan`, `finish`, `ask_user`. Further layers apply: any call whose per-call risk is not `read` is refused, and `ctx.extras["read_only"]` makes the browser tools refuse state-changing clicks and `http_request` refuse everything except GET.
- **Read-only sign-in.** The auditor often needs a session the worker does not have to give it: after a crash and resume the fresh browser context has none, and it could not see the portal. So in read-only mode the auditor may authenticate, and nothing else. `browser_fill_secret` works in read-only mode only on the fields of a *login form*: an `<input>` (text, e-mail or password) inside a form whose visible submit controls are **all** classified read (Sign in, Log in, Search; at least one) and which has a password field or a sign-in button. The "Sign in" click is classified read, so it passes. Everything else stays refused in read-only mode: other typing, selects, write and irreversible clicks. (`browser_type` is not in the verifier's toolset; the read-only check would permit only the username field of a login form without submit, but the verifier does not receive that tool today.) The credential's host binding still applies and the values never reach the model. The verifier prompt tells it to sign in with `list_credentials` + `browser_fill_secret` when it meets a login page.
- **Output:** it must call `submit_verdict(passed, checks[{criterion, passed, observed}], summary)`. The runtime recomputes `passed`: a verdict with no checks, or with any failed check, is downgraded to failed.
- **Budget:** at most 12 turns; on the last turn only `submit_verdict` is offered. Running out of turns, or an LLM failure, yields an *inconclusive* verdict, which the runner treats as `needs_attention` (never as success).
- **Feedback loop:** a failed verdict becomes `VERIFIER FEEDBACK (unresolved)` in the worker's state block along with an instruction to fix, re-check and `finish` again. After more than two failed audits the run ends `failed`.
- **Recording:** verifier tool calls are stored as steps of kind `verify` (emitted with `phase: "verify"`), do not count against the worker's budget or failure counters, and its token usage is added to the run. Its conversation is redacted like the worker's.
- **Known weakness:** the auditor shares the worker's browser context (it needs the page state) and is the same model family as the worker. The load test saw resumed runs fail an audit that could not open the portal (SCALING.md, "bugs found"); the sign-in capability fixes that at the platform level: after a crash and resume the first audit now passes. The policy agent's second-round "audit_resync" workaround (re-open the source after a failed audit) remains only as a fallback.

## 14. Guards

[guards.py](../worker/agent/guards.py), configured by `MAX_STEPS` (60), `MAX_CONSECUTIVE_FAILURES` (4) and `RUN_TIMEOUT_S` (1800); in multi-user mode the organization's `max_steps` and `run_timeout_s` replace the first and the last.

| Guard | Trigger | Response |
|---|---|---|
| Step budget | `MAX_STEPS` real actions (tool calls except `remember`, `update_plan`, `finish`, `ask_user`), or 3x that many steps in total | Stop: `needs_attention` with a partial report |
| Timeout | **active** time exceeds `RUN_TIMEOUT_S` (the clock is paused while the run waits for a person) | Stop: `needs_attention` |
| Failure streak | consecutive failed tool calls reach half the limit (2 by default) | One nudge: re-read state, check the error, change approach, consider the API path |
| Failure streak | reaches `MAX_CONSECUTIVE_FAILURES` | Escalate: ask the human "Keep trying / Stop the task"; guidance is injected into the next prompt and counters reset |
| Repetition | same tool, same arguments and same observation 3 times within the last 6 steps | First time a nudge; second time escalate |
| Cancel | `POST /api/runs/{id}/cancel` | Stop at the next safe point; also releases a pending approval or question |

Denials, skipped calls, rejected finishes and `ask_user` are not counted as failures. The counters and the accumulated active time are part of the checkpoint, so a resumed run continues its budget instead of starting a new one.

## 15. Reliability matrix

| Failure | Detected by | Response | Where |
|---|---|---|---|
| Timeout, connection reset, detached element, `net::ERR_*` on an idempotent or read-level call | Exception type and message match, Playwright timeout | Tool layer retries with backoff (0.5 s, 1 s; 1 or 2 retries per tool); observation notes "after N automatic retries" | [retry.py](../worker/tools/retry.py), [browser.py](../worker/tools/browser.py) |
| HTTP 5xx from `http_request` (GET, HEAD, OPTIONS) | Status code | Marked transient and retried; if still failing the 5xx body is shown to the model | [http.py](../worker/tools/http.py) |
| HTTP 5xx, timeout or dropped connection on POST, PUT, PATCH, DELETE | Method plus status or exception | Never auto-retried (the write may have partly happened); non-transient failure with the note to GET and check whether the record exists before retrying | [http.py](../worker/tools/http.py), [retry.py](../worker/tools/retry.py) |
| Timeout or closed page on a state-changing browser action (click, press or typed submit classified above read) | Risk classification at call time | Never auto-retried; non-transient failure telling the agent to re-read the page and check whether the action took effect. Read-level actions keep the retry | [browser.py](../worker/tools/browser.py), [retry.py](../worker/tools/retry.py) |
| Browser or page closed (read-level action) | Error text match | Session reset, retried; next observation says the browser restarted | [browser.py](../worker/tools/browser.py) |
| Chromium crashed | Pool sees the browser disconnect | Relaunch on the next acquire; the run's session notices and tells the agent the page is gone | [browser_pool.py](../worker/tools/browser_pool.py) |
| No free browser context | Pool slot wait times out | Clear error to the run (`BROWSER_MAX_CONTEXTS`) | [browser_pool.py](../worker/tools/browser_pool.py) |
| Server error on a UI form submit (injected 500) | Page shows an error alert, status 500 in the header | Model reads the alert and retries the submit or switches to the ERP REST API | prompt guidance |
| Validation error (wrong date format, bad amount) | `INVALID` marker and alert in the snapshot | Semantic error: the model fixes the value (the prompt says to convert formats) and resubmits | snapshot, prompt |
| Stale element ref | `StaleRefError` | Message says refs were renumbered; model calls `browser_read_page` | [browser.py](../worker/tools/browser.py) |
| Wrong control for the action (typing into a select, clicking a text field) | Pre-checks in `browser_type`, `browser_select` | Error names the right tool and lists valid options | [browser.py](../worker/tools/browser.py) |
| Session expiry | Redirect to login page with an "expired" notice | Model logs in again with the vault credentials | prompt, [secrets.py](../worker/tools/secrets.py) |
| Data missing where expected (invoice not in portal) | Search finds nothing | Model re-plans (`update_plan`) and looks in email attachments | prompt |
| Duplicate record, including two runs of one org creating it at once | Prompt requires search-before-create; ERP rejects duplicates (409 / form error) | Model reports it or asks, instead of forcing a second entry (load-tested with 10% repeated tasks: no duplicate bills) | prompt, simulator |
| Ambiguity (two plausible vendors) | Model judgement guided by the prompt | `ask_user` with options | [interaction.py](../worker/tools/interaction.py) |
| Action denied by the human | `denied` step | Observation tells the model not to retry unchanged | [runner.py](../worker/agent/runner.py) |
| Nobody answers an approval | Deadline passes (database clock) | Request expires; the run stops `needs_attention` with the reason | [pg_broker.py](../worker/agent/pg_broker.py), [approvals_pg.py](../worker/approvals_pg.py) |
| Two people answer the same approval | Conditional update | First writer wins, the second gets 409 | [approvals_pg.py](../worker/approvals_pg.py) |
| NOTIFY lost or listener disconnected | Poll fallbacks, reconnect with resync | Latency of a few seconds, no lost answer or event | [pg_listen.py](../worker/pg_listen.py), [live.py](../worker/live.py) |
| Worker crashes or is killed | Lease expires; another worker's reaper | Run re-queued and resumed from the checkpoint in a fresh browser; after `JOB_MAX_ATTEMPTS` claims `needs_attention` | [jobqueue.py](../worker/jobqueue.py), [jobs.py](../worker/jobs.py) |
| Worker loses its lease but is alive | Heartbeat returns "not ours" | Writes fenced, run cancelled locally | [jobs.py](../worker/jobs.py) |
| Worker stopped on purpose | SIGINT / SIGTERM | Runs handed back (attempt not counted), or drained with `--drain` | [jobs.py](../worker/jobs.py) |
| API restarts | none needed | Sessions and approvals are rows; open WebSockets drop and reconnect with replay | [runs_api.py](../worker/runs_api.py), [live.py](../worker/live.py) |
| Database briefly unavailable | Exceptions in claim, heartbeat, limiter, usage | Claim loop logs and retries; heartbeat tolerates up to one lease; the shared limiter falls back to a local throttle; usage and budget reads fail open | [jobs.py](../worker/jobs.py), [limiter.py](../worker/llm/limiter.py), [usage.py](../worker/usage.py) |
| Resumed run's audit meets a login page | Auditor has a fresh browser | Read-only sign-in through the vault (the first audit passes); a failed audit still feeds back as a fallback | [verifier.py](../worker/agent/verifier.py), [secrets.py](../worker/tools/secrets.py) |
| Model returns prose instead of a tool call | No tool calls in response | Nudge and force `tool_choice="any"`; second time `needs_attention` | [runner.py](../worker/agent/runner.py) |
| Model names an unknown tool or sends invalid arguments | JSON Schema validation | One corrective re-prompt inside `ResilientLLM`; the runner also reports unknown tools | [resilient.py](../worker/llm/resilient.py) |
| Gemini 429 or 5xx, malformed function call | Mapped to `RateLimitError` / `TransientLLMError` | Exponential backoff with jitter, honouring the server's retry hint, up to 5 retries; then `needs_attention` | [gemini.py](../worker/llm/gemini.py), [resilient.py](../worker/llm/resilient.py) |
| A model's daily quota is gone | `QuotaExhaustedError` | The failover chain skips it until the reset; shared across processes | [failover.py](../worker/llm/failover.py), [health_pg.py](../worker/llm/health_pg.py) |
| The organization's daily LLM budget is used up | `QuotaGuard` before each call | Run ends `needs_attention` with a plain message | [usage.py](../worker/usage.py) |
| Repeated failures or identical actions | Guards | Nudge, then ask the human | [guards.py](../worker/agent/guards.py) |
| Step or time budget exhausted | Guards | Stop with a partial, honest report | [runner.py](../worker/agent/runner.py) |
| False success (claim not true in the system) | Verifier | Feedback loop; `failed` after repeated failed audits | [verifier.py](../worker/agent/verifier.py) |
| Verifier cannot decide | Out of turns or LLM error | `needs_attention`, reported as unverified | [verifier.py](../worker/agent/verifier.py) |
| Bug inside the runner or the worker | Exception handlers in `run()` and `_execute` | Run ends `failed` with the partial report intact | [runner.py](../worker/agent/runner.py), [jobs.py](../worker/jobs.py) |
| UI or event consumer broken | Exception in the event sink | Logged and ignored; the run continues | [runner.py](../worker/agent/runner.py) |

## 16. LLM layer

- [base.py](../worker/llm/base.py): neutral `Message`, `ToolSpec`, `ToolCall`, `LLMResponse`, `Usage`, and the error hierarchy (`LLMError`, `RateLimitError`, `TransientLLMError`, `QuotaExhaustedError` for a model's own daily quota, `QuotaExceededError` for the organization's budget, `ModelUnavailableError`). One method: `complete(messages, tools, temperature, tool_choice)`.
- [gemini.py](../worker/llm/gemini.py): `google-genai` with native function calling and automatic function calling disabled (the runner owns the loop). Converts schemas to the subset Gemini accepts (drops unsupported keys, turns free-form objects into JSON-encoded strings and decodes them on return), echoes the provider's `thought_signature` on later turns, merges consecutive tool results, maps SDK errors and extracts retry hints.
- [ollama.py](../worker/llm/ollama.py): `/api/chat` with tools; `tool_choice="any"` is advisory only.
- [failover.py](../worker/llm/failover.py): `FailoverLLM` walks the model chain. A daily quota error marks the model unusable until the next quota reset (midnight Pacific time), a 404 or persistent 503 skips it (until reset, or for 60 s), one 503 gets a quick retry, connectivity errors never mark a model. The memory of unusable models is a `ModelHealth`.
- [resilient.py](../worker/llm/resilient.py): wraps any client with a limiter, retry with backoff, tool-argument validation with one corrective re-prompt, counters, and three optional seams used in multi-user mode: `limiter` (decides when a call may start), `quota` (raises before a call when the org's budget is gone) and `usage` (records every call). Network outages are retried on a time budget (`LLM_NETWORK_RETRY_S`) with progress reported to the UI.
- [limiter.py](../worker/llm/limiter.py): `PgRateLimiter`, one token bucket per API-key hash in `llm_limits`, shared by every run of every process. Capacity `LLM_BUCKET_CAPACITY` (2), refill `LLM_REFILL_PER_S` or `1 / LLM_MIN_INTERVAL_S`. `acquire` takes the row `FOR UPDATE` for one short transaction using the database clock (`clock_timestamp()`); when the bucket is empty it commits, **sleeps outside the transaction** for the deficit plus jitter, and tries again, so a waiting run holds neither a lock nor a connection. If the database is unreachable it falls back to a per-instance throttle.
- [health_pg.py](../worker/llm/health_pg.py): `PgModelHealth` keeps the synchronous `ModelHealth` interface (it is read in tight loops and properties) with a small in-process cache over the `model_health` table; writes go through at once, reads refresh at most every `LLM_HEALTH_CACHE_S` (3 s).
- [usage.py](../worker/usage.py): `UsageRecorder` inserts one `usage_events` row per call (a failure is logged, never fails the run); `QuotaGuard` compares the org's UTC-day totals with `max_tokens_per_day` and `max_llm_calls_per_day` before each call, caching the totals for `LLM_QUOTA_CACHE_S` and bumping them locally per call (one call may overshoot; unreadable accounting fails open). Reports back the Usage card.
- [factory.py](../worker/llm/factory.py): `make_llm(settings, *, org_id, user_id, run_id, sessionmaker, vault_scope, org_settings)`. The API key is the org's own `gemini` vault credential when it has one, else `GEMINI_API_KEY`; everything shared is keyed by the SHA-256 of the key. The model chain is the org's `gemini_models` override or the deployment's `GEMINI_MODELS`.
- [fake.py](../worker/llm/fake.py): `ScriptedLLM` for tests; `LLM_PROVIDER=fake` selects an empty one.
- [policy.py](../worker/llm/policy.py): `PolicyLLM`, a deterministic rule-based stand-in that drives the real tools for the invoice task (and acts as its auditor); `LLM_PROVIDER=policy`, used by [scripts/loadtest.py](../scripts/loadtest.py) (see [SCALING.md](SCALING.md)). It reads the same prompt a model would, sleeps `POLICY_THINK_MS` per call to imitate latency, and goes through the same limiter and usage accounting.

## 17. Organization memory

Long-term, org-wide knowledge about the organization's *systems*, never task data ([memory_store.py](../worker/memory_store.py), [memory_api.py](../worker/memory_api.py), [tools/memory_tools.py](../worker/tools/memory_tools.py)).

- **Candidates.** The agent calls `remember(key, value, source, durable=true, scope="erp")`. That only marks the fact in the run's own `run_facts` (so it survives a resume).
- **The write gate.** After `AgentRunner.run()` returns, the worker calls `commit_run_memory`, the single place that writes to `org_memory`. It acts only if the final state is `succeeded`, which the runner reports only after the independent audit passed. It lives in the worker, next to the code that knows the database, the org and the run's creator; the runner stays storage-agnostic and only sees an optional `ctx.extras["org_memory"]` with an async `recall`. A lost or handed-back run never commits.
- **Screening** (`screen`): in order, a secret (a redaction marker, or the run's redactor still finds one), a bad key (3 to 80 characters), content over 500 characters or under 3 words, a scope over 40 characters, task data (at least 40% of the whitespace tokens contain a digit, or at least 30% of the characters are digits), and text that reads like an instruction to the agent (a pattern such as "ignore ... instructions", "system prompt", "new instructions"). At most 10 candidates are saved per run. The saving is an upsert on `(org, lower(key))` that replaces content, scope, provenance and creator, bumps `updated_at` and clears an expiry. Outcomes become `memory_saved` and `memory_rejected` events (keys and reasons, never the rejected text) and an audit row.
- **Recall.** `websearch_to_tsquery('english', ...)` ranked by `ts_rank_cd` over the generated `search` column (key weight A, content B, scope C), then use count, then recency. Mode `auto` tries "all words" and falls back to "any word" (an OR query), and goes straight to "any" for a long text such as a task. Only `verified`, unexpired notes are returned; each recalled note increments `use_count` and sets `last_used_at`, which also keeps it from expiring. This is keyword search by design: no embeddings, no vector store.
- **Injection.** At run start (and again after a resume) the best `memory_recall_limit` notes for the task text go into `self._knowledge` and the state block under "Organization knowledge (from past verified runs; verify before relying on it)", with scope and age; a `memory_recalled` event names the keys. The `recall(query, scope?)` tool searches on demand.
- **Governance.** Notes carry provenance (source run, step, source text, creator). Admins list, search, edit (content, scope, expiry), delete and expire stale notes from the Memory page; every change is audited. Notes unused for `memory_ttl_days` (180 by default) get an expiry: they stop being recalled but stay visible, flagged. The sweep runs when a run commits memory and when an admin asks; there is no scheduled job. Setting `memory_recall_limit` to 0 turns memory off for the org (no recall, no commit).

## 18. Simulated company

`python -m company_sim` serves, on port 8001:

- `/mail`: shared AP inbox (login), message view, PDF attachment download, compose and reply (writes to the Sent folder).
- `/portal`: vendor billing portal (login). Five invoices per page in internal-id order (not date order); the list shows number, vendor, issue date, status. Detail page and PDF show amount, due date and line items.
- `/erp`: "Ledger" accounts payable (login). Bills list and search, "New AP bill" form (vendor dropdown, invoice number, amount with at most 2 decimals, currency, issue and due dates strictly `YYYY-MM-DD`, due not before issue), duplicate rejection on (vendor, invoice number), approve action. REST API under `/erp/api` (`X-API-Key`; summary at `/erp/api/docs-summary`).
- `/admin`: chaos flags, reset, ground-truth state, and tenant management (`/admin/tenants[...]`: create, list, inspect, reset, delete, per-tenant state and chaos). Open for local use; `SIM_ADMIN_TOKEN` makes every `/admin` request require an `X-Admin-Token`.
- `/t/<token>/`: a tenant's entry link (section 7).

Data is seeded deterministically relative to "today" ([seed.py](../company_sim/seed.py), with a per-tenant salt so tenants differ); chaos flags are in [chaos.py](../company_sim/chaos.py) and are per tenant. See the README's reference section for credentials and flag descriptions. The simulator is a replaceable environment: the worker only knows a start URL, a workspace folder, an HTTP allowlist and, in multi-user mode, the org's vault scope and tenant cookie.

## 19. HTTP API

Single-user mode (no auth, or the shared `ACCESS_PASSWORD` gate): `POST /api/runs {task, approval_mode?, headed?}` (409 while a run is active), `GET /api/runs`, `GET /api/runs/{id}`, `POST /api/runs/{id}/respond {request_id, approved|answer, note?, remember?}`, `POST /api/runs/{id}/cancel`, `WS /api/runs/{id}/events`, `GET /runs/{id}/files/...`, `GET /api/scenarios`, `POST /api/sim/reset`, `GET /api/sim/chaos`, `GET /api/health`, `GET /api/config`.

Multi-user mode serves the same paths with different semantics (a run is queued, not started; everything is org-scoped and role-checked) plus `/api/auth/*`, `/api/approvals` and its stream, `/api/memory`, `/api/usage/me`, and `/api/admin/*` (members, settings, credentials, environment, audit, usage, system). The complete table with roles is in [MULTI_USER.md](MULTI_USER.md#10-http-api). `/healthz`, `/readyz` and `/metrics` exist in both modes.

## 20. Extension points

### Add a tool

Subclass `Tool` ([base.py](../worker/tools/base.py)), set `name`, `description`, a JSON Schema `parameters`, and optionally `risk`, `read_only_safe`, `verifier_allowed`, `max_retries`, `classify(args, ctx)`, `retry_safe(args, ctx)`, `no_retry_note(args)` and `describe_action(args, ctx)`. Return a `ToolResult`; raise for failures (exceptions are converted to results, and timeouts, connection errors and 5xx are retried unless `retry_safe` is False, which is the right answer for state-changing calls). Register it in `build_registry` in [toolset.py](../worker/agent/toolset.py). Tool instances are shared by every run in the process: keep per-run state in `ctx.extras` (as the browser tools do), never on the tool.

```python
class LookupTool(Tool):
    name = "lookup_order"
    description = "Fetch an order by id from the order system."
    parameters = {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}
    # risk defaults to READ and read_only_safe to True, so the verifier may use it too

    async def run(self, args, ctx):
        ...
        return ToolResult(ok=True, observation="...")
```

Set `read_only_safe = False` for anything that changes state so the verifier never receives it, and set `risk` (or override `classify`) so the approval gate covers it. A tool that needs a secret should read it from `ctx.extras["vault_scope"]` and return only masked text (see [secrets.py](../worker/tools/secrets.py)); the run's redactor is a second line of defence, not the first. The Gemini schema sanitizer test (`test_all_registered_tool_specs_are_gemini_compatible`) will flag schemas the provider cannot accept.

### Add an LLM provider

Implement `LLMClient.complete` in `worker/llm/<name>.py` returning an `LLMResponse` with parsed `ToolCall`s and `Usage`, raising `RateLimitError` or `TransientLLMError` for retryable failures. Add a branch in [factory.py](../worker/llm/factory.py) and the name to the `LLM_PROVIDER` literal in [config.py](../worker/config.py). `ResilientLLM` wraps it automatically, and in multi-user mode the shared limiter, the usage ledger and the org budget apply through the hooks `make_llm` passes (add the provider's own limiter key in the branch if it has a different quota than a Gemini key).

### Point it at a different web application

Single-user mode, nothing in the agent is specific to the simulator:

1. Set `SIM_BASE_URL` to the new start page (it is given to the model as the intranet start page and used to resolve relative `browser_navigate` paths).
2. Put the logins and any notes the agent needs in `WORKSPACE_DIR`, or in the vault (`python -m worker.vault put ...`), which is better: the agent then never sees them.
3. Add the new API hosts to `HTTP_ALLOWED_HOSTS` if the agent should call them.
4. Leave prompts and tools unchanged.
5. For evals, add a scenario YAML and a checker in [evals/checkers.py](../evals/checkers.py) (register it in `CHECKERS`).

Multi-user mode additionally expects each organization to have an *environment*: a run starts only when the org has a `sim_tenant_id` and a `sim_entry` credential (`load_org_environment`; otherwise HTTP 409). For a real application replace `provision_org_environment` / `OrgEnvironment` in [provisioning.py](../worker/provisioning.py) with your own: what a run actually needs is the org's vault scope (store the app's logins as credentials with host limits) and, optionally, cookies to pre-seed into the browser context (`browser_cookies`).

### Swap the artifact store

There is no `ArtifactStore` class yet; the seam is two methods of the `RunStore` protocol, `artifact_dir(run_id)` and `artifact_file(run_id, rel_path)`, which return local paths. Screenshots and downloads are written by the tools into `ctx.run_dir` (the directory `artifact_dir` returns), the report collects evidence by scanning it, and `GET /runs/{id}/files/...` serves `artifact_file` with `FileResponse`. To use object storage: make `artifact_dir` a local scratch directory and upload what a step produced (or the whole directory when the run ends); make `artifact_file` fetch or redirect to a signed URL; and change the file route to return a redirect instead of a `FileResponse`. Until then, workers and API processes must share the filesystem path in `RUNS_DIR` for files to be visible everywhere.

### Change policy

Set `APPROVAL_MODE` (or, in multi-user mode, the organization's `approval_mode`), or adjust the label patterns in [browser.py](../worker/tools/browser.py) (`_IRREVERSIBLE_RE`, `_WRITE_RE`, `_BENIGN_RE`) for an application whose buttons are named differently. Tests for the matrix are in `tests/test_agent_policy.py` and `tests/test_browser_tool.py`. New per-organization settings are added in [org_settings.py](../worker/org_settings.py) (`EDITABLE`, defaults, validation); the admin page renders the form from them.

## Deployment topology (optional, opt-in)

For a hosted demo the container runs two processes under `deploy/start.py`: the simulated company on `127.0.0.1:8001`
(internal only, a single process because chaos flags and sessions live in memory) and the worker on `0.0.0.0:$PORT`
(the one public port). With `PUBLIC_SIM_PROXY=1` the worker streams `/mail`, `/portal`, `/erp`, `/static` and a `/company`
alias to the sim (cookies and redirects preserved, `Location` rewritten, `/admin/*` never exposed); the agent's browser
still talks to the sim directly. With `ACCESS_PASSWORD` set, an ASGI gate (`worker/deploy.py`) requires a signed session
cookie on every route including the WebSocket; `/api/health` stays public but minimal. `DEPLOY_MODE=1` forces headless
runs and `MAX_RUNS_PER_DAY` caps runs per UTC day (HTTP 429). Disk is assumed ephemeral. None of this changes local
`python run.py` behavior.

Without a `DATABASE_URL` the container stays the single-user shape described above. With one it is the multi-user
shape: `deploy/start.py` (`ROLE=all`) starts the simulator, optionally migrates, runs `python -m worker.provisioning resync`
(the simulator's SQLite data is ephemeral, so every organization's tenant is created again and its logins in the vault
rewritten), starts the API and then `python -m worker.jobs`; on SIGTERM the job worker stops first so it can hand its runs
back to the queue. Hosted on Google Cloud Run it runs as one instance (the simulator lives inside the container) against a
managed PostgreSQL such as Neon, with secrets from Secret Manager and a GitHub Actions pipeline that migrates, deploys
without traffic, smoke tests and then switches traffic. Artifacts are still local files (section 20). See
[DEPLOY.md](DEPLOY.md) for the topology, the settings and why. In multi-user mode `ACCESS_PASSWORD` and the global
`MAX_RUNS_PER_DAY` are ignored (users sign in with their own accounts and the quota is per organization).

## Observability

Logs, metrics and health checks work in both modes (file mode and database mode). Code: `worker/logging_setup.py`,
`worker/metrics.py`, `worker/observability.py`, `worker/system_api.py`, `worker/health.py`.

### Logs

`LOG_FORMAT=json` (the default when `DEPLOY_MODE` is on) writes one JSON object per line: `ts` (UTC, ms), `level`,
`logger`, `msg`, the context fields below, any `extra=` fields, and `exc` for a traceback. `LOG_FORMAT=text` (the default
otherwise) is the readable `time LEVEL logger: message` line with the context appended (`[run=ab12cd34 org=... worker=...]`).
`LOG_LEVEL` sets the level (default `INFO`). The API (`python -m worker.server`, or `uvicorn worker.server:app`) and the
workers (`python -m worker.jobs`) call `setup_logging`; uvicorn's own access log is replaced by one request line from the
middleware (`GET /api/runs -> 200 in 12 ms`; health checks and `/metrics` only at `DEBUG`).

**Context** is carried in `contextvars`, so it follows asyncio tasks without being passed around: `request_id` (the
middleware accepts a sane `X-Request-ID`, otherwise makes one, and echoes it in the response), `org_id` and `user_id` (bound by
the auth dependency), and `run_id`, `org_id`, `user_id`, `worker_id` (a worker binds them around the tasks of each run it
claims; in file mode the runner binds `run_id`). Every log line written while they are bound carries them.

**Redaction safety net.** Secrets are kept out of logs in addition to the run-level redaction of stored data and prompts. A
log record factory scrubs each record *when it is created*, so every handler (stderr, a collector, pytest's caplog) only sees
clean text. It masks the message and its arguments, tracebacks, and string `extra` fields:

* the password in any `scheme://user:password@host` URL (so a database URL in an error message is safe);
* `X-API-Key`, `X-Admin-Token`, `Authorization`, `Cookie` / `Set-Cookie` header values, `Bearer ...` tokens, Google API keys,
  and `password=`, `api_key=`, `secret=` style pairs;
* the exact values of this process's own secrets (`GEMINI_API_KEY`, `VAULT_KEY`, `SESSION_SECRET`, `SIM_ADMIN_TOKEN`,
  `METRICS_TOKEN`, `ACCESS_PASSWORD`, the database password), registered by `setup_logging`;
* the vault secrets of every active run: the runner registers its `Redactor` when `run()` starts and removes it when it ends,
  so a tool or library that logs a credential is masked as `[REDACTED:erp.password]`.

This is a net, not a license to log secrets: it cannot catch a secret the code transforms first.

### Metrics

Every process keeps its own Prometheus registry (`prometheus-client`). **Scrape every process:** in database mode the run,
tool, LLM and approval series come from the workers, the HTTP, WebSocket and queue-depth series from the API.

| Metric | Type, labels | Meaning |
| --- | --- | --- |
| `oneforall_runs_total` | counter `status` | finished runs by final status |
| `oneforall_run_duration_seconds` | histogram | active time of a run, human waits excluded |
| `oneforall_runs_active` | gauge | runs executing in this process |
| `oneforall_queue_depth` | gauge `org` | queued runs per org slug (an API process samples the DB every `QUEUE_SAMPLE_S`, 15 s; at most 50 orgs, the rest as `_other`; with several API copies use `max`, not `sum`) |
| `oneforall_queue_wait_seconds` | histogram | `queued_at` to the first claim |
| `oneforall_tool_calls_total` | counter `tool`, `ok` | tool executions |
| `oneforall_tool_duration_seconds` | histogram `tool` | tool time, retries included |
| `oneforall_llm_calls_total` | counter `model`, `outcome` | per attempt: `ok`, `rate_limited`, `network`, `transient`, `malformed`, `error` |
| `oneforall_llm_tokens_total` | counter `model`, `direction` | `input` / `output` tokens |
| `oneforall_llm_latency_seconds` | histogram `model` | time inside the provider call |
| `oneforall_llm_ratelimit_wait_seconds` | histogram | time spent in `limiter.acquire()` |
| `oneforall_approvals_total` | counter `kind`, `outcome` | `approved`, `denied`, `answered`, `expired`, `cancelled`, `no_response` |
| `oneforall_approval_wait_seconds` | histogram | how long a run waited for a person |
| `oneforall_worker_leases_lost_total` | counter | runs this worker lost to the reaper |
| `oneforall_runs_requeued_total` | counter `reason` | `worker_lost`, `shutdown` |
| `oneforall_runs_resumed_total` | counter | runs continued from a checkpoint |
| `oneforall_browser_contexts_active` | gauge | open isolated browser contexts |
| `oneforall_browser_relaunches_total` | counter | Chromium relaunches after a crash |
| `oneforall_ws_connections` | gauge | open WebSockets |
| `oneforall_http_requests_total` | counter `route`, `method`, `status` | `route` is the path template (`/api/runs/{run_id}`), unmatched paths share `unmatched` |
| `oneforall_http_request_duration_seconds` | histogram `route` | request time |

The process, GC and Python platform collectors are exposed too.

Where it is measured: `AgentRunner` (`RunObserver`, tool calls, approvals, the `_interact` wait), `ResilientLLM` (call
attempts, tokens, latency, limiter wait), `jobqueue` (queue wait at claim, requeues, runs ended without a runner),
`jobs.Worker` (lost leases), `BrowserPool` (contexts, relaunches), and one ASGI middleware (`worker/observability.py`) for
HTTP and WebSockets.

**Scraping.** API: `GET /metrics`. Workers: `python -m worker.jobs --metrics-port 9101` serves
`http://127.0.0.1:9101/metrics` (loopback only; off unless the flag is given). Access to the API's `/metrics`:

* an `Authorization: Bearer <METRICS_TOKEN>` header (for the scraper), or a signed-in admin/owner (database mode), or the
  logged-in session when `ACCESS_PASSWORD` is set (file mode);
* otherwise it is open **only for local development**: `DEPLOY_MODE` off, no `METRICS_TOKEN`, no `ACCESS_PASSWORD`. Any of
  those makes it 401 without credentials (403 for a signed-in non-admin). A deployed server without a token therefore serves
  `/metrics` to admins only.

```yaml
scrape_configs:
  - job_name: oneforall-api
    metrics_path: /metrics
    authorization: { credentials: <METRICS_TOKEN> }
    static_configs: [{ targets: ["api-host:8000"] }]
  - job_name: oneforall-workers
    static_configs: [{ targets: ["127.0.0.1:9101"] }]   # one target per worker process
```

### Health and the admin summary

* `GET /healthz`: liveness, no dependencies, always fast.
* `GET /readyz`: readiness. Database reachable, migrations at the Alembic head, and the simulated company answering its
  `/healthz` (turn that off with `READINESS_CHECK_SIM=0`). `503` with a per-check reason (never a URL or credential) when
  something is missing. A worker logs the same checks as a readiness line at start. `/api/health` is unchanged.
* `GET /api/admin/system` (admin role, database mode): queue depth, running and waiting runs per org, the workers currently
  holding a run (distinct `lease_owner` with a recent heartbeat), runs finished in the last 24 hours by status, and the p50 /
  p95 run time from the `runs` table (wall time, approval waits included). An org admin sees their own org only; admins whose
  e-mail is in `PLATFORM_ADMIN_EMAILS` see every org. The Admin page shows it as a read-only "System" card that refreshes
  every 15 seconds.
