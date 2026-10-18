<a id="top"></a>

<p align="center">
  <img src="docs/assets/banner.svg" alt="OneForAll Agent: give it a business task in plain words. It does the work in real systems, asks before it changes anything, and proves the result." width="100%">
</p>

<p align="center">
  <a href="https://github.com/adieladaniel/OneForAll-Agent/actions/workflows/ci.yml"><img src="https://github.com/adieladaniel/OneForAll-Agent/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/adieladaniel/OneForAll-Agent/actions/workflows/security.yml"><img src="https://github.com/adieladaniel/OneForAll-Agent/actions/workflows/security.yml/badge.svg" alt="Security"></a>
  <img src="https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white" alt="Python 3.11">
  <img src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/PostgreSQL-18-4169E1?logo=postgresql&logoColor=white" alt="PostgreSQL 18">
  <img src="https://img.shields.io/badge/Playwright-Chromium-2EAD33?logo=googlechrome&logoColor=white" alt="Playwright with Chromium">
  <img src="https://img.shields.io/badge/Gemini-free%20tier-8E75B2?logo=googlegemini&logoColor=white" alt="Gemini free tier">
  <img src="https://img.shields.io/badge/Google%20Cloud%20Run-4285F4?logo=googlecloud&logoColor=white" alt="Google Cloud Run">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT"></a>
</p>

<p align="center">
  <a href="#1-setup-and-run"><b>Quick start</b></a> &nbsp;·&nbsp;
  <a href="#how-it-works"><b>How it works</b></a> &nbsp;·&nbsp;
  <a href="#11-evaluation"><b>Evaluation</b></a> &nbsp;·&nbsp;
  <a href="#16-optional-hosting-it-on-google-cloud-run"><b>Deploy</b></a> &nbsp;·&nbsp;
  <a href="#documentation"><b>Docs</b></a>
</p>

<br>

Give it a business task in plain language, for example *"Find the latest invoice from Company X, extract the amount and due date, enter it into our internal system, and tell me once it is done."* It plans the work, drives a real browser and other tools, recovers from failures, asks for approval before changing anything, checks its own result with an independent verifier, and reports with evidence.

It runs against a simulated company (mail inbox, vendor billing portal, internal ERP) that contains deliberate traps, so every behaviour is exercised against real, failing, ambiguous systems instead of mocks. The agent runtime itself is task-agnostic: nothing in `worker/agent/` or `worker/tools/` knows about invoices.

**v1** was a single-user prototype. **v2** (this version) keeps all of that and adds what a team would need to run it: organizations with users and roles, PostgreSQL storage, a job queue with worker processes, durable approvals answered by a second person, an encrypted credential vault, governed organization memory, metrics and logs, and a load test that measures it. Both modes ship in the same code: with no database configured it works like v1 (one local user, files, one run at a time).

<!-- Demo video: <add link> -->

## At a glance

<table>
  <tr>
    <td align="center" width="25%"><img src="https://img.shields.io/badge/tests-676-4f46e5?style=for-the-badge" alt="tests: 676"><br><sub>automated tests, run with a scripted model and no API key</sub></td>
    <td align="center" width="25%"><img src="https://img.shields.io/badge/soak%20runs-280%2F280-0f766e?style=for-the-badge" alt="soak runs: 280/280"><br><sub>succeeded and passed ground truth, through a worker kill and an API restart</sub></td>
    <td align="center" width="25%"><img src="https://img.shields.io/badge/isolation%20leaks-0-0f766e?style=for-the-badge" alt="isolation leaks: 0"><br><sub>across 47,508 responses in the soak (about 118,000 scanned in all profiles)</sub></td>
    <td align="center" width="25%"><img src="https://img.shields.io/badge/roles-5-4f46e5?style=for-the-badge" alt="roles: 5"><br><sub>viewer, approver, operator, admin, owner, with separation of duties</sub></td>
  </tr>
  <tr>
    <td align="center" width="25%"><img src="https://img.shields.io/badge/eval%20scenarios-8-4f46e5?style=for-the-badge" alt="eval scenarios: 8"><br><sub>judged by deterministic ground-truth checkers instead of an LLM</sub></td>
    <td align="center" width="25%"><img src="https://img.shields.io/badge/database-1-4169E1?style=for-the-badge" alt="database: 1"><br><sub>PostgreSQL 18 is the queue, the event bus, the rate limiter and the system of record</sub></td>
    <td align="center" width="25%"><img src="https://img.shields.io/badge/agent%20frameworks-0-475569?style=for-the-badge" alt="agent frameworks: 0"><br><sub>no LangChain or similar; the runner is one file of about 700 lines</sub></td>
    <td align="center" width="25%"><img src="https://img.shields.io/badge/modes-2-475569?style=for-the-badge" alt="modes: 2"><br><sub>single user with files, or multi-user with PostgreSQL, from the same code</sub></td>
  </tr>
</table>

The load-test figures come from [docs/SCALING.md](docs/SCALING.md). They were measured on one machine with the deterministic stand-in model instead of Gemini, so they show the platform's structure (queueing, leases, approvals, recovery, isolation) and not model quality. The caveats are in [section 7](#7-scale-and-proof).

## A real run

These screenshots were captured from a local run of the headline task (find the latest invoice, enter it in the ERP, verify it), driven by the deterministic stand-in model (`LLM_PROVIDER=policy`) so that no Gemini quota was used.

<p align="center">
  <img src="docs/assets/screens/dashboard-run.png" alt="The dashboard while a run is in progress: the live timeline of tool calls, the live browser view, the plan and the working memory." width="100%"><br>
  <sub><b>The dashboard mid-run.</b> Each tool call appears in the live timeline; the right column shows the browser, the plan and the working memory. Logins show as dots: the tool types the credential, and the model never sees it.</sub>
</p>

<table>
  <tr>
    <td width="50%" align="center">
      <img src="docs/assets/screens/approval-card.png" alt="The approval card for the Save bill click, listing the exact values about to be submitted."><br>
      <sub><b>The approval gate.</b> Before the write, the card shows the exact action and the form values.</sub>
    </td>
    <td width="50%" align="center">
      <img src="docs/assets/screens/final-report.png" alt="The final report: the independent audit's passed checks, the agent's evidence, evidence screenshots and run statistics."><br>
      <sub><b>The report.</b> The independent audit's checks, the agent's evidence, screenshots and run stats.</sub>
    </td>
  </tr>
</table>

## How it works

```mermaid
flowchart LR
    U["Browser dashboard"] -->|"HTTPS + WebSocket"| A["API (FastAPI)"]
    A -->|"SQL + LISTEN/NOTIFY"| P[("PostgreSQL: queue, state, events, vault, memory")]
    P -->|"claim with SKIP LOCKED"| W["Workers + Chromium"]
    W -->|"real browser, files, HTTP"| C["Simulated company: mail, portal, ERP"]
```

