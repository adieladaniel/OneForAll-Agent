# Deploying OneForAll Agent to Google Cloud Run

This is the runbook for the hosted, multi-user deployment: one container image, one Cloud Run service per environment
(staging and production), a free Neon PostgreSQL database, secrets in Secret Manager, and GitHub Actions doing the
building, the migration, the smoke test and the traffic switch. The code side is in `Dockerfile`, `deploy/start.py`,
`.github/workflows/deploy.yml`, `_deploy-env.yml` and `rollback.yml`; the one-time cloud setup is `deploy/gcp/setup.sh`.

Contents: [architecture](#1-architecture) | [why these Cloud Run settings](#2-why-one-instance-and-instance-based-billing) |
[Neon](#3-the-database-neon) | [one-time setup](#4-one-time-setup) | [the pipeline](#5-the-pipeline-step-by-step) |
[first deploy](#6-first-deploy-and-seeding) | [rollback](#7-rollback) | [costs](#8-costs-and-free-tiers) |
[limitations](#9-known-limitations) | [troubleshooting](#10-troubleshooting)

---

## 1. Architecture

```
                         GitHub                                   Google Cloud project
  push to main --> CI workflow (lint, tests, docker smoke test)
                      |  success
                      v
                  Deploy workflow
                   build image ---------------------------------> Artifact Registry  (oneforall/oneforall-agent@sha256:...)
                      |   (Workload Identity Federation, no key)
                      v
          staging  ---------------------------------------------> Cloud Run service  oneforall-agent-staging
                      |  required reviewers approve                   |
                      v                                               |   secrets from Secret Manager
          production --------------------------------------------> Cloud Run service  oneforall-agent-prod
                                                                      |
   inside ONE container (ROLE=all), started by deploy/start.py        |
   +------------------------------------------------------------------+---------------+
   |  company_sim   127.0.0.1:8001   the simulated company (SQLite tenants, ephemeral) |
   |  resync        python -m worker.provisioning resync   (once, before the API)      |
   |  API           0.0.0.0:8080     uvicorn worker.server:app  (dashboard, /healthz)  |
   |  job worker    python -m worker.jobs     (claims runs, runs Chromium)             |
   +----------------------------------------+-----------------------------------------+
                                            | TLS, direct (non-pooler) endpoint
                                            v
                                   Neon PostgreSQL (free tier): users, runs, queue, vault, memory
```

Two things to keep in mind:

* The **database is the only durable state**. The container's disk (the simulator's tenants, run screenshots and
  downloads) disappears whenever the instance is replaced or scaled to zero.
* The API only queues runs. The job worker in the same container claims them (`FOR UPDATE SKIP LOCKED`), keeps a lease
  alive with a heartbeat, writes a checkpoint after every step and gives the run back to the queue when it is stopped.

`ROLE` chooses what a container runs (`deploy/start.py`): `all` (this deployment), `api`, `worker` or `sim`. The other
roles exist for hosts where you want the pieces apart (a VM, Kubernetes, a Cloud Run worker pool); a Cloud Run
**service** must listen on `$PORT`, so `ROLE=worker` alone is not usable as a service.

## 2. Why one instance and instance-based billing

| Setting | Value | Reason |
|---|---|---|
| `--max-instances` | **1** | The simulated company lives inside the container. A second instance would have its own, different tenants (different logins and tokens) while the vault in PostgreSQL holds only one set, so runs on the other instance would fail. One instance keeps one company per organization. |
| `--no-cpu-throttling` (instance-based billing) | on | The job worker is a background process. With request-based billing Cloud Run gives a container CPU only while a request is in flight, so a run would freeze between WebSocket messages. |
| `--min-instances` | 0 | The service costs nothing while nobody uses it. The first request starts the container (about a minute: simulator, resync, API, worker). |
| `--cpu 2 --memory 2Gi`, `--cpu-boost` | | Chromium plus the API plus the simulator need it. CPU boost speeds up the start. |
| `--timeout 3600` | | The dashboard watches runs over WebSockets; this is the longest a connection may live. |
| `--session-affinity` | on | Keeps a browser tab on the same instance if there is ever more than one (for example during a deploy). |
| `--execution-environment gen2` | | Full Linux compatibility, which Chromium prefers. |
| `--allow-unauthenticated` | on | The app has its own sign-in; Cloud Run IAM would block the browser before the login page. |

**When the instance stops in the middle of a run** (it is scaled to zero after being idle, replaced by a deploy, or the
platform moves it): the worker hands its runs back on a graceful stop; if it is killed instead, the lease (30 s) expires.
In both cases the run goes back to the queue and **resumes from its last checkpoint the next time an instance starts**
(the next visit to the site starts one). This is intended behaviour, not a failure. Files the run had saved under
`RUNS_DIR` are lost with the instance; the run's steps, events and report text are in the database.

## 3. The database: Neon

1. Create a Neon project (free plan). Pick a region close to the Cloud Run region (for `asia-south1`, a nearby AWS
   region such as Singapore works well).
2. In the dashboard, open **Connect** and turn **Connection pooling off**. Copy the connection string. It looks like
   `postgresql://USER:PASSWORD@ep-xxxx.REGION.aws.neon.tech/DBNAME?sslmode=require&channel_binding=require`.
   **Use the direct host (no `-pooler` in the name).** The platform relies on `LISTEN/NOTIFY` and session-level
   features, which do not work through PgBouncer in transaction mode. If you paste a pooler URL anyway, the app logs
   a warning at start.
3. Store it as the secret `<prefix>-database-url` (the setup script prints the exact command). You can use one Neon
   project with two databases (or two branches) for staging and production; use a separate URL per environment.

The code accepts the URL exactly as Neon prints it: `sslmode=require` is translated to the `ssl=require` that asyncpg
wants and `channel_binding` (which asyncpg does not know) is dropped, for the SQLAlchemy engines, the migrations, the
`LISTEN` connection and `scripts/bootstrap.py`.

The free compute scales to zero when idle. While a container is running, the worker polls the database every couple
of seconds, so the database stays awake for as long as the Cloud Run instance does.

## 4. One-time setup

You need: a Google Cloud project with billing enabled, `gcloud` logged in as an owner (Cloud Shell is easiest), a GitHub
repository (suggested name `oneforall-agent`) and a Neon database.

1. **Run the setup script** in Cloud Shell, from a checkout of the repository:

   ```bash
   PROJECT_ID=my-project GITHUB_REPO=my-user/oneforall-agent BILLING_ACCOUNT=012345-ABCDEF-678901 \
     bash deploy/gcp/setup.sh
   ```

   `BILLING_ACCOUNT` is optional (without it no budget alert is created); `REGION` defaults to `asia-south1` and
   `BUDGET_AMOUNT` to 5 (USD per month). The script is safe to run again. It:

   * enables the APIs (Cloud Run, Artifact Registry, Secret Manager, IAM, IAM Credentials, STS, Billing Budgets);
   * creates the Artifact Registry repository `oneforall`, keeping the 3 newest images (each is about 2 GB with Chromium, and storage above the small free allowance is billed);
   * creates the service accounts `ofa-deployer` (Cloud Run admin, Artifact Registry writer on that repository, and
     permission to act as `ofa-runtime` only) and `ofa-runtime` (reads the secrets, and is the identity the app runs as);
   * creates the Workload Identity pool `github` and provider `github-oidc`, limited to your repository, so GitHub Actions
     can deploy **without any stored key**;
   * creates the secrets `ofa-gemini-api-key` and, for each of `ofa-staging-*` and `ofa-prod-*`: `database-url`,
     `session-secret`, `vault-key`, `metrics-token`, `sim-tenant-secret`, `demo-password`. It generates every one of them except the database URL (the vault key is a
     Fernet key) and **never replaces a secret that already has a value**;
   * creates a budget with alerts at 50%, 90% and 100%.

2. **Add the two secrets it cannot generate.** The script prints the commands:

   ```bash
   gcloud secrets versions add ofa-gemini-api-key     --project my-project --data-file=-
   gcloud secrets versions add ofa-staging-database-url --project my-project --data-file=-
   gcloud secrets versions add ofa-prod-database-url    --project my-project --data-file=-
   ```

   Paste the value, press Enter, then Ctrl+D. (`--data-file=-` reads standard input, so the value never appears in your
   shell history or in a log.)

3. **Set the GitHub variables** the script prints (Settings > Secrets and variables > Actions > Variables):
   repository variables `GCP_PROJECT_ID`, `GCP_REGION`, `GAR_REPOSITORY` (`oneforall`), `WIF_PROVIDER`, `DEPLOYER_SA`,
   `RUNTIME_SA`. It also prints `gh variable set` commands if you prefer the GitHub CLI.

4. **Create the GitHub Environments** (Settings > Environments): `staging` and `production`. On each set the
   environment variables `CLOUD_RUN_SERVICE` and `SECRET_PREFIX` (staging: `oneforall-agent-staging` and `ofa-staging`;
   production: `oneforall-agent-prod` and `ofa-prod`). On **production add Required reviewers**: the pipeline then pauses
   for an approval before it touches production.

Nothing is deployed until `GCP_PROJECT_ID` is set: until then the Deploy workflow skips every job, so pushes stay green.

## 5. The pipeline, step by step

Every push to `main` runs **CI** (`ci.yml`). When CI succeeds, **Deploy** (`deploy.yml`) starts (it ignores pull requests,
forks and failed runs). It can also be started by hand (Actions > Deploy > Run workflow).

1. **build**: checks out the exact commit CI tested, signs in to Google through Workload Identity Federation, builds the
   Docker image and pushes it as `.../oneforall/oneforall-agent:<full commit sha>`. The jobs after it use the image
   **by digest**, so staging and production run byte-for-byte the same image.
2. **staging**, then **production** (production waits for the reviewers you set). Each runs `_deploy-env.yml`:
   1. **Migrate**: a Cloud Run job `<service>-migrate` runs `python3 -m worker.db migrate` with that environment's
      `DATABASE_URL` secret. It waits for the result and does not retry; a failed migration stops the deploy before any new
      code is live.
   2. **Seed** (only if you ticked `seed_demo`): a job `<service>-seed` runs `python3 scripts/bootstrap.py` with
      `BOOTSTRAP_DEMO_PASSWORD` taken from the secret `<prefix>-demo-password`. Existing users keep their passwords, and
      the password is not printed to the job log.
   3. **Deploy without traffic**: records the revision that serves now, then deploys the new revision with `--no-traffic`
      and the tag `sha-<first 7 of the commit>`, so it has its own URL. (The very first deploy of a service has nothing to
      protect and `--no-traffic` is not allowed for a service that does not exist, so that revision takes traffic at once.)
   4. **Smoke test** on the revision's own URL: `/readyz` (retried for up to 3 minutes, because the first request starts
      the container: simulator, environment resync, API, worker), then `/api/health` and `/` (the dashboard and sign-in page). It does not use `/healthz`: Google's front end reserves that path on `run.app` URLs and answers 404 without reaching the container.
   5. **Switch traffic**: only after the smoke test passes, `update-traffic --to-latest` sends 100% to the new revision.
      If the smoke test fails, traffic stays on the previous revision and the job fails with the previous revision name and
      the commands to inspect it.
   6. A job summary lists the URL, the image, the new and the previous revision.

**Why a deploy does not break running tasks.** While a new revision is smoke tested, its container (with its own simulator)
runs next to the old one, and its start-up resync writes each organization's logins into the shared vault. If those logins
were random, the vault would then match only the new simulator and runs starting on the old one would fail. With the
secret `<prefix>-sim-tenant-secret` mounted as `SIM_TENANT_SECRET`, the simulator derives a tenant's entry token, ERP API
key and passwords as `HMAC-SHA256(secret, "<tenant id>:<purpose>")` in the same format and length as the random ones. The
same secret and tenant id therefore give identical logins in every container, so a restart, a new revision and the old
revision all agree with the vault, and a failed smoke test leaves the old revision fully working. The seeded sample
data already depended only on the tenant id. Keep the secret private (anyone who has it can compute every tenant's token),
and do not change it casually: changing it changes every tenant's logins, which the next resync then rewrites in the vault.
Without `SIM_TENANT_SECRET` (local development, the single-user container) tenants stay random, exactly as before.
The secret never appears in logs; the simulator also hides `/t/<token>/` entry paths in its access log.

What the container does at start (`deploy/start.py`, `ROLE=all`): start the simulator, wait until it is healthy, run
`python -m worker.provisioning resync` (re-creates every organization's simulated company and rewrites its logins in the
vault, because the simulator's data does not survive a restart), start the API, wait for `/healthz`, start the job
worker. If any process dies, the others are stopped and the container exits so Cloud Run restarts it. On SIGTERM the job
worker stops first (so it can hand its runs back), then the API, then the simulator.

## 6. First deploy and seeding

1. Make sure CI is green on `main` and the setup is complete (section 4).
2. Actions > **Deploy** > Run workflow > tick **seed_demo** > Run. Staging deploys; approve production when asked (or stop
   there and deploy production later).
3. Open the service URL from the job summary. Sign in as `owner@acme.test` with the password of that environment:

   ```bash
   gcloud secrets versions access latest --secret ofa-staging-demo-password --project my-project
   ```

   The demo organizations are `acme` (owner, admin, operator, approver, viewer) and `globex` (operator, approver).
4. The first request after a quiet period takes about a minute while the container starts. Each organization's simulated
   company is created during that start, so runs work immediately afterwards.

`seed_demo` only creates what is missing, so ticking it again is harmless. The demo password is fixed by the secret: to
change it, add a new version of the secret and delete the users, or change passwords in the app.

## 7. Rollback

Actions > **Rollback** > Run workflow: choose the environment and, optionally, a revision. Without a revision it picks the
newest ready revision that is **not** serving now. It moves 100% of the traffic to it in a few seconds; nothing is rebuilt.
Production rollbacks also wait for the environment's required reviewers.

The same thing by hand:

```bash
gcloud run revisions list --service oneforall-agent-prod --region asia-south1
gcloud run services update-traffic oneforall-agent-prod --region asia-south1 --to-revisions REVISION=100
```

The next successful Deploy sends traffic to its new revision again (`--to-latest`). A rollback does not undo database
migrations: write migrations so the previous version can still run against the new schema (add columns before using them,
remove them in a later release).

## 8. Costs and free tiers

Prices and free allowances change; check the current pricing pages before relying on any number here.

* **Cloud Run** with instance-based billing charges for the whole life of an instance (CPU and memory), not just request
  time, so a 2 vCPU / 2 GiB instance that stays up for an hour costs that hour. With min instances 0 and one instance, you
  pay only while someone has used the site recently (an instance lingers a few minutes after the last request, and while a
  WebSocket is open). The Cloud Run free tier covers a small amount of instance time every month; a few demo sessions fit,
  an instance left running all month does not.
* **Neon free plan**: a small shared compute that scales to zero, with a monthly compute-hours allowance and a storage limit.
  The database is awake while the container is running (see section 3).
* **Artifact Registry**: a free allowance of storage; the cleanup policy keeps only the 3 newest images (enough to roll back two releases).
* **Secret Manager**: a few secrets with a few accesses a month are inside the free allowance.
* **Gemini API free tier**: rate and daily limits per model; the app already fails over between models and backs off on 429.
  A busy day can exhaust the free quota; runs then wait or fail with a clear message.
* **Budget alert**: the setup script creates a monthly budget (default 5 USD) with e-mails at 50%, 90% and 100%. A budget
  **only alerts**; it does not stop spending. To cap spending, delete the Cloud Run services or disable billing when an alert arrives.

## 9. Known limitations

* **The simulated company is ephemeral.** Its SQLite data lives on the instance's disk. Every start (a deploy, a scale from
  zero, a crash) recreates each organization's tenant from the seed, with the same logins when `SIM_TENANT_SECRET` is set
  (new random ones without it, which the start-up resync then writes to the vault). Whatever the agent changed in the
  simulated mail, portal or ERP is gone after a restart. Run history in PostgreSQL is not.
* **Run artifacts are ephemeral too** (screenshots and downloads under `RUNS_DIR`); see limitation "Artifacts are local files" in
  the README.
* **Single instance.** `max-instances 1` limits throughput to one worker process with `WORKER_CONCURRENCY=2` runs at a time
  and one Chromium. This is a demo-scale deployment.
* **Simulated-company data is lost on a restart; its logins are not.** Bills, mail and ERP records created or changed
  inside a container's simulator disappear when the container does, which is expected: the simulator stands in for
  external systems. The *credentials* of each organization's company are stable, because `SIM_TENANT_SECRET` makes them
  deterministic (see below).
* **Cold starts.** After a quiet period the first request waits for the container to start (about a minute).
* **A stopped instance does not work through the queue.** Runs are executed only while an instance is up; queued runs wait
  until the next start.
* **Demo accounts in production.** `seed_demo` creates users with a known (secret-stored) password. Leave it unticked on
  production if you do not want demo users there.

## 10. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Deploy workflow shows every job as skipped | `GCP_PROJECT_ID` is not set, or CI did not succeed on a push to `main`. Set the variable, or run Deploy by hand. |
| `google-github-actions/auth` fails: "unable to impersonate" | `WIF_PROVIDER` or `DEPLOYER_SA` is wrong, or the provider's condition does not match your repository (re-run the setup script with the right `GITHUB_REPO`). The repository must be the one that runs the workflow. |
| Permission denied on a secret | The secret has no version yet, or `ofa-runtime` lacks access. Re-run `setup.sh` (it re-applies the bindings) and add the missing value. |
| Migration job fails with a connection error | Wrong `database-url` secret, or the Neon project is paused or out of allowance. Read the job's logs: `gcloud run jobs executions list --job oneforall-agent-staging-migrate --region asia-south1`. |
| Warning "points at a pooled endpoint" in the logs | The URL uses Neon's `-pooler` host. Use the direct host. |
| Smoke test: `/readyz` never returns 200 | Open the logs: `gcloud run services logs read SERVICE --region REGION --limit 100`. Usual causes: the migration did not run (readiness reports `migrations`), `VAULT_KEY` or `SESSION_SECRET` missing, or the resync could not reach the simulator (`sim` check). |
| `startup probe failed` / container fails to start in 4 minutes | Cloud Run waits for the port to answer; the container starts the API only after the simulator and the resync. If the database is slow to wake it can exceed the probe. Re-run the deploy; check the `resync` lines in the logs. |
| Sign-in works, but runs fail with a login error in the simulated company | The vault holds logins that this simulator does not have: usually `SIM_TENANT_SECRET` was changed or removed. Restore the secret (or deploy again so the start-up resync rewrites the vault). |
| Dashboard loads but a run never starts | No job worker is running: check the logs for `worker ... started`. The container logs a line when a process exits. |
| `Revision ... is not ready` after a deploy with a new secret | The runtime account cannot read the secret version `latest`. Check the secret has a version and the binding. |

Useful commands:

```bash
gcloud run services describe oneforall-agent-prod --region asia-south1 --format='value(status.url)'
gcloud run services logs read oneforall-agent-prod --region asia-south1 --limit 100
gcloud run revisions list --service oneforall-agent-prod --region asia-south1
```

Health endpoints (no sign-in): `/healthz` (the process answers; on Cloud Run use `/api/health` instead, because Google reserves `/healthz` on `run.app` URLs), `/readyz` (database, migrations, simulator), and `/metrics`
with `Authorization: Bearer <metrics-token>`.