The API never runs a task. It writes a run into PostgreSQL, a worker claims it, and every step, event and checkpoint goes back to the same database. The architecture is described in [section 2](#2-architecture).

One run, from the task to the report:

```mermaid
sequenceDiagram
    autonumber
    actor O as Operator
    participant R as Runner
    participant M as Model
    participant T as Tools
    actor H as Approver
    participant V as Verifier
    O->>R: task in plain words
    R->>M: goal, success criteria and plan
    loop every turn
        R->>M: prompt rebuilt from stored state
        M-->>R: tool calls
        R->>R: classify the risk of each call
        opt write or irreversible action
            R->>H: approval card with the exact values
            H-->>R: approve or deny
        end
        R->>T: execute, retry transient errors only
        T-->>R: observation
    end
    R->>V: claim of success
    V->>T: re-check the real systems, read only
    V-->>R: verdict with one check per criterion
    R-->>O: report with evidence
```

**Plan.** The agent states the goal, checkable success criteria and a plan. **Act.** It uses a real browser, files and HTTP, and code (not the model) decides which calls need a human. **Verify.** A separate auditor with read-only tools re-observes the result, and a failed audit goes back into the loop. **Report.** A summary with evidence, the audit checks and screenshots.

## Highlights

<table>
  <tr>
    <td width="33%" valign="top">🛡️ <b>Approval gate</b><br>Risk is classified in code, not by the model. Writes and irreversible actions pause for a human, and a denial goes back to the agent as an observation.</td>
    <td width="33%" valign="top">✅ <b>Independent verifier</b><br>A fresh-context auditor with read-only tools re-checks the real systems before a run is allowed to count as succeeded.</td>
    <td width="33%" valign="top">🔐 <b>Credential vault</b><br>Credentials are encrypted at rest. The model sees names, tools use the values, and a redactor scrubs everything that is stored or sent.</td>
  </tr>
  <tr>
    <td valign="top">♻️ <b>Job queue with resume</b><br>A <code>SKIP LOCKED</code> queue with leases and heartbeats. When a worker dies, its run resumes from the last checkpoint on another one.</td>
    <td valign="top">🧠 <b>Org memory with a write gate</b><br>Shared how-to notes are written only after a run succeeded and its audit passed, and each note is screened first.</td>
    <td valign="top">🏢 <b>Multi-tenant simulated company</b><br>Every organization gets its own mail, vendor portal and ERP, with deliberate traps and chaos flags that inject failures.</td>
  </tr>
  <tr>
    <td valign="top">🔒 <b>Organization isolation</b><br>Another organization's run, file or approval answers 404, never 403, and the load test found no leak in about 118,000 scanned responses.</td>
    <td valign="top">📈 <b>Observability</b><br>A live timeline, structured logs, Prometheus <code>/metrics</code>, <code>/healthz</code> and <code>/readyz</code>, and an admin System card.</td>
    <td valign="top">🚀 <b>CI/CD to Cloud Run</b><br>GitHub Actions lint, test on PostgreSQL 18 and build the image, then deploy to staging and production with a smoke-tested traffic switch and a rollback workflow.</td>
  </tr>
</table>

## Contents

| | |
|---|---|
| [1. Setup and run](#1-setup-and-run) | [11. Evaluation](#11-evaluation) |
| [2. Architecture](#2-architecture) | [12. Testing](#12-testing) |
| [3. Design decisions and why](#3-design-decisions-and-why) | [13. Configuration reference](#13-configuration-reference) |
| [4. Security model](#4-security-model) | [14. Known limitations](#14-known-limitations) |
| [5. Reliability](#5-reliability) | [15. What I would build next](#15-what-i-would-build-next) |
| [6. Memory](#6-memory) | [16. Assumptions](#16-assumptions) |
| [7. Scale and proof](#7-scale-and-proof) | [17. Models, APIs, frameworks and components used](#17-models-apis-frameworks-and-components-used) |
| [8. Observability](#8-observability) | [18. Project structure](#18-project-structure) |
| [9. Demo](#9-demo) | [19. Reference: simulated company](#19-reference-simulated-company) |
| [10. Capability map](#10-capability-map) | [20. Troubleshooting](#20-troubleshooting) |

### Documentation

| Document | What it covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How it is built |
| [docs/MULTI_USER.md](docs/MULTI_USER.md) | Operating the multi-user mode |
| [docs/DEPLOY.md](docs/DEPLOY.md) | The Cloud Run deployment |
| [docs/SCALING.md](docs/SCALING.md) | Load-test results and limits |
| [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) | The recording plan |

---

## 1. Setup and run

Requirements: Python 3.11+ (developed on 3.11.9 / Windows 11), about 400 MB of disk for Chromium, and a free Gemini API key. The multi-user mode also needs PostgreSQL (developed and load-tested on 18; it uses generated columns, `websearch_to_tsquery` and `DROP DATABASE ... WITH (FORCE)`, so use version 13 or newer).

### Two modes

| | Mode 1: quick start | Mode 2: multi-user |
|---|---|---|
| Needs | Python, Chromium, Gemini key | the same, plus PostgreSQL |
| Storage | files under `runs/`, vault in `.vault/` | PostgreSQL (runs, steps, events, approvals, users, vault, memory, usage) |
| Sign-in | none (or one shared `ACCESS_PASSWORD`) | e-mail and password, organizations, five roles |
| Runs | one at a time, inside the API process | queued, executed by separate worker processes, many in parallel |
| Approvals | in memory, wait forever, lost on restart | database rows with expiry, an inbox, separation of duties |
| Selected by | `DATABASE_URL` empty (or `AUTH_MODE=file`) | `DATABASE_URL` set (`AUTH_MODE=auto` or `db`) |

> [!NOTE]
> A `DATABASE_URL` in your `.env` switches `python run.py` to Mode 2 automatically. To stay in Mode 1 leave it out, or set `AUTH_MODE=file`.

### 1.1 Install

Windows (PowerShell):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium     # on Linux you may also need: python -m playwright install-deps chromium
```

Two extra files support development and CI: [requirements-dev.txt](requirements-dev.txt) (ruff, pip-audit, actionlint-py) and [constraints.txt](constraints.txt) (the exact versions the suite is verified against; the same `playwright` version as the Dockerfile). To get exactly what CI uses:

```bash
pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt
```

### 1.2 Get a free Gemini API key and configure `.env`

1. Open <https://aistudio.google.com/apikey> and sign in with a Google account.
2. Click **Create API key**. No billing details are needed for the free tier.
3. Copy the template and paste the key:

```powershell
copy .env.example .env      # macOS/Linux: cp .env.example .env
```

```ini
LLM_PROVIDER=gemini
GEMINI_API_KEY=your-key-here
# optional: the fallback chain of models (this is the default in worker/config.py)
GEMINI_MODELS=gemini-3.5-flash,gemini-3.6-flash,gemini-3.7-flash,gemini-2.5-flash,gemini-3.8-flash,gemini-3.5-flash-lite,gemini-2.5-flash-lite
```

All other settings have working defaults; the important ones are listed in [section 13](#13-configuration-reference) and every one is documented in [.env.example](.env.example).

### 1.3 Mode 1: quick start (single user, no database)

```bash
python run.py            # add --open to launch the browser tab for you
```

This starts the simulated company on <http://localhost:8001> and the worker dashboard on **<http://localhost:8000>**, waits for both to be healthy, and stops both on Ctrl+C. The simulator creates its SQLite database and invoice PDFs on first start (`company_sim/data/`).

In the dashboard: pick an example task (or type your own), optionally switch on **Headed browser** to watch Chromium work, and press **Run task**. Approval cards and clarification questions appear inline. The simulated apps can be browsed by hand at <http://localhost:8001> (credentials are in [section 19](#19-reference-simulated-company)).

CLI alternative (no dashboard), with the simulator running (`python -m company_sim` in another terminal, or `python run.py`):

```bash
python -m worker.agent "Find the latest invoice from Company X, enter it into our internal system, and tell me when done." --headed
python -m worker.agent "..." --approval confirm_all      # auto | confirm_writes | confirm_all
```

Events print to the terminal, and approvals and questions are answered at the prompt (`y` / `n` / `a` for "always for similar"). Exit code is 0 only when the run `succeeded`. The same run files are written to `runs/<id>/`.

### 1.4 Mode 2: multi-user (PostgreSQL)

> [!WARNING]
> The demo organizations, users and passwords exist for local development only. Choose your own demo password, and do not reuse it anywhere shared.

1. **Install PostgreSQL** and note the `postgres` password (on Windows the EDB installer with the default port 5432 is fine). The bootstrap connects to the `postgres` maintenance database to create the application database, so the role needs the right to create databases.
2. **Point `.env` at it:**

   ```ini
   DATABASE_URL=postgresql+asyncpg://postgres:YOUR_PASSWORD@localhost:5432/oneforall
   ```

   (`postgresql://` and `postgres://` are accepted too; the password is masked in every log line and message.)
3. **Create, migrate and seed the database.** Choose the demo password yourself so you do not have to catch it in the output:

   ```bash
   python scripts/bootstrap.py --demo-password "choose-a-password"
   ```

   This creates the database named in `DATABASE_URL` (`oneforall` above) if it is missing, applies the Alembic migrations, and seeds two demo organizations. Without `--demo-password` a random one is generated and **printed once**, the first time the users are created; re-running never changes existing passwords.

   | Organization | Users (all `@acme.test` / `@globex.test`) |
   |---|---|
   | **Acme Corp** (`acme`) | `owner@`, `admin@`, `operator@`, `approver@`, `viewer@acme.test` |
   | **Globex** (`globex`) | `operator@`, `approver@globex.test` |

   Each organization has its own simulated company (its own mail, portal and ERP data and logins), which is created in the next step.
4. **Start everything, then give the demo orgs their simulated companies:**

   ```bash
   python run.py --workers 2                    # simulator :8001, API + dashboard :8000, two worker processes
   python scripts/bootstrap.py --provision-sim  # in a second terminal; safe to repeat
   ```

   `--provision-sim` asks the running simulator for one isolated tenant per demo org and stores that tenant's logins, encrypted, in the vault. (Without a running simulator it prints a note and skips; an org admin can do the same later with **Admin > Provision environment**.) Runs cannot start in an org until its environment exists.
5. **Sign in** at <http://localhost:8000>. The best way to see it: open one window as `operator@acme.test` and a private window as `approver@acme.test`, start a task as the operator, and approve it as the approver. The operator cannot approve the irreversible actions of their own run (separation of duties).

The API only queues runs; the worker processes execute them. You can also start the pieces yourself, on one machine or several that reach the database:

```bash
python -m company_sim                                                 # the simulated company
python -m worker.server                                               # API + dashboard (127.0.0.1:8000)
python -m worker.jobs --concurrency 4 --worker-id w1 --metrics-port 9101   # a worker; start as many as you like
python -m worker.db check                                             # connection, server version, migration state
python -m worker.db migrate                                           # apply pending migrations
```

The vault key must be the same for the API and every worker. Without `VAULT_KEY` a development key is generated into `.vault/.vault_key` on first use, which works when everything runs from the same checkout; set `VAULT_KEY` (`python -m worker.vault generate-key`) for anything shared. Details of roles, organization settings, the approvals inbox and operations are in [docs/MULTI_USER.md](docs/MULTI_USER.md).

### 1.5 Optional: local models with Ollama

```bash
ollama pull qwen2.5:3b
# .env
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen2.5:3b
OLLAMA_URL=http://localhost:11434
```

The Ollama adapter uses `/api/chat` with native tool calling behind the same interface as Gemini. Expect much weaker reliability from small models; see [decision 3.12](#312-gemini-free-tier-behind-a-provider-adapter).

### 1.6 Optional: hosting it on Google Cloud Run

The `Dockerfile` builds one image with two shapes. Without a `DATABASE_URL` it is the single-user demo (simulated company plus API on one public port). With one it is the multi-user deployment: `deploy/start.py` (`ROLE=all`) starts the simulator, re-creates every organization's simulated company, starts the API and a job worker, and GitHub Actions deploys it to Cloud Run with a free Neon PostgreSQL, Secret Manager, a smoke-tested traffic switch and a rollback workflow. The full runbook, including the one-time setup script and the costs, is in [docs/DEPLOY.md](docs/DEPLOY.md).

---

## 2. Architecture

### 2.1 The system (multi-user mode)

```mermaid
flowchart TD
    B["Browser dashboard: runs, approvals inbox, memory, admin, credentials"]
    A["API service: FastAPI, stateless, any number of copies (python -m worker.server)"]
    P[("PostgreSQL: system of record, job queue, event bus")]
    W["Worker service(s): K concurrent runs per process (python -m worker.jobs)"]
    S["Multi-tenant simulated company: one isolated SQLite dataset per org, one process"]
    B -->|"HTTPS + WebSocket, cookie session"| A
    A -->|"SQL + LISTEN/NOTIFY"| P
    W -->|"claim: SELECT FOR UPDATE SKIP LOCKED, lease + heartbeat"| P
    P -.->|"NOTIFY wake-ups"| W
    W -->|"one shared Chromium per worker, one isolated browser context per run"| S
```

PostgreSQL holds: `orgs`, `users`, `memberships`, `auth_sessions`, `login_attempts`, `runs`, `run_steps`, `run_facts`, `run_events`, `approvals`, `org_memory`, `credentials` (encrypted), `usage_events`, `llm_limits`, `model_health` and `audit_log`.

The API process never executes a run. A run is a row in PostgreSQL that moves through a state machine; workers claim it, execute it with the task-agnostic runner and write every step, event and checkpoint back to the same database. Postgres alone does the work that would otherwise need a broker and a cache: `FOR UPDATE SKIP LOCKED` is the job queue, `LISTEN/NOTIFY` carries live events and approval answers between processes, and `now()` is the one clock that leases, expiry and rate limits agree on (see [decision 3.14](#314-postgresql-alone-no-redis-no-broker)).

### 2.2 One agent run (the same in both modes)

```mermaid
flowchart TD
    subgraph runner["AgentRunner (worker/agent/runner.py), one per task"]
        ctx["context.py: build the prompt from state"]
        llm["LLM: Gemini chain, Ollama or a stand-in"]
        pol["policy.py: risk class + approval mode"]
        gate{{"Human approval, via the broker"}}
        exe["retry.py: run the tool, retry safe transient errors"]
        st["state.py: record the step"]
        gd["guards.py: budget, timeout, repetition"]
        ver["verifier.py: read-only audit"]
    end
    ctx --> llm --> pol
    pol -->|"approval needed"| gate --> exe
    pol -->|"allowed"| exe
    exe --> st --> gd
    gd -->|"next turn"| ctx
    gd -->|"finish with status success"| ver
    ver -->|"audit failed, at most 2 correction rounds"| ctx
    ver -->|"audit passed"| done(["Run succeeded: report and evidence"])
```

| Part | What it does |
|---|---|
| `context.py` | Rebuilds the prompt from `RunState` every turn |
| LLM | `ResilientLLM` over Gemini (model chain), Ollama, or the scripted and policy stand-ins |
| `policy.py` | Risk class plus approval mode lead to the approval gate (a human, via the broker) |
| `retry.py` | Executes the tool and auto-retries transient errors that are safe to repeat |
| `state.py` | `Step`, `Fact` and `PlanItem` recorded through a `RunStore` after every step |
| `guards.py` | Failure streak, repetition, step budget, timeout (human waits do not count) |
| `verifier.py` | Fresh-context auditor with read-only tools, and its verdict |
| redaction | Vault secrets are scrubbed from everything stored, emitted or sent to the model |

Tools: `browser_*` (Playwright), `list_files`, `read_file`, `write_file`, `http_request`, `remember`, `recall`, `list_credentials`, `browser_fill_secret`, `update_plan`, `ask_user`, `finish`.

One turn of the loop: build context from state, ask the LLM for tool calls, classify each call's risk, ask a human if policy requires it, execute with transient-retry, record the step, check guards. When the agent calls `finish(status="success")`, a separate verifier re-checks the real systems before the run is allowed to count as succeeded.

### 2.3 Run lifecycle

```mermaid
stateDiagram-v2
    [*] --> queued: operator POST /api/runs
    queued --> running: a worker claims it (SKIP LOCKED, lease)
    queued --> cancelled: cancel
    running --> waiting_approval: approval needed
    waiting_approval --> running: answered
    running --> waiting_user: question for a person
    waiting_user --> running: answered
    running --> verifying: agent claims success
    verifying --> running: failed audit, at most 2 correction rounds
    verifying --> succeeded: audit passed
    running --> queued: worker lost or stopped, resume from the checkpoint
    running --> failed
    running --> needs_attention
    running --> cancelled: cancel
    succeeded --> [*]
    failed --> [*]
    needs_attention --> [*]
    cancelled --> [*]
```

- **queued:** inserted by the API in one transaction with the daily-quota check (advisory lock per org) and a `run_queued` event.
- **claimed / running:** a worker takes the oldest, highest-priority queued run whose org still has a free slot (`max_concurrent_runs`), sets a lease (`JOB_LEASE_S`, 30 s by default), and builds the run from the database: org settings, the org's vault scope, a store scoped to the org, a `PgBroker`, an LLM with the shared limiter. The heartbeat renews the lease every third of it.
- **waiting approval / waiting user:** the run writes an `approvals` row and waits on `NOTIFY`; the wall-clock guard is paused meanwhile.
- **verifying:** the independent audit; a failed audit feeds back into the loop.
- **requeue and resume:** if the worker dies, the lease expires, another worker's reaper puts the run back in the queue and the next claim resumes it from its last checkpoint in a fresh browser context (after `JOB_MAX_ATTEMPTS` = 3 claims it ends `needs_attention`). A worker that is shut down on purpose hands its runs back without using up an attempt.
- **final:** the report and a `run_finished` event are stored; organization memory is committed only for `succeeded`.

### 2.4 NOTIFY channels

A notification is only a wake-up; every consumer re-reads the database and also polls slowly, so a lost NOTIFY costs latency, never correctness. A listener that loses its connection reconnects and re-reads what it may have missed.

| Channel | Payload | Sent when | Heard by |
|---|---|---|---|
| `run_events` | `<run>:<seq>` | an event was stored | API processes (fan out to WebSockets) |
| `runs_queued` | `<run>` or `slot` | a run was queued, re-queued, or a slot freed | workers (wake the claim loop) |
| `run_control` | `<run>:cancel` | someone cancelled a running run | the worker holding it |
| `approvals` | `<org>:<approval id>` | an approval or question was raised | API processes (the inbox's push channel) |
| `approval_responses` | `<org>:<approval id>` | it was answered, expired or cancelled | the waiting worker, and the API (inbox) |

One step-by-step sequence diagram of the agent loop, the event model, the data model and the reliability matrix are in **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## 3. Design decisions and why

### 3.1 A task-agnostic runtime, tested with several unrelated tasks

The agent loop, prompts, tools and verifier contain no invoice logic. The task arrives as user text; the agent infers success criteria and a plan on its first turn (`update_plan`). Task-specific knowledge lives only in the simulated company and in the evals. The proof is the eval suite: invoice entry, replying to a vendor email, and a multi-item "backfill this month" task run on identical code. Outside `evals/`, the simulator, `scripts/loadtest.py` and `worker/llm/policy.py` (the load-test stand-in, which has to know the task it plays), invoice wording appears only in the fallback preset list the dashboard shows when no scenario YAML is found ([worker/server.py](worker/server.py)), in file-name examples inside tool descriptions, and in comments.

### 3.2 Text DOM snapshots with element refs, not screenshots

Every browser action returns a compact text observation: URL, title, alerts, page text with tables as pipe rows, and numbered interactive elements (`[7] button "Save bill"`). Refs are stamped into the page as `data-agent-ref` and renumbered on every snapshot, so a stale ref produces a clear error instead of a wrong click. Why not vision:

- **Cost and free tier:** a snapshot is capped at about 3,500 characters of text plus 80 element lines per view (very roughly a thousand tokens); images cost more and burn the free quota and rate limit faster.
- **Reliability:** the agent clicks `[11]`, not a pixel coordinate. Form values, select options (with the valid list), `INVALID: <message>` markers, and validation alerts are exact text, not something to be read off a picture.
- **Portability:** works with any model that has tool calling, including text-only local models.
- Screenshots are still taken on state-changing actions and saved as human-readable evidence (not sent to the model).

The cost is that canvas-only UIs, iframes and visually encoded information are invisible to the agent (see limitations).

### 3.3 Rebuild context from state every turn, instead of a growing chat history

[worker/agent/context.py](worker/agent/context.py) builds each prompt from the explicit `RunState`: system prompt; task and environment; one-line summaries of older steps (capped at 60 lines); the last 6+ steps as whole turns with properly paired tool-call and tool-result messages (only the latest turn's observations are shown in full); and a state block with goal, success criteria, plan, working memory, recalled organization knowledge, verifier feedback and guard notices. Reasons: the token cost stays bounded across a 40-step run on a free tier, old page dumps do not drown the signal, and cutting history by whole turns avoids orphaned tool results that providers reject. A test property-checks the pairing invariant over many histories. In v2 this design has a second payoff: because the prompt is a pure function of stored state, a run that lost its worker can continue on another one with nothing but the database.

### 3.4 Working memory with provenance

The agent calls `remember(key, value, source)` when it learns something it will need later; the state block shows it back every turn with its source and step number. This is what lets old observations be dropped safely, and the provenance is shown on approval cards, passed to the verifier, and exported in the report. It is also a hallucination check: a value with no source is visibly suspicious.

### 3.5 Risk classification plus a runtime-enforced approval policy

The LLM never decides whether to ask. Each tool declares a risk, and calls can be reclassified by arguments: `http_request` GET is read, POST/PUT/PATCH write, DELETE irreversible; browser clicks are classified from the element's role and accessible name (a button named "Save bill" is a write, "Delete" or "Pay" is irreversible, "Next" or "Search" is a read); pressing Enter or `type(submit=true)` is classified by the form's submit buttons. [worker/agent/policy.py](worker/agent/policy.py) maps risk plus the approval mode to a pause, and the runner blocks on a human answer before the call executes. The card shows the exact action, the form values about to be submitted and the memory behind it. A denial returns to the agent as an observation, and it must adapt. Prompt-only "please ask before writing" fails the moment the model forgets; this cannot be skipped. Honest caveat: browser classification is heuristic on labels (see limitations). Classification reads the calling run's own page (it takes the run's context, not a shared "last page" pointer), which is what makes it safe with many runs in one process.

### 3.6 An independent verifier with fresh context and read-only tools

When the agent claims success, [worker/agent/verifier.py](worker/agent/verifier.py) runs a separate, short LLM conversation. It sees only the task, the success criteria, the claim, and the memory facts; it does not see the agent's reasoning. It has only the read-only subset of the tools and must re-observe the world (open the bill, compare amount and due date, look for duplicates) and submit one check per criterion with what it actually saw. A verdict with no checks, or any failed check, is downgraded to failed. This targets the classic failure of agents that report success after a form silently did not save. Failed audits feed their findings back into the loop (up to 2 correction rounds); an audit that cannot complete is reported as unverified (`needs_attention`) instead of passed.

The auditor can **sign in**, and nothing else. After a crash and resume the fresh browser has no portal session, and a read-only auditor would be blind. So `browser_fill_secret` is in the verifier's tool subset (a `Tool.verifier_allowed` flag), and in read-only mode it works only on the fields of a *login form*: a text, e-mail or password `<input>` inside a form whose visible submit controls are all classified read ("Sign in", "Log in", "Search"; at least one) and which has a password field or a sign-in button. The "Sign in" click is read, so it passes. Everything else is still refused in read-only mode: other typing, selects, and write or irreversible clicks (`browser_type` is not in the verifier's toolset at all). The verifier prompt tells it to sign in through `list_credentials` + `browser_fill_secret` when it meets a login page. The load test found this gap; with the fix the first audit after a resume passes, and the policy agent's second-round "audit_resync" workaround remains only as a fallback ([docs/SCALING.md](docs/SCALING.md)).

### 3.7 Guards that escalate to a human instead of thrashing

[worker/agent/guards.py](worker/agent/guards.py): a step budget, a wall-clock timeout, a consecutive-failure counter (nudge at half the limit, escalate at the limit), and a repeat detector (same action and same observation three times in six steps). Soft problems get one corrective nudge, then the run pauses and asks the human "Keep trying / Stop the task". Hard limits end the run as `needs_attention` with a partial report. Denials, skipped calls and rejected finishes are not counted as tool failures. The timeout counts active time only: while the run waits for a person, the clock is paused (a person taking an hour must not look like a stuck agent), and the guard counters are saved with every checkpoint so they survive a resume.

### 3.8 Transient vs. semantic errors, handled at different layers

Transient failures (timeouts, connection resets, detached elements, HTTP 5xx, a closed browser) of idempotent or read-level calls are retried with exponential backoff inside the tool layer ([worker/tools/retry.py](worker/tools/retry.py)); the LLM sees only the final outcome plus a note such as "succeeded after 1 automatic retry". Semantic failures (validation errors, not found, stale ref, wrong format) are returned to the model with an actionable message ("Date must be in YYYY-MM-DD format", the list of valid select options) so it re-plans. Retries follow an idempotency rule: only calls that are safe to repeat are retried automatically. For `http_request` that means GET, HEAD and OPTIONS; a 5xx or network error on POST, PUT, PATCH or DELETE is returned as a non-transient failure with the note "the write may or may not have happened: check whether the record exists (GET) before retrying", because a 5xx after a partial write would otherwise duplicate it. The same applies to the browser: clicks and key presses classified above read (and typing with submit) are not retried after a timeout or a closed page; the agent is told to re-read the page and check whether the action took effect. Read-level browser actions keep their auto-retry. Retrying a validation error is wasted calls; re-planning a network blip wastes tokens, so the layers are split.

### 3.9 A simulated company with deliberate traps

A narrow prototype that genuinely works beats mocked breadth. Rather than pretending to integrate with a dozen SaaS products, the repo ships three small but real web apps with sessions, validation and a REST API, plus failure injection. Traps: the portal list is paginated and **not sorted by date**; amount and due date appear only on the detail page and inside the PDF; the ERP wants `YYYY-MM-DD` while invoices show other formats; the ERP rejects duplicates; "Company X Ltd" vs "Company X Holdings" is ambiguous; credentials are only in a workspace file (single-user mode) or the vault (multi-user mode); and chaos flags add one-time 500s, session expiry, missing portal invoices and latency. Everything is seeded deterministically, relative to "today", and is resettable, so runs are comparable. In v2 the simulator is multi-tenant: every organization gets a private copy (see 3.17).

### 3.10 Deterministic ground-truth evals, not LLM-judged

Each scenario ends with a Python checker ([evals/checkers.py](evals/checkers.py)) that compares what is in the simulator afterwards (bills and sent mail from `GET /admin/state`, expected invoices derived from the deterministic seed) with what should be there: exactly one bill, right vendor, exact amount, currency and due date, no duplicates or stray records, nothing written after a denial, a reply actually in the Sent folder that states the due date. The agent's own claims are never used to decide success; if the agent says `succeeded` while the ground truth disagrees, the run is flagged as a **false success**. LLM-as-judge would grade the agent with the same kind of fallibility it is being tested for, whereas the checkers can see what the agent cannot. The load test reuses the same checker logic.

### 3.11 No agent framework

No LangChain, LlamaIndex or similar. The runner is one file of about 700 lines and is the interesting part of the task: context policy, approval gating, guard behaviour, checkpoint/resume and verification all need precise control over when and how the model is called, and a framework would hide exactly those decisions. It also keeps the behaviour inspectable and the dependency list short.

### 3.12 Gemini free tier behind a provider adapter

Zero cost was a goal, so the default is the Gemini free tier with native function calling via the `google-genai` SDK. [worker/llm/base.py](worker/llm/base.py) defines a neutral message and tool-spec format, so Gemini and Ollama (and scripted fakes for tests) are interchangeable. A `ResilientLLM` wrapper adds the pieces free tiers need: retry with backoff that honours the server's retry hint on 429/5xx, JSON-schema validation of tool arguments with one corrective re-prompt, and token accounting. A `FailoverLLM` walks a chain of models: a model whose daily free-tier quota is gone (the free tier allows only a handful of requests per model per day) is skipped until the next reset and the run continues on the next one, mid-run, because the prompt is rebuilt from state every turn. In multi-user mode the throttle, the "which models are exhausted" memory, the usage ledger and the per-org daily budgets all live in PostgreSQL (3.16). Ollama is the offline fallback, but a 4 GB laptop GPU (RTX 3050) only fits roughly 3B-parameter models, which were not reliable enough at multi-step tool calling to be the primary path.

### 3.13 Design rules that came from failures I expected

- Calls after a failure in the same turn are skipped (the plan changed), and `finish` must be called alone, so the model cannot claim success in the same breath as an unseen result.
- The system prompt requires checking whether a record exists before creating it, and trusting the authoritative source (PDF over list view).
- File access is sandboxed to `workspace/` plus the run's `downloads/`; `http_request` is restricted to an allowlist of hosts.

### 3.14 PostgreSQL alone: no Redis, no broker

The system needs a durable queue, cross-process signalling, shared rate limits and a system of record. One database provides all four: `SELECT ... FOR UPDATE SKIP LOCKED` for the queue, `LISTEN/NOTIFY` for wake-ups, a row with `FOR UPDATE` for the shared token bucket, ordinary tables for the rest. One dependency is easier to explain, to deploy and to reason about (a claim, a lease and an event can be part of one transaction). The cost is documented: every process holds a connection pool plus one LISTEN connection, which a transaction-pooling proxy cannot carry, and the single rate-limit row would need sharding at hundreds of calls per second ([docs/SCALING.md](docs/SCALING.md)).

### 3.15 Queue, leases and resume instead of "just run it in the request"

Runs take minutes, hold a browser, and wait for people. Running them in the API process (v1) cannot survive a restart or scale past one run. So the API only enqueues; a worker claims a run with a lease it must keep renewing; a run whose lease expires is taken back and **resumed from its checkpoint**. Resume works because the runner already rebuilds its prompt from state, and because the runner's hidden state (turn counters, nudges, verifier feedback, guard counters, the "approve similar" cache, the claim and verdict) is saved with every step. The resumed agent is told the browser was restarted and must re-check the world; writes are never replayed blindly. Per-org concurrency is enforced in SQL when a run is claimed, so one organization cannot starve the others, and the daily quota is counted in the enqueue transaction under a per-org advisory lock, so concurrent requests cannot both slip under the limit.

### 3.16 Shared LLM limits, health, usage and quotas

N parallel runs on one API key would each throttle themselves and still trip the provider's per-minute limit. So the throttle is one token bucket per API key (SHA-256 of the key, never the key) in a PostgreSQL row, timed by the database clock; a waiting run sleeps outside the transaction, so it holds neither a lock nor a connection. Model health (which Gemini models are out of quota) is shared the same way, with a short in-process cache. Every call is written to `usage_events` (org, user, run, model, tokens), and an org's daily token and call budgets are checked before each call: a run that hits its budget ends `needs_attention` with a plain message instead of failing mysteriously. An org may bring its own Gemini key (a vault credential the agent cannot see or use), which then gets its own bucket.

### 3.17 One simulated company per organization (the tenant model)

The simulator stands in for each customer's own external systems, so SQLite per tenant is realistic and stays separate from the platform's PostgreSQL. A tenant has its own database, PDFs, logins, API key, sessions and chaos flags. The browser reaches its tenant through a secret entry token held in a `sim_tenant` cookie, which the platform **pre-seeds into the run's browser context**; the ERP REST API identifies the tenant by its per-tenant API key. A client can never name a tenant id, and no URL or template is rewritten, so it also works through the public proxy. The result is that the same agent code runs against different data for different orgs, and a run cannot land in another org's data even if it skips the entry link.

### 3.18 A credential vault, so the model never holds a secret

In v1 the agent read logins from a plaintext workspace file and they flowed into prompts and logs. In v2 credentials are encrypted at rest (Fernet), the agent sees only names, and tools use the values internally: `browser_fill_secret` types a field into a page, `http_request(auth_credential=...)` attaches a header. Everything that leaves the run (steps, events, approval cards, the report, the prompt itself) passes a redactor as a second line of defence. Details and honest limits are in [section 4](#4-security-model).

### 3.19 Organization memory with a write gate, not a free-for-all

Letting agents write to shared long-term memory invites poisoning (a failed run learning a wrong fact, a page injecting an instruction). So memory is written at exactly one place, after a run has ended `succeeded` (which already means the independent audit passed), and every candidate is screened. Recall is plain keyword search by design. See [section 6](#6-memory).

### 3.20 A deterministic stand-in for the model to load-test the platform

The point of the load test is to exercise the platform (queue, pool, vault, approvals, events, isolation), and a free-tier Gemini key cannot serve hundreds of runs. So `LLM_PROVIDER=policy` swaps the model for `PolicyLLM`, a rule-based client that reads the same prompt a model would (page snapshots, working memory) and drives the **real** tools. It is not a script of canned calls and it hides nothing about the platform, but it is not a language model: see the caveats in [section 7](#7-scale-and-proof).

---

## 4. Security model

> [!IMPORTANT]
> Secrets never reach the model. It sees credential names only, tools use the values inside the worker process, and a redactor scrubs everything that is stored, emitted or sent to the model (details in 4.4).

> [!WARNING]
> Multi-user mode is meant for a small trusted team behind its own network or login, and it has not been security-audited ([section 16](#16-assumptions)).

### 4.1 Roles and capabilities

Users belong to organizations through memberships. A user can belong to several organizations (with a different role in each) and switches between them in the dashboard; the active organization is stored on the server-side session and every request is scoped to it. The role is read from the membership on every request, so a role change or deactivation applies immediately.

| Capability | viewer | approver | operator | admin | owner |
|---|---|---|---|---|---|
| Watch runs, reports, the approvals inbox, organization memory | yes | yes | yes | yes | yes |
| Answer the agent's questions | no | yes | yes | yes | yes |
| Approve or deny actions | no | yes | only if the org sets `operators_can_approve` | yes | yes |
| Start and cancel runs | no | no | yes | yes | yes |
| Members, organization settings, credentials, environment, usage, system card, audit, edit or delete memory | no | no | no | yes | yes |
| Create, promote or demote owners | no | no | no | no | yes |

Rules for who may change whom: nobody can change their own role or deactivate themselves; an admin can neither assign a role above admin nor touch an owner. Deactivating a member ends their sessions in that organization.

### 4.2 Separation of duties

With the organization setting `separation_of_duties` (on by default) the person who started a run cannot approve its **irreversible** actions; with `sod_for_writes` also not its writes. They can always deny. The rule is applied on the server when an answer arrives (HTTP 403 with the reason), and the approval card says who may approve and why its buttons are disabled. The timeline records "approved by <name>", and the audit log records the answer together with who started the run. The org's approval mode is a floor: an operator may ask for stricter confirmation when starting a run, never looser.

### 4.3 Organization isolation

Every tenant-owned table carries an `org_id`, and the run store, approval store and memory store are constructed for one organization and filter every statement on it. A run, report, file, event stream, approval or memory note of another organization is **indistinguishable from one that does not exist: it answers 404, never 403** (tests assert, for every role of another organization, that run detail, answers, report, files and cancel all return the same 404 as for an id that never existed, that nothing was changed, and that the event WebSocket refuses a foreign run). Files are served only after the org check on the run. The same holds below the API: a worker builds its store, vault scope and browser cookie from the claimed run's own organization. The load test scanned every response and WebSocket frame that any simulated user received for another org's identifiers and found none (section 7).

### 4.4 The credential vault

- **Encrypted at rest.** The secret fields of a credential are sealed with Fernet (`VAULT_KEY`; required when `DEPLOY_MODE` is on) into one token; the table keeps only non-secret metadata (name, kind, description, URL and host hint, field *names*) in clear. The admin credentials API and page never return values; secret fields are write-only (blank means "keep the stored one").
- **The model never sees a secret.** It gets credential names ("credentials available: erp, portal, mail"). `browser_fill_secret(ref, credential, field)` types the value straight into a text or password `<input>` inside the worker process; the observation says `•••• (credential erp.password)`. Textareas are refused on purpose: a free-text box is where a prompt-injected agent would copy a secret to.
- **Host-bound credentials.** A credential may carry a host; it is typed only into a page on that host (or a subdomain) and sent only to that host, even if the HTTP allowlist would permit others. Filling is classified read (it commits nothing); the submit click is classified and gated as usual.
- **The read-only verifier may authenticate, nothing more.** `browser_fill_secret` is the one write-capable tool it gets (flag `verifier_allowed`), and in read-only mode it only fills the fields of a login form (all submit buttons read-classified, with a password field or a sign-in button); the host binding and redaction still apply, and every other typing, selecting or write click is refused.
- **Hidden credentials.** The organization's tenant entry token and its own Gemini key are stored in the same vault but never listed to the agent or the credentials page.
- **Redaction choke points.** A per-run `Redactor` replaces secret values and their common encodings (URL, HTML, JSON-escaped, base64) with `[REDACTED:erp.password]`. It is applied to: tool results (inside `execute_with_retry`, so the runner and the verifier both get clean observations); steps, events, approval payloads, the plan, facts and the report (in the runner); the complete message list right before every LLM call (also the verifier's); and every write to the run store (`RedactingStore`). A log filter scrubs the same values, database URLs, API-key headers and bearer tokens from log records when they are created. Secrets typed into a task are redacted before the task is stored.
- `python -m worker.vault put|list|delete|rotate-key|generate-key` manages the **file** vault of single-user mode; in multi-user mode credentials are managed on the Credentials page.

### 4.5 Identity, sessions and the audit log

- **Passwords** are hashed with argon2id. The password check runs for unknown and deactivated accounts too (against a dummy hash), so neither the answer nor its timing reveals whether an e-mail exists.
- **Sessions** are rows in PostgreSQL; the cookie `ofa_session` holds a random 256-bit token (HttpOnly, SameSite=Lax, Secure when `DEPLOY_MODE`) and only its SHA-256 is stored, so a database leak cannot be replayed. Idle timeout 12 h (sliding) and an absolute limit of 7 days, both configurable; logout, deactivation and org changes revoke sessions.
- **Login throttling.** Failed sign-ins are counted per e-mail (5) and per client address (20) in fixed 15-minute windows; over the limit the answer is 429 with `Retry-After`, and every failure is answered after an exponentially growing pause (0.5 s doubling, at most 8 s).
- **CSRF.** SameSite=Lax cookies, plus a guard that refuses state-changing requests the browser itself marks `Sec-Fetch-Site: cross-site` (403). WebSocket handshakes check the `Origin` against the host.
- **Audit log.** An append-only table records sign-ins (also failed ones), sign-outs, org switches, run start and cancel, every approval and answer, member changes, setting changes, credential changes, environment provision and reset, and memory saves, edits and deletions. Admins read it under **Admin > Recent activity**.
- **Metrics.** `GET /metrics` needs `Authorization: Bearer <METRICS_TOKEN>` or a signed-in admin; it is open only for local development (no deploy mode, no token, no password). The simulator's `/admin/*` endpoints take an optional `SIM_ADMIN_TOKEN`.

### 4.6 What this does not protect against

- **Screenshots are not scrubbed.** Evidence screenshots are taken from the real page; a page that displays a secret in plain text would put it in the image. (Password inputs render as dots, and the filled value is not echoed into the text snapshot.)
- **Redaction is exact-match.** It cannot catch a secret the model transforms itself (reversed, split, spelled out) or one inside an image, and values shorter than 4 characters are not matched.
- **Prompt injection is limited, not solved.** Page text and PDF contents are untrusted input that reaches the model. The approval gate, the read-only verifier, host-bound credentials, no secrets in the model's context and the memory screening limit the damage; they are not a boundary.
- **The agent's workspace folder (`WORKSPACE_DIR`) is one directory shared by all organizations**, and `write_file` (an approval-gated write) can add to it. Treat it as non-secret shared drive content.
- No SSO or MFA, and no password change or reset: a password is set when the member is created.
- Fencing of a worker that lost its lease is in-process (section 5), not enforced by the database.

---

## 5. Reliability

- **Transient vs. semantic errors, idempotency-aware retries.** See [3.8](#38-transient-vs-semantic-errors-handled-at-different-layers): transient failures of repeat-safe calls are retried with backoff in the tool layer; a failed write is never re-sent blindly (the agent is told to check whether it took effect); semantic errors go back to the model with an actionable message.
- **Guards.** Step budget, active-time timeout, failure streaks, repetition detection, escalation to a human instead of thrashing ([3.7](#37-guards-that-escalate-to-a-human-instead-of-thrashing)).
- **An independent verifier** with fresh context and read-only tools, feeding failures back for up to two correction rounds ([3.6](#36-an-independent-verifier-with-fresh-context-and-read-only-tools)). It can sign in through the vault (`browser_fill_secret` into the fields of a login form, then the "Sign in" click, which is classified read) but is still refused everything else, so a resumed run's fresh browser does not blind the audit; the first audit after a resume now passes.
- **Crash recovery.** A worker holds a run through a *lease* it renews with a *heartbeat* (every third of `JOB_LEASE_S`). If the worker dies, its leases expire; every worker also runs a *reaper* that puts expired runs back in the queue (`FOR UPDATE SKIP LOCKED`, so reapers do not collide), and the next claim *resumes* from the checkpoint with a note that the browser was restarted. After `JOB_MAX_ATTEMPTS` claims the run ends `needs_attention` ("lost too many times"). A graceful stop (Ctrl+C, SIGTERM) hands runs back without using an attempt; `--drain` lets them finish instead. If a heartbeat finds the lease gone, the old worker is *fenced* (its writes are dropped) and cancels the run locally. Load-tested: a hard worker kill with four runs in flight, twice, all four resumed and succeeded each time ([section 7](#7-scale-and-proof)).
- **Durable approvals.** An approval is a row. Any API process can record the answer; the **first writer wins** (a second answer is a 409); an answer past its deadline is refused. A request expires after `approval_timeout_s` (an org setting, default 1 hour): the run then ends `needs_attention` with "approval expired after N min" rather than guessing. API restarts lose nothing; a resumed run **re-attaches** to its still-open request (or takes an answer that arrived while no worker was waiting) instead of asking twice, and cancels a leftover request the resumed agent no longer asks for.
- **Service restarts.** Sessions and approvals are in PostgreSQL, so an API restart only drops open WebSockets (clients reconnect and replay); a database restart pauses the listeners, which reconnect and re-read.
- **Org limits as back-pressure.** `max_concurrent_runs` keeps one organization from taking every slot; daily run, token and call budgets stop runaway spending with a clear message.

---

## 6. Memory

Two different things, kept apart on purpose.

**Per-run working memory.** `remember(key, value, source)` stores facts for this run, shown back every turn with provenance (3.4). It lives in the run's rows and is the only thing the agent needs to survive a context cut or a resume.

**Governed organization memory.** Long-term, org-wide how-to knowledge about the organization's *systems* ("the ERP new-bill form needs the date as YYYY-MM-DD"), never task data. One table, `org_memory`, with a key (unique per organization, case-insensitive), content, scope (the system, e.g. `erp`), provenance (source run, step and source text, creator), usage counters and optional expiry.

- **The write gate.** During a run the agent marks a fact `durable`; that is only a candidate, kept in the run's own facts. After the runner returns, the worker calls `commit_run_memory`, the single place that writes shared memory, and it writes **only if the run ended `succeeded`**, which the runner reports only after the independent audit passed. Failed, blocked, cancelled and unverified runs write nothing, however confident their candidates.
- **Screening.** Each candidate is rejected if it contains a secret (a redaction marker, or the redactor still finds one), has a bad key (3 to 80 characters), is longer than 500 characters or shorter than 3 words, looks like task data (at least 40% of the words contain a digit, or 30% of the characters are digits), or reads like an instruction aimed at the agent ("ignore previous instructions", "system prompt", and similar). At most 10 notes are saved per run (the rest are rejected as over the limit). Outcomes are events on the run (`memory_saved`, `memory_rejected`, with reasons and never the rejected text) and an audit row. Saving an existing key updates the note and bumps its provenance.
- **Recall is keyword search, by design.** PostgreSQL full text (`websearch_to_tsquery` ranked by `ts_rank_cd` over a generated `tsvector` of key, content and scope; GIN index). At run start the best matches for the task text are injected into the state block (default 5, `memory_recall_limit`; 0 turns memory off for the org), marked "verify before relying on it", and a `recall(query)` tool does the same on demand. Matching tries "all words" and falls back to "any word"; long texts go straight to "any". Each recalled note counts as a use. No embeddings and no RAG: the notes are short and their vocabulary is the organization's own, so the cost, the extra service and the opaque ranking of embeddings buy little here, and a keyword hit can be explained to an admin.
- **Admin review.** The **Memory** page lists and searches notes for every member; admins can edit a note's content, scope or expiry, delete it, and expire stale notes now. Notes unused for `memory_ttl_days` (default 180) stop being recalled but stay visible, flagged expired. Expiry is applied when a run commits memory and by the admin button (there is no scheduled sweeper). There is no pre-approval queue: a note enters because a verified run wrote it, and humans can correct or remove it afterwards.

---

## 7. Scale and proof

[docs/SCALING.md](docs/SCALING.md) reports a real load test of the whole stack on one machine, with the method, the hardware, what limits throughput, the recovery tests, the bugs the test found, and the caveats. [scripts/loadtest.py](scripts/loadtest.py) reproduces it: it creates a throwaway database, starts the simulator, the API and N worker processes, then drives many organizations and users over real HTTP and WebSockets (operators submit runs, approvers of the same org approve after a human-like delay, viewers watch event streams and keep probing other orgs' runs). Success is judged from each org's ERP, not from what the agent says.

> [!IMPORTANT]
> Every run in this load test was executed by the deterministic policy stand-in, not by Gemini, on one machine. The caveats follow the numbers.

Headline results, copied from the generated tables (all runs executed by the policy LLM, 14 to 16 model calls per run):

| Profile | Orgs x users | Runs | Workers x slots | Succeeded and ground-truth pass | Throughput (runs/min) | End to end p50 / p95 (s) | Queue wait p50 / p95 (s) | Isolation: responses scanned / leaks | Cross-org probes not refused |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 3 x 4 | 30 | 2 x 4 | 30 / 30 | 19.7 | 25.6 / 36.0 | 6.0 / 17.0 | 7,355 / 0 | 0 of 311 |
| stress (20% chaos, worker killed) | 5 x 4 | 60 | 3 x 4 | 60 / 60 | 25.5 | 34.1 / 62.9 | 12.5 / 41.7 | 14,970 / 0 | 0 of 825 |
| soak (25% chaos, worker killed, API restarted) | 6 x 5 | 280 | 3 x 4 | 280 / 280 | 38.8 | 97.1 / 124.4 | 81.1 / 108.5 | 47,508 / 0 | 0 of 4,050 |
| llm-bound (model limit 2 calls/s) | 3 x 4 | 24 | 2 x 4 | 24 / 24 | 7.2 | 103.6 / 153.0 | 45.4 / 97.9 | 6,878 / 0 | 0 of 674 |

In every profile there were no false successes, duplicate or stray bills, deadlocks, out-of-order events or breaches of the per-org concurrency cap. In the soak profile **280 of 280 runs succeeded and passed ground truth with a worker kill (4 runs in flight, all resumed) and an API restart; 0 isolation leaks across 47,508 responses.** The approval wait is almost exactly the simulated human's think time; the platform's own part (notification to the approver's inbox, the answer, the NOTIFY back to the run) is 0.02 to 0.07 s. A worker with four open runs uses about 0.9 to 1.0 GB, roughly 115 to 165 MB per browser context.

What limits throughput: execution slots while the model is cheap (the machine used 3.5% CPU in the soak), then the model's own rate (the llm-bound profile finished exactly at the limiter's ceiling), then browser memory per context; the database was not the limit at about 490 transactions per second.

**Caveats, stated plainly:**

- The model is a **policy**, not Gemini. It never misreads a page or hallucinates, never asks a question, and needs only 16 calls per run, so absolute latencies are optimistic. What the test proves is the structure: queueing, leases, approvals, recovery, isolation and per-slot memory.
- It is **one machine**: the database, API, workers, browsers, simulated company and the load generator share the same CPU and RAM, with zero network latency. A cloud database adds round trips to every claim, heartbeat, event and approval.
- Simulated approvers answer in 0.5 to 3 seconds; real people take minutes, and a waiting run keeps its slot (and browser context) occupied.
- The runs are one task family, and a 7-minute soak cannot distinguish a slow leak from a plateau (memory drifted about 14 MB/min per worker).

---

## 8. Observability

- **Structured logs.** `LOG_FORMAT=json` (the default when `DEPLOY_MODE` is on) writes one JSON object per line with `run_id`, `org_id`, `user_id`, `worker_id` and `request_id` carried through `contextvars`; `text` is the readable default. Secrets, database passwords, API-key headers and bearer tokens are masked when the record is created.
- **`/metrics`** (Prometheus): runs by status, run duration (human waits excluded), active runs, queue depth per org, queue wait, tool calls and duration, LLM calls, tokens, latency and limiter wait, approval outcomes and wait time, lost leases, requeues and resumes, open browser contexts and Chromium relaunches, WebSocket connections, HTTP requests and duration. Every process has its own registry: scrape the API and each worker (`python -m worker.jobs --metrics-port 9101` serves loopback only). Access is described in [4.5](#45-identity-sessions-and-the-audit-log).
- **`/healthz`** is liveness (no dependencies). **`/readyz`** is readiness: database reachable, migrations at the head revision, and the simulated company answering; it returns 503 with a per-check reason. A worker logs the same checks at start.
- **The System card** on the Admin page (admin role) shows queue depth, runs in flight and waiting per org, the workers currently holding a run, runs finished in the last 24 hours by status, and p50 and p95 run time; admins listed in `PLATFORM_ADMIN_EMAILS` see every org. The **Usage** card next to it shows LLM calls and tokens per day, per user and per model against the org's budget.
- Every run keeps its full timeline: `run_events` (what the dashboard replays and streams), `run_steps`, the final report and the checkpoint.

The full metric table and the scrape configuration are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#observability).

---

## 9. Demo

<!-- Demo video: <add link> -->

The recording shows, in order (shot-by-shot script, with an optional multi-user segment, in **[docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md)**):

1. **Headline task, headed browser.** The agent reads the shared workspace, finds credentials, logs in to the vendor portal, works out which invoice is the latest despite the unsorted list, reads the amount and due date from the invoice, then logs in to the ERP.
2. **Approval gate.** Before the "Save bill" click, an approval card shows the exact values; approving lets it proceed. The ERP bill is shown in the real ERP afterwards.
3. **Chaos run.** Same task with a one-time server 500 and session expiry: re-login, and a clean result (the API 500 on a write is not retried blindly: the agent checks whether the bill exists, then retries).
4. **Ambiguity.** With "Company X Holdings" present, the agent asks which vendor instead of guessing.
5. **Verification and report.** The final card shows the independent audit's checks, what it actually saw, evidence screenshots, and run stats.
6. **A second, unrelated task** (reply to a vendor's payment-date question, then the month-end backfill) on the same code, showing generalisation.
7. **Eval table** from `python -m evals.run`.
8. **Multi-user segment (optional).** An operator and an approver in two windows, the approvals inbox, separation of duties, killing a worker and watching the run resume, the Memory page, and the Usage and System cards.

---

## 10. Capability map

| Capability | How it is implemented | Where |
|---|---|---|
| Understands the end goal | First turn restates the goal, defines checkable success criteria, lists unknowns, calls `update_plan` | [prompts.py](worker/agent/prompts.py), [control.py](worker/tools/control.py) |
| Breaks it into actions | Plan checklist kept current by `update_plan`; status per item; shown in the dashboard | [control.py](worker/tools/control.py), [state.py](worker/agent/state.py) |
| Acts on a real computer | Playwright Chromium (10 browser tools), files, HTTP; a pooled browser with an isolated context per run | [browser.py](worker/tools/browser.py), [browser_pool.py](worker/tools/browser_pool.py), [files.py](worker/tools/files.py), [http.py](worker/tools/http.py) |
| Observes | Compact DOM snapshot with element refs, alerts, validation errors; PDF text extraction | [snapshot.js](worker/tools/snapshot.js), [snapshot.py](worker/tools/snapshot.py) |
| Remembers | Working memory with provenance, re-injected every turn; state-based context; governed organization memory with a verified-only write gate | [memory.py](worker/agent/memory.py), [context.py](worker/agent/context.py), [memory_store.py](worker/memory_store.py) |
| Detects failure | `ToolResult.ok/error`, page alerts, HTTP status, guards (streak, repetition, budget, timeout) | [retry.py](worker/tools/retry.py), [guards.py](worker/agent/guards.py) |
| Retries / finds alternatives | Auto-retry for transient errors of repeat-safe calls; semantic errors go to the LLM; prompt directs it to alternatives such as the ERP REST API | [retry.py](worker/tools/retry.py), [prompts.py](worker/agent/prompts.py) |
| Verifies its work | Independent fresh-context auditor, read-only tools (with vault sign-in), per-criterion checks, feedback loop | [verifier.py](worker/agent/verifier.py), [runner.py](worker/agent/runner.py) |
| Asks for clarification | `ask_user` tool routed to the dashboard and the approvals inbox; prompt says look first, ask only when ambiguous | [interaction.py](worker/tools/interaction.py), [broker.py](worker/agent/broker.py), [pg_broker.py](worker/agent/pg_broker.py) |
| Asks for approval | Runtime risk classification + approval policy; durable approvals with expiry, roles and separation of duties | [policy.py](worker/agent/policy.py), [approvals_pg.py](worker/approvals_pg.py), [org_settings.py](worker/org_settings.py) |
| Summary with evidence | `finish` carries summary, outputs, claim-to-source evidence; report adds audit checks, screenshots, stats, `report.md` | [report.py](worker/agent/report.py), [app.js](worker/ui/app.js) |
| Safe by default | Approval modes, file sandbox, HTTP host allowlist, step budget, timeout, no-retry on denial, encrypted vault, redaction | [config.py](worker/config.py), [files.py](worker/tools/files.py), [vault.py](worker/vault.py), [redaction.py](worker/redaction.py) |
| Observable | Live timeline, plan, memory, usage, event log, structured logs, `/metrics`, health, System card | [server.py](worker/server.py), [metrics.py](worker/metrics.py), [ui/](worker/ui) |
| Generalises | Task-agnostic runtime; 8 eval scenarios of different shapes | [evals/scenarios/](evals/scenarios) |
| **v2:** many users and organizations | E-mail and password sign-in, server-side sessions, five roles, org-scoped stores, 404 on foreign ids | [auth.py](worker/auth.py), [admin_api.py](worker/admin_api.py), [store_pg.py](worker/store_pg.py) |
| **v2:** parallel runs, survives crashes | Job queue on `SKIP LOCKED`, leases, heartbeat, reaper, resume from checkpoint | [jobqueue.py](worker/jobqueue.py), [jobs.py](worker/jobs.py) |
| **v2:** many runs on one key | Shared token-bucket limiter, shared model health, usage ledger, per-org budgets | [limiter.py](worker/llm/limiter.py), [health_pg.py](worker/llm/health_pg.py), [usage.py](worker/usage.py) |
| **v2:** isolated company per org | Multi-tenant simulator, cookie pre-seeded into the run's browser, per-tenant API key | [tenants.py](company_sim/tenants.py), [provisioning.py](worker/provisioning.py) |
| **v2:** proof it scales | Load test with ground-truth checks and isolation scanning | [loadtest.py](scripts/loadtest.py), [docs/SCALING.md](docs/SCALING.md) |

---

## 11. Evaluation

Scenarios live in [evals/scenarios/](evals/scenarios). Each one defines the task text, the chaos flags to set, how approvals and questions are answered automatically (`approve_all`, or `deny_writes`), and a deterministic checker that inspects the simulator database afterwards.

| Scenario | What it tests |
|---|---|
| `invoice_happy` | Baseline: find latest Company X invoice, enter in ERP |
| `invoice_server_error` | One-time ERP 500 and session expiry: retry and re-login |
| `invoice_duplicate` | Bill already exists: must not double-enter |
| `invoice_ambiguous_vendor` | Ltd vs Holdings: must ask, then enter under the right vendor |
| `invoice_portal_missing` | Invoice absent from the portal: must re-plan and use the email PDF |
| `reply_payment_date` | Non-invoice task: reply to a vendor email with the correct due date |
| `month_backfill` | Multi-item: record every open invoice due this month that is not in the ERP |
| `deny_write` | Human denies the write: nothing may change and the agent must report honestly |

```bash
python -m evals.run                                  # every scenario in evals/scenarios/*.yaml
python -m evals.run --only invoice_happy,deny_write  # a subset
python -m evals.run --list                           # show scenarios and their chaos flags
python -m evals.run --repeat 3 --sleep-between 20    # repeat runs; pause between runs for free-tier quotas
python -m evals.run --parallel 3                     # 3 scenarios at once, each in its own simulated-company tenant
```

`--parallel N` implies `--tenant-per-scenario`: every scenario gets a freshly provisioned, isolated copy of the simulated company (own data, logins and chaos flags, reached through an entry link `/t/<token>/`, with its own workspace folder holding that tenant's `credentials.json`), so scenarios cannot disturb each other and share one browser pool. Without these flags the scenarios run one after the other on the default tenant, as before. Tenants live in `company_sim/data/tenants/`; the simulator's `/admin/*` endpoints can be locked with `SIM_ADMIN_TOKEN` (send it as `X-Admin-Token`; set the same variable for the evals and the worker).

Other flags: `--headed`, `--provider gemini|ollama`, `--sim-url`, `--scenarios-dir`. The runner starts the simulator itself if nothing is listening on `SIM_BASE_URL`, resets it with each scenario's chaos flags, runs the real agent in-process with scripted approvals and answers, then judges the result from the simulator's data, never from the agent's claim: if the agent says `succeeded` while the ground truth disagrees, it is flagged as a **false success**. It needs a valid `GEMINI_API_KEY`, makes real LLM calls, and takes several minutes on the free tier. Results print as a table and are written to `evals/results/latest.md` (plus a timestamped JSON). Exit code: 0 all passed, 1 some failed, 2 setup problem.

**Real-Gemini results, free tier, one run per scenario.** Latest full table: [evals/results/latest.md](evals/results/latest.md). The same five invoice scenarios were measured three times; each round's traces drove the fixes for the next one.

| Round | Passed | False successes | What the traces showed | What changed afterwards |
|---|---|---|---|---|
| 2026-10-16 | 3 of 5 | 1 | `invoice_happy` saved a correct bill but ran out of its 40-step budget; `invoice_ambiguous_vendor` picked "Company X Ltd" without noticing "Company X Holdings" | Step budget 40 to 60; prompt rules on several matching entries, guessed logins and file re-reads |
| 2026-10-17 | 0 of 5 | 2 | Three runs died on Gemini's `MALFORMED_FUNCTION_CALL` response, which the client treated as fatal and retried unchanged. The verifier passed two wrong results: it saw the competing Holdings invoice and passed anyway, and it never compared the mailbox with the portal | Malformed calls fail over to the next model, retry at a higher temperature, then reach the agent as a correction; two mandatory verifier checks, **Ambiguity** and **Completeness**, enforced by the verdict schema |
| **2026-10-18** | **4 of 5** | 1 | `invoice_portal_missing`, `invoice_happy`, `invoice_duplicate` and `invoice_server_error` passed, most of them on the weakest model in the chain (`gemini-3.5-flash-lite`). `invoice_ambiguous_vendor` still entered Ltd's invoice without asking: the verifier listed both vendors but judged "no ambiguity" without looking up Holdings' invoices | The Ambiguity verdict is now decided by code: the verifier lists every matching entity and the candidate it found for each, and two or more candidates with no question asked is a failure (not yet re-measured) |

Latest round in detail (2026-10-18):

| Scenario | Agent said | Ground truth | Steps | LLM calls | Approvals | Model the run ended on |
|---|---|---|---|---|---|---|
| `invoice_ambiguous_vendor` | succeeded | **FALSE SUCCESS** | 49 | 49 | 1/1 | gemini-3.7-flash |
| `invoice_portal_missing` | succeeded | **PASS** | 40 | 51 | 1/1 | gemini-3.5-flash-lite |
| `invoice_happy` | succeeded | **PASS** | 35 | 29 | 1/1 | gemini-3.5-flash-lite |
| `invoice_duplicate` | succeeded | **PASS** (found the existing bill, wrote nothing) | 22 | 31 | 0/0 | gemini-3.5-flash-lite |
| `invoice_server_error` | succeeded | **PASS** (recovered from the 500 and the expired session) | 59 | 41 | 2/2 | gemini-3.5-flash-lite |

No round produced a wrong or duplicate write in the ERP; every failure was a missing question or an unfinished run, and the eval harness, which judges from the simulator's database instead of the agent's claim, flagged every false success. Most runs end on "lite" models because the stronger free-tier models are often overloaded (HTTP 503) or out of daily quota, so the numbers partly measure free-tier availability.

`reply_payment_date` and `deny_write` passed in the 2026-10-16 round and were not re-run since; `month_backfill` has not been run against the real model yet because of the free-tier quota, so do not read its absence as a pass. All scenarios are exercised by the unit tests with a scripted LLM, and the multi-user platform end to end by the load test with the policy LLM ([section 7](#7-scale-and-proof)). LLM behaviour varies between runs (`--repeat N` helps); treat any single table as a sample, not a guarantee.

---

## 12. Testing

```bash
python -m pytest -q            # 676 tests
```

The count comes from `python -m pytest -q --co` (collect only). No API key and no network are needed: the model is replaced by a scripted fake (or the policy stand-in). The suite takes a few minutes because many tests start Chromium or PostgreSQL.

What it covers: the simulator and its tenants; the tool framework, retry rules and risk classification; the browser tool and snapshot against local HTML fixtures and the browser pool (needs Chromium); context building; guards; the runner end to end with a scripted LLM (including verifier failure, recovery, checkpoint and resume); the Gemini, Ollama, failover, shared limiter, model-health and usage layers; the stores (the same contract tests run against the file store and the PostgreSQL store); authentication, roles, separation of duties and quotas; organization isolation (a foreign id is a 404 everywhere, including WebSockets); the queue (concurrent claims, the daily quota, lost workers, resume, graceful shutdown and drain); durable approvals (answer from another API process, first writer wins, expiry, re-attach after resume, the inbox stream); the vault and redaction (secrets never appear in steps, events or prompts); organization memory (the write gate, screening, recall, governance); observability; and the policy LLM.

**Database tests use a separate test database.** `DATABASE_URL` supplies the server and credentials, and the tests replace the database name with `oneforall_test`, which is dropped and recreated at the start of the session, migrated once, emptied between tests and dropped at the end; the development database is never touched. To run two pytest sessions at once, give each its own: `ONEFORALL_TEST_DB=oneforall_test_a`. Without a reachable PostgreSQL the tests that need it are **skipped**, not failed. The fixtures default to `AUTH_MODE=file` so a `DATABASE_URL` in your `.env` does not change the v1 tests; the multi-user tests ask for database mode explicitly.


### 12.1 Continuous integration

GitHub Actions workflows live in [.github/workflows/](.github/workflows).

<details>
<summary><b>Workflows and automation, one row each</b></summary>

| Workflow | When | What it does |
|---|---|---|
| `ci.yml` | every push, pull request, manual | **lint**: `ruff check .` and `actionlint` on the workflows. **test-fast**: the whole suite in file mode without a database (the 149 PostgreSQL tests skip by design), Chromium installed and cached, JUnit XML uploaded, pass/fail/skip counts in the job summary. **test-full** (after lint): the whole suite against a `postgres:18` service container; the job **fails if any database test was skipped**; it also runs `python -m worker.db migrate` and `check` on a scratch database. **docker**: builds the image (not pushed); starts it in the single-user shape and curls `/healthz`; then, against a `postgres:18` service, migrates and seeds with the image, starts it with `ROLE=all`, waits for `/readyz`, checks that the logs show the environment resync and the job worker, signs in as `owner@acme.test` through the real login endpoint and calls an authenticated endpoint. |
| `security.yml` | push to `main`, pull requests, weekly | **gitleaks** over the whole git history (the CLI, so no licence key is needed; allowlist in [.gitleaks.toml](.gitleaks.toml) covers only the fake simulator and test-fixture credentials). **pip-audit** on `constraints.txt` and on a fresh resolve of `requirements.txt`; it reports in the job summary and as warnings but does not fail the build, because pip-audit cannot filter by severity. |
| `nightly.yml` | nightly, manual | A small load test (2 orgs, 4 users each, 6 runs, 1 worker x 2 slots, policy LLM, no Gemini) against a PostgreSQL service. Fails if the ground-truth pass rate is below 100% or anything leaked between organizations. Results are uploaded and shown in the summary. |
| `evals.yml` | manual only | Runs `python -m evals.run` against real Gemini for the scenarios and repeat count you choose (default: the five core scenarios, once). Skips with a clear message if the secret is missing. Uploads `latest.md` and the JSON. |
| `deploy.yml` | after `ci.yml` succeeds on `main`, manual | Builds and pushes the image and deploys it to staging, then production (see 12.2). Skipped until the repository variable `GCP_PROJECT_ID` is set. |
| `rollback.yml` | manual | Sends an environment's traffic back to an earlier Cloud Run revision. |
| `dependabot.yml` | weekly | Grouped minor/patch update PRs for pip and for the actions. |

</details>

The only secret any workflow reads is `GEMINI_API_KEY`, and only `evals.yml` does (Settings > Secrets and variables > Actions > New repository secret). The database password in the workflows is a throwaway value for a container that exists only for the job.

Run the same checks locally:

```bash
ruff check .                                   # lint (rules in pyproject.toml; the code is not auto-formatted)
python -m pytest -q                            # whole suite; without DATABASE_URL the PostgreSQL tests skip
actionlint                                     # the workflow files (installed by requirements-dev.txt)
pip-audit -r constraints.txt --no-deps --disable-pip
```

A `DATABASE_URL` in your `.env` makes the suite run the database tests too (against `oneforall_test`, never your data). To reproduce the file-mode job exactly, override it for one run: `DATABASE_URL= python -m pytest -q` (PowerShell: `$env:DATABASE_URL=""; python -m pytest -q`; an environment variable beats `.env`). To get the same result as the full job set `DATABASE_URL` to a PostgreSQL server you can create databases on and `ONEFORALL_TEST_DB` to a name starting with `oneforall_test`.

Recommended branch protection for `main` (Settings > Branches > Add rule): require a pull request before merging, require status checks to pass with `Lint (ruff, actionlint)`, `Tests (file mode, no database)`, `Tests (full, PostgreSQL 18)` and `Docker image builds and starts` selected (they only appear in the list after a first run), require branches to be up to date, and block force pushes. Add `gitleaks` from the Security workflow if you want secret scanning to block merges as well.

### 12.2 Continuous deployment

`deploy.yml` (with the reusable `_deploy-env.yml`) deploys what CI has just verified. After `ci.yml` succeeds on a push to `main` it builds the image once, pushes it to Artifact Registry tagged with the commit and deploys the same image by digest to **staging**, then to **production** once the reviewers of the `production` GitHub Environment approve. Each environment does the same things: run the database migration as a Cloud Run job, optionally seed the demo users (`seed_demo` on a manual run), deploy the new revision with no traffic, smoke test it on its own URL (`/readyz`, `/healthz`, `/`), and only then move traffic to it. A failed smoke test leaves traffic on the previous revision. `rollback.yml` moves traffic back to an earlier revision.

Google Cloud is reached through Workload Identity Federation, so no service-account key is stored in GitHub. The one-time cloud setup is `bash deploy/gcp/setup.sh`; the GitHub variables, environments and approvals are listed in [docs/DEPLOY.md](docs/DEPLOY.md). Until `GCP_PROJECT_ID` is set the deploy jobs skip themselves, so pushes stay green. 
> [!NOTE]
> The workflows were checked with `actionlint` and shellcheck, but the cloud side has not been exercised against a real project yet.

---

## 13. Configuration reference

Settings come from environment variables or `.env` (names match [worker/config.py](worker/config.py); [.env.example](.env.example) documents each). Selected, grouped:

<details>
<summary><b>Selected settings, grouped (click to expand)</b></summary>

| Group | Name | Default | Purpose |
|---|---|---|---|
| LLM | `LLM_PROVIDER` | `gemini` | `gemini`, `ollama`, `fake` (tests) or `policy` (load test) |
| | `GEMINI_API_KEY` | empty | free key from Google AI Studio |
| | `GEMINI_MODELS` / `GEMINI_MODEL` | chain of seven models | failover chain, tried in order; `GEMINI_MODEL` is tried first |
| | `LLM_MIN_INTERVAL_S` | 4 | single-user throttle; in multi-user mode it sets the shared bucket's refill rate (1 / interval) unless `LLM_REFILL_PER_S` is set |
| | `LLM_BUCKET_CAPACITY`, `LLM_REFILL_PER_S` | 2, 0 | shared token bucket: burst size, calls per second (0 = derive from the interval) |
| | `LLM_HEALTH_CACHE_S`, `LLM_QUOTA_CACHE_S` | 3, 5 | how often a process re-reads shared model health and the org's daily usage |
| | `LLM_NETWORK_RETRY_S` | 300 | how long to keep retrying through a network outage |
| | `OLLAMA_MODEL`, `OLLAMA_URL` | `qwen2.5:3b`, `http://localhost:11434` | local fallback |
| | `POLICY_THINK_MS` | 800 | simulated model latency of the policy LLM |
| Environment | `SIM_BASE_URL` | `http://localhost:8001` | where the simulated company runs |
| | `SIM_ADMIN_TOKEN` | empty | token for the simulator's `/admin/*` (set on both sides) |
| | `WORKSPACE_DIR`, `RUNS_DIR` | `./workspace`, `./runs` | shared drive for the agent; run artifacts (screenshots, downloads) |
| | `HEADLESS` | `true` | invisible browser |
| | `BROWSER_MAX_CONTEXTS`, `BROWSER_ACQUIRE_TIMEOUT_S` | 8, 60 | open run contexts per process; wait for a free one |
| | `HTTP_ALLOWED_HOSTS` | `localhost,127.0.0.1` | hosts `http_request` may call |
| Agent | `APPROVAL_MODE` | `confirm_writes` | `auto`, `confirm_writes`, `confirm_all` (in multi-user mode the org's setting, with this as its default) |
| | `MAX_STEPS`, `MAX_CONSECUTIVE_FAILURES`, `RUN_TIMEOUT_S` | 60, 4, 900 | guards (org settings override `MAX_STEPS` and `RUN_TIMEOUT_S`) |
| Database | `DATABASE_URL` | empty | PostgreSQL URL; empty means single-user mode. Managed-host URLs work as given (Neon's `sslmode=require&channel_binding=require` is translated); use the direct, not the `-pooler`, endpoint |
| | `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT_S` | 10, 10, 30 | connection pool per process |
| Auth | `AUTH_MODE` | `auto` | `auto`, `file` (ignore the database for sign-in) or `db` |
| | `SESSION_IDLE_TTL_HOURS`, `SESSION_ABSOLUTE_TTL_DAYS` | 12, 7 | sliding and absolute session limits |
| | `LOGIN_WINDOW_S`, `LOGIN_MAX_FAILURES_PER_EMAIL`, `LOGIN_MAX_FAILURES_PER_IP`, `LOGIN_BACKOFF_BASE_S` | 900, 5, 20, 0.5 | sign-in throttling |
| Vault | `VAULT_KEY`, `VAULT_DIR` | empty, `./.vault` | Fernet key (required with `DEPLOY_MODE`); where a development key and the file vault live |
| Queue | `WORKER_CONCURRENCY` | 2 | runs one worker process executes at once |
| | `JOB_LEASE_S`, `JOB_MAX_ATTEMPTS` | 30, 3 | lease length; claims before a run is given up on |
| | `JOB_POLL_S`, `JOB_REAPER_INTERVAL_S` | 2, 10 | claim poll fallback; reaper period |
| | `APPROVAL_POLL_S`, `LIVE_FALLBACK_POLL_S` | 3, 5 | fallbacks when a NOTIFY is missed |
| Observability | `LOG_LEVEL`, `LOG_FORMAT` | `INFO`, empty | empty format means JSON in deploy mode, text otherwise |
| | `METRICS_TOKEN`, `PLATFORM_ADMIN_EMAILS` | empty | bearer token for scrapers; admins who see every org on the System card |
| | `QUEUE_SAMPLE_S`, `READINESS_CHECK_SIM` | 15, `true` | queue-depth gauge period; `/readyz` also checks the simulator |
| Container (`deploy/start.py`) | `ROLE` | `all` | what the container runs: `all` (simulator, API and, with a database, the resync and job workers), `api`, `worker` or `sim` |
| | `WORKER_PROCESSES` | 1 | job worker processes started by `ROLE=all` (database mode only) |
| | `MIGRATE_ON_START` | off | `1` runs `python -m worker.db migrate` before the API starts (the Cloud Run pipeline migrates in a job instead) |
| | `BOOTSTRAP_DEMO_PASSWORD` | empty | read by `scripts/bootstrap.py` when `--demo-password` is not given; otherwise a password is generated and printed once |
| | `SIM_TENANT_SECRET` | empty | simulator only: when set, each tenant's entry token, API key and passwords are derived from it and the tenant id (same secrets after every restart); empty keeps them random |
| | `PORT`, `HOST`, `SIM_PORT`, `SIM_HOST` | 8080, `0.0.0.0`, 8001, `127.0.0.1` | listener addresses; with `ROLE=all` and no `SIM_ADMIN_TOKEN` the supervisor generates one |
| Hosting (opt-in) | `DEPLOY_MODE`, `PUBLIC_SIM_PROXY` | off | headless only, secure cookies, JSON logs; expose the simulator through the worker's port |
| | `ACCESS_PASSWORD`, `SESSION_SECRET`, `MAX_RUNS_PER_DAY` | empty, empty, 0 | single-user mode only (ignored when the database is on): shared password gate, cookie signing, daily cap |

Per-organization settings (approval mode and timeout, separation of duties, run and concurrency limits, LLM budgets, model chain, memory) are edited under **Admin > Organization settings** and listed in [docs/MULTI_USER.md](docs/MULTI_USER.md). Simulator-only variables (`SIM_CHAOS`, `SIM_TODAY`, `SIM_PORT`, `SIM_HOST`, `SIM_DATA_DIR`, `SIM_TENANT_SECRET`) are read from the shell, not from `.env`.

</details>

---

## 14. Known limitations

**Scope and environment**

- **Scope is web, files and HTTP.** There is no desktop or pixel-level computer use, no OS automation, no vision. The browser tool does not handle iframes or file uploads, and it cannot see canvas-only content.
- **Simulated environment.** The company is small and clean compared with real systems; real-world behaviour (CAPTCHAs, SSO/MFA on the target apps, flaky SPAs) is untested. The simulator is a single process with in-memory sessions and chaos state, and a chaos flag belongs to a company, not to one run.
- **The LLM can still misjudge.** The verifier is the same model family as the worker, so correlated errors are possible. It catches missing or wrong records, but not misreading ambiguous requirements it also misreads.
- **Free-tier rate limits slow runs.** One call every 4 seconds by default (a shared bucket in multi-user mode), backing off on 429, so a 30-step task takes minutes. Quotas and model names can change on Google's side. The real model was never load-tested; the load test used the policy LLM ([section 7](#7-scale-and-proof)).
- **Browser risk classification is heuristic.** It reads element roles and labels. A destructive action behind an unlabeled or oddly named control could be classified as a read. API calls and file writes are classified exactly.
- **No idempotency keys.** Automatic retry is restricted to idempotent calls (see 3.8), so a failed write is never re-sent blindly. The check "does the record exist" before retrying is prompt-driven, not enforced by the runtime.

**Platform**

- **Memory recall is keyword-only** (by design), and the screening that guards the write gate is heuristic (a digit-share rule and a phrase pattern can misjudge a note). Expiry is not swept on a schedule.
- **Artifacts are local files.** Screenshots and downloads are written under `RUNS_DIR` on the machine that ran the step; the report text is stored in the database. A run resumed on another machine loses its earlier files, and an API process can serve only what is on its own disk. Multi-machine use needs a shared volume or an object-store implementation (not written).
- **Fencing is in-process.** A worker that loses its lease drops its own writes, but SQL statements carry no per-claim fencing token, so a stalled-but-alive worker could still write in the short window before its next heartbeat notices. Event numbers and step keys are unique per run, which bounds the damage; a database-enforced token would remove it.
- **A waiting run holds its slot.** An approval can take a long time, and the run's browser context and worker slot stay occupied meanwhile.
- **Quota counts every queued run.** The daily run limit counts all runs queued that UTC day, whatever their outcome, including cancelled ones.
- **No SSO or OIDC, no MFA, no password change or reset, and no way to create an organization from the UI or API** (organizations come from the seed or SQL; admins add members to existing ones). Login throttling is per e-mail and per address (behind a proxy it needs `DEPLOY_MODE` so the forwarded address is used).
- **The workspace folder is shared by all organizations** (section 4.6), and `python -m worker.vault` manages only the file vault.
- **Scaling was measured on one machine** with a policy LLM, in one region, for minutes, not hours. A longer soak (the memory drift could be a leak or a plateau) and a real model run are still to do.
- **The Cloud Run deployment is single-instance and its pipeline is untested against a real project.** `max-instances 1` because the simulated company lives inside the container; its SQLite data and the run artifacts are ephemeral, so every start creates fresh tenants (the start-up resync rewrites the vault) and a stopped instance resumes its runs from the last checkpoint on the next start. Bills and other data created inside the simulated company are lost on a restart (it stands in for external systems), but its logins are stable because `SIM_TENANT_SECRET` makes them deterministic. See [docs/DEPLOY.md](docs/DEPLOY.md). The workflows pass `actionlint` and the image was not built locally (no Docker here), so the first real deploy is the real test.
- **Dates are anchored to the real current date**, so seeded data shifts day to day (`SIM_TODAY` can pin it). The agent's own "today" comes from the host clock.
- **Real-model eval table pending** (section 11).

---

## 15. What I would build next

In priority order:

1. **Rerun the real-Gemini evals** across all eight scenarios, in parallel across tenants within the free quota, and publish the table.
2. **Prove the deployment.** The pipeline to Cloud Run exists (section 12.2); run it against a real project, then an hour-long soak there, and recycle a worker's Chromium after N contexts if the memory drift turns out to be real. After that, split the API and the workers into separate services with shared storage for the simulator and the artifacts, so the service can scale past one instance.
3. **Object storage for artifacts** behind the `RunStore` seam (`artifact_dir` / `artifact_file`), served by signed URL, so resumed runs and any API copy see every file.
4. **Database-enforced fencing.** A claim token checked in the SQL of every run write, replacing the in-process fence.
5. **Identity.** OIDC single sign-on, MFA, password change and reset, session listing.
6. **Idempotency and exactly-once writes.** Idempotency keys on writes and a pre-write "does it exist" check enforced by the runtime rather than the prompt.
7. **Stronger verification.** A second verifier model from a different family to cut correlated errors; verifier-written checks that run as deterministic assertions where the system exposes an API.
8. **Real integrations.** Connectors for real mail and ERP APIs where they exist (cheaper and more reliable than UI driving), with the browser as the fallback.
9. **Vision fallback.** Screenshot-based observation only when the DOM snapshot is empty or ambiguous (canvas, iframes).
10. **Better memory, if needed.** Measure recall misses first; only then consider hybrid keyword and embedding ranking, and a scheduled expiry sweep with a review queue.
11. **Run-scoped chaos in the simulator** and partitioning or archiving of `run_events` and `run_steps` (about 90 to 110 events per run).

---

## 16. Assumptions

- The reader has Python 3.11+, can install Chromium through Playwright, and can create a free Google AI Studio key. For the multi-user mode they can install PostgreSQL and create a database.
- "Using a computer" means a real browser plus files and HTTP against web applications. A simulated company is acceptable because real accounts cannot be shared, and it lets failure modes be injected and checked deterministically.
- "Latest invoice" means the most recent issue date, not the highest internal id or the first row.
- The company name "Company X" in the task is ambiguous only when the environment contains an alias; otherwise it maps to "Company X Ltd".
- A human is available to approve writes and answer questions during interactive runs; evals use scripted responders instead. In multi-user mode the approver is a different person from the operator when separation of duties is on.
- "Payment date" for the vendor-reply task is taken to mean the invoice due date, as stated in the task text.
- The amount to record is the invoice's total as printed on it (including tax), in the invoice's own currency; this is what the evals check against.
- Each organization models a separate company with its own external systems, so a SQLite database per tenant in the simulator is a faithful stand-in and stays separate from the platform's PostgreSQL.
- Single-user mode is for one local user; multi-user mode is for a small trusted team behind its own network or login. It has not been security-audited.

---

## 17. Models, APIs, frameworks and components used

| Component | Use |
|---|---|
| **Google Gemini API**, a failover chain starting with `gemini-3.5-flash` (the default list is in [worker/config.py](worker/config.py), overridable with `GEMINI_MODELS` and per organization) via the **`google-genai`** SDK | Primary LLM, native function calling, free tier |
| **Ollama** (optional, default model `qwen2.5:3b`) | Local fallback behind the same adapter |
| **PolicyLLM** ([worker/llm/policy.py](worker/llm/policy.py), `LLM_PROVIDER=policy`) | Deterministic stand-in for a model, used only by the load test |
| **PostgreSQL** with **SQLAlchemy 2.0** (async), **asyncpg**, **Alembic** | System of record, job queue, LISTEN/NOTIFY, shared limits |
| **Playwright** (Chromium) | Real browser automation, pooled per process |
| **FastAPI**, **uvicorn**, **Jinja2**, `python-multipart`, `itsdangerous` | API, WebSocket events, the simulated company's web apps and sessions, the optional access gate |
| **argon2-cffi**, **cryptography** (Fernet) | Password hashing; credential vault |
| **prometheus-client**, **psutil** | `/metrics`; the load test's process measurements |
| **SQLite** (stdlib `sqlite3`) | Simulator data store (one file per tenant) |
| **reportlab** | Generates the invoice PDFs |
| **pypdf** | Extracts text from PDFs for the `read_file` tool |
| **httpx** | HTTP tool, server-to-simulator calls, health checks |
| **pydantic**, `pydantic-settings`, `python-dotenv` | Settings and request models |
| **jsonschema** | Validates tool-call arguments before execution |
| **PyYAML** | Eval scenario files and dashboard presets |
| **pytest**, `pytest-asyncio` | Test suite |
| Vanilla JavaScript, HTML, CSS | Dashboard (no build step, no CDN) |

No agent framework (LangChain, LlamaIndex, AutoGen and similar) is used, deliberately; see [decision 3.11](#311-no-agent-framework). All dependencies are free and open source; the only external service is the Gemini API. (The load test also imports `websockets`, which comes with `uvicorn[standard]`.)

---

## 18. Project structure

<details>
<summary><b>The repository tree (click to expand)</b></summary>

```
oneforall-agent/
├── run.py                    start simulator (:8001) + API/dashboard (:8000) [+ N workers in database mode]
├── requirements.txt  pytest.ini  alembic.ini  .env.example  Dockerfile  .gitignore
├── requirements-dev.txt  constraints.txt  pyproject.toml  .gitleaks.toml    dev tools, exact pinned versions, ruff config, secret-scan allowlist
├── .github/                  workflows/ (ci, security, nightly, evals, deploy, _deploy-env, rollback), scripts/ (summary helpers), dependabot.yml
├── scripts/
│   ├── bootstrap.py          create the database, migrate, seed demo orgs/users, --provision-sim
│   └── loadtest.py           the load test (writes docs/loadtest-results.json and .md)
├── deploy/
│   ├── start.py              container entry point: role-based supervisor (simulator, resync, API, job workers)
│   └── gcp/setup.sh          one-time Google Cloud setup (APIs, registry, service accounts, Workload Identity, secrets, budget)
├── company_sim/              simulated company (FastAPI + Jinja2 + SQLite), multi-tenant
│   ├── app.py  __main__.py   app wiring, `python -m company_sim`
│   ├── apps/                 mail.py  portal.py  erp.py (UI + REST API)  admin.py (chaos, reset, tenants, state)
│   ├── tenants.py            tenant registry, per-tenant db/PDF paths, provisioning
│   ├── tenant_web.py         entry route /t/<token>/, cookie and API-key tenant resolution
│   ├── seed.py  db.py        deterministic seed data, schema
│   ├── chaos.py              failure-injection flags (per tenant)
│   ├── auth.py  state.py     cookie sessions (per tenant); ground-truth queries for evals
│   ├── pdfgen.py             invoice PDFs
│   └── templates/ static/    HTML for the three apps
├── worker/
│   ├── config.py             settings (env / .env)
│   ├── server.py             API app factory: file-mode run endpoints, wiring of the routers, scenarios, sim proxy
│   ├── auth.py               sign-in, sessions, roles, OrgContext, CSRF guard, throttling, audit
│   ├── admin_api.py          members, org settings, credentials, environment, audit
│   ├── runs_api.py           database-mode runs, respond, approvals inbox, event WebSockets
│   ├── memory_api.py  usage_api.py  system_api.py   org memory admin, usage reports, /healthz /readyz /metrics, System card
│   ├── jobqueue.py           enqueue, claim, lease, heartbeat, reap, cancel (the SQL of the queue)
│   ├── jobs.py               the worker process: `python -m worker.jobs`
│   ├── run_context.py        what starting a run needs (org settings, environment, LLM)
│   ├── approvals_pg.py       durable approval rows and their state changes
│   ├── live.py  pg_listen.py event fan-out hub; the LISTEN connection with reconnect
│   ├── store.py  store_pg.py RunStore protocol, FileRunStore; PgRunStore (org-scoped)
│   ├── event_log.py          events synthesized for runs without a log
│   ├── vault.py  vault_pg.py credential vault (Fernet), file and PostgreSQL backends; `python -m worker.vault`
│   ├── redaction.py          Redactor and RedactingStore
│   ├── provisioning.py       create/reset an org's simulated company, store its logins; `python -m worker.provisioning resync`
│   ├── org_settings.py       per-org settings, approval rights, separation of duties
│   ├── memory_store.py       organization memory: screening, write gate, recall, governance
│   ├── usage.py              usage ledger, daily budget guard, reports
│   ├── metrics.py  logging_setup.py  observability.py  health.py    Prometheus metrics, JSON logs, request middleware, readiness
│   ├── deploy.py             opt-in hosting helpers: access gate, sim proxy, daily cap
│   ├── db/                   models.py  session.py  migrate.py  bootstrap.py  seed.py  migrations/versions/ (5 revisions)
│   ├── agent/                runner.py  context.py  state.py  memory.py  policy.py  guards.py  verifier.py
│   │                         report.py  broker.py  pg_broker.py  prompts.py  thoughts.py  toolset.py  cli.py
│   ├── llm/                  base.py  gemini.py  ollama.py  failover.py  resilient.py  factory.py  fake.py
│   │                         limiter.py  health_pg.py  policy.py
│   ├── tools/                base.py  registry.py  retry.py  browser.py  browser_pool.py  snapshot.py/.js
│   │                         files.py  http.py  secrets.py  memory_tools.py  interaction.py  control.py
│   └── ui/                   index.html  app.js  admin.js  approvals.js  memory.js  styles.css (dashboard)
├── evals/                    scenarios/*.yaml, run.py (runner), checkers.py, scenarios.py, simstate.py, results/
├── tests/                    676 tests (scripted LLM, local HTML fixtures, a throwaway PostgreSQL database)
├── workspace/                the agent's "shared drive": credentials.json, README.txt
├── docs/                     ARCHITECTURE.md  MULTI_USER.md  DEPLOY.md  SCALING.md  DEMO_SCRIPT.md  loadtest-results.md/.json
└── runs/                     (generated) per-run screenshots, downloads, report.md; file-mode run logs
```

</details>

---

## 19. Reference: simulated company

> [!WARNING]
> The logins below belong to the simulated company's fake data and are for development only. Real credentials never go in this repository.

<details>
<summary><b>Logins, seeded data, chaos flags and admin endpoints (click to expand)</b></summary>

The default tenant (used in single-user mode and by the CLI). Credentials (also in [workspace/credentials.json](workspace/credentials.json), which the agent is expected to find in single-user mode):

| App | URL | Login |
|---|---|---|
| Mail (shared AP inbox) | <http://localhost:8001/mail> | `ap@acme.example` / `mail-pass-123` |
| Vendor portal | <http://localhost:8001/portal> | `acme-ap` / `portal-pass-456` |
| ERP "Ledger" | <http://localhost:8001/erp> | `ap.clerk` / `ledger-pass-789` |
| ERP REST API | <http://localhost:8001/erp/api> (docs at `/erp/api/docs-summary`) | header `X-API-Key: ledger-api-key-001` |

In multi-user mode every organization has its own tenant with generated passwords and API key, stored in the vault and never shown to the agent; to browse an org's company by hand, a member with the operator role or above can use the dashboard's "View company systems" link when the sim proxy is on (`PUBLIC_SIM_PROXY`).

Seeded data: six vendors (including both **Company X Ltd** and **Company X Holdings**), 14 portal invoices in non-chronological order (5 per page), inbox emails (three with PDF invoices, a vendor asking about the payment date of INI-20502, and noise), and five historical ERP bills.

Chaos flags (toggle in the dashboard, with `POST /admin/chaos`, or `SIM_CHAOS=a,b` when starting the sim; data flags trigger a reseed; in multi-user mode an org admin sets them for the org's own tenant):

| Flag | Effect |
|---|---|
| `erp_submit_500_once` | First ERP form submit (and first ERP API POST) returns HTTP 500, then works |
| `erp_session_expiry` | ERP session expires after the 3rd page view, once per reset |
| `duplicate_bill` | Latest Company X Ltd invoice already exists as an ERP bill |
| `ambiguous_vendor` | Company X Holdings has an invoice with the same issue date as Company X Ltd's latest |
| `portal_missing_invoice` | Latest Company X Ltd invoice is hidden from the portal (email attachment only) |
| `slow_pages` | 1 to 3 seconds random latency on page loads |

Admin endpoints (open unless `SIM_ADMIN_TOKEN` is set; localhost only): `GET/POST /admin/chaos`, `POST /admin/reset` (optional body `{"chaos": [...]}`), `GET /admin/state` (bills and sent mail, the ground truth the evals read) for the default tenant, and `/admin/tenants[...]` to create, list, reset, inspect and delete the others.

</details>

---

## 20. Troubleshooting

<details>
<summary><b>Symptoms and fixes (click to expand)</b></summary>

| Symptom | Fix |
|---|---|
| `DATABASE_URL is not set` / `error: could not bootstrap` / a connection error | Check the URL in `.env` (`postgresql+asyncpg://postgres:PASSWORD@localhost:5432/oneforall`), that the PostgreSQL service is running, and run `python -m worker.db check`: it prints the masked URL, the server version and the migration state without ever printing the password. The bootstrap connects to the `postgres` database to create `oneforall`, so the role must be allowed to create databases. |
| Log says "The database schema is at revision X but the code needs Y"; `/readyz` returns 503 with `migrations` not ok | Run `python -m worker.db migrate` (or `python scripts/bootstrap.py`, which also migrates). |
| Runs stay `queued` | No worker is running (`python run.py --workers N` or `python -m worker.jobs`), the org is at its `max_concurrent_runs` (default 2; they wait for a free slot), or workers cannot reach the database. The Admin page's System card shows the queue and the workers holding runs. |
| `This organization has no environment yet` (409) when starting a run | Provision the org's simulated company: `python scripts/bootstrap.py --provision-sim` (the simulator must be running), or **Admin > Provision environment**. A 502 there means the simulator is not reachable at `SIM_BASE_URL`. |
| `cannot decrypt credential ... wrong VAULT_KEY?` | The API and the workers must use the same `VAULT_KEY` (or the same `.vault/.vault_key` file). |
| A run ends with "approval expired" | Nobody answered within the org's `approval_timeout_s` (default 1 hour). Start it again and answer from the Approvals page. |
| HTTP 429 when starting a run, "daily limit", or a run stops with "Organization daily LLM budget reached" | The org's daily run, token or LLM-call budget is used up (UTC day; defaults 50 runs, 2,000,000 tokens, 500 calls). An admin can raise them under **Admin > Organization settings**. A 429 on sign-in means too many failed attempts: wait for the window (15 minutes) to end. |
| Run stops with "language model could not be reached" or log shows 429 / `RESOURCE_EXHAUSTED` | Free-tier quota or rate limit. The client already backs off and honours retry hints; wait a minute, raise `LLM_MIN_INTERVAL_S` (for example `8`), or edit the `GEMINI_MODELS` chain. A model whose free-tier daily quota (about 20 requests per model per day) is used up is skipped automatically and the run continues on the next model; "All configured Gemini models are out of free-tier quota" means every model in the chain is used up for the day. |
| `GEMINI_API_KEY is not set` / dashboard badge says no key | Create `.env` from `.env.example` and set the key; restart `python run.py`. |
| `Chromium is not installed. Run: python -m playwright install chromium` | Run exactly that in the activated venv. On Linux also `python -m playwright install-deps chromium`. |
| `port(s) 8000/8001 already in use` | Another instance is still running. On Windows: `netstat -ano \| findstr :8000`, then `taskkill /PID <pid> /F`. On macOS/Linux: `lsof -i :8000`. The load test uses ports 8601 and up so it never collides with these. |
| Browser tools fail with `NotImplementedError` on Windows | Playwright needs subprocess support in the event loop. Start via `python run.py` or `python -m worker.server`; **do not use `uvicorn --reload`**, which can switch to a loop that cannot spawn Chromium. |
| Dashboard says the environment is not reachable | The simulator is not running. Use `python run.py`, or start `python -m company_sim` yourself. |
| Data looks wrong or a previous run changed things | Press **Reset environment now** in the dashboard (disabled during a run; in multi-user mode it resets the org's own tenant and needs the admin role) or `POST http://localhost:8001/admin/reset`. Deleting `company_sim/data/` also forces a clean reseed on next start. |
| `409 Run ... is still active` | Single-user mode runs one at a time; cancel the active run or wait. |
| Many tests are skipped | PostgreSQL is not reachable with `DATABASE_URL`; the tests that need it skip rather than fail. |
| Browser window does not appear | Switch on **Headed browser** in the dashboard or pass `--headed` to the CLI; `HEADLESS=true` is the default, and a deploy-mode server is always headless. |
| Eval or test failures because the date moved | Seed dates are relative to today. Pin them with `SIM_TODAY=YYYY-MM-DD` in the shell when starting the simulator. |

</details>

---

<p align="center">
  <sub>Built by <a href="https://github.com/adieladaniel">Adiel A Daniel</a> &nbsp;·&nbsp; <a href="LICENSE">MIT License</a> &nbsp;·&nbsp; <a href="#top">Back to top</a></sub>
</p>
