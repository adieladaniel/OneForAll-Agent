"""Evaluation harness: ``python -m evals.run [--only a,b] [--repeat N] [--parallel N] [--headed] [--provider gemini|ollama]``.

For every scenario: reset the sim with the scenario's chaos flags, snapshot the baseline, run the *unchanged* agent
in-process with scripted approvals and answers, then judge the outcome with a deterministic checker against the sim's
ground truth (never the agent's own claims, never an LLM). Prints a table, writes ``evals/results/<timestamp>.json``
and ``evals/results/latest.md``. Exit codes: 0 all passed, 1 some failed, 2 setup problem (including an environment
error, see below), 130 interrupted.

The harness knows which simulator it talks to: it starts one with a fresh instance id (or adopts the identifiable one
already running), checks before every scenario and again right before grading that the server at SIM_BASE_URL has that id
and that no other address of "localhost" (127.0.0.1, ::1) answers with another one (``evals/instance.py``), and aborts
with a setup error otherwise. Checkers grade from the invoices and inbox the sim actually holds; when those disagree with
the seed formula the scenario is reported as ``ENV ERROR`` (not graded, not counted as passed) with the differing fields.

By default the scenarios run one after the other on the sim's default tenant. With ``--tenant-per-scenario`` (implied by
``--parallel N`` > 1) each scenario instead gets its own freshly provisioned tenant: a private copy of the company with
its own logins and chaos flags, an entry link the agent starts from, and its own workspace folder holding that tenant's
``credentials.json``. Scenarios then cannot disturb each other, so up to N run concurrently, sharing one browser pool.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from company_sim.seed import workspace_files
from evals.checkers import CheckResult, classify_outcome, run_check
from evals.instance import LOCAL_HOSTS, SimSetupError, ensure_instance, find_running_sim, probe
from evals.scenarios import Scenario, ScenarioError, ScriptedHuman, load_scenarios
from evals.simstate import admin_headers, build_sim_state, delete_tenant, fetch_admin_state, provision_tenant
from worker.agent.broker import AutoBroker
from worker.agent.runner import AgentRunner
from worker.agent.toolset import build_registry
from worker.config import Settings, get_settings
from worker.llm.base import LLMError
from worker.llm.factory import make_llm

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = Path(__file__).resolve().parent / "results"
OUTCOME_LABEL = {"pass": "PASS", "fail": "FAIL", "false_success": "FALSE SUCCESS", "error": "ERROR", "env_error": "ENV ERROR"}


# --------------------------------------------------------------------------- sim process


@contextlib.contextmanager
def running_sim(base_url: str):
    """Yield the identity (``/healthz``: instance_id, sim_today, pid ...) of the sim that serves ``base_url``.

    An identifiable sim that already answers is used (every address of "localhost" must refuse or give the same
    instance id). If anything unidentifiable answers on any of those addresses, or two instances do, this raises
    ``SimSetupError`` instead of starting a sim next to it. Otherwise ``python -m company_sim`` is started with a fresh
    ``SIM_INSTANCE_ID`` and stopped again afterwards.
    """
    running = find_running_sim(base_url)
    if running:
        print(f"[evals] using the sim already running at {base_url} (instance {running['instance_id']}, "
              f"pid {running.get('pid', '?')}, sim_today {running.get('sim_today', '?')})")
        yield running
        return
    parsed = urlparse(base_url)
    if parsed.hostname not in LOCAL_HOSTS:
        raise RuntimeError(f"The sim at {base_url} is not reachable and is not local, so it cannot be started here.")
    port = parsed.port or 80
    instance_id = uuid.uuid4().hex[:12]
    print(f"[evals] starting the sim on port {port} (python -m company_sim, instance {instance_id})")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    env = {**os.environ, "SIM_PORT": str(port), "SIM_INSTANCE_ID": instance_id}
    proc = subprocess.Popen([sys.executable, "-m", "company_sim"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    try:
        deadline = time.monotonic() + 45
        while True:
            answer = probe(parsed.hostname, port, parsed.scheme or "http")
            if answer.state == "sim":
                break
            if proc.poll() is not None:
                raise RuntimeError(f"The sim exited early with code {proc.returncode}.")
            if time.monotonic() > deadline:
                raise RuntimeError("The sim did not become reachable within 45 s.")
            time.sleep(0.5)
        info = ensure_instance(base_url, instance_id)  # also proves nothing else came up beside it
        yield info
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        print("[evals] sim stopped")


# --------------------------------------------------------------------------- one scenario


def reset_sim(base_url: str, chaos: list[str]) -> None:
    resp = httpx.post(base_url.rstrip("/") + "/admin/reset", json={"chaos": chaos}, headers=admin_headers(), timeout=60.0)
    resp.raise_for_status()


def tenant_settings(settings: Settings, base_url: str, tenant: dict) -> tuple[Settings, Path]:
    """Settings for a scenario in its own tenant: start at the tenant's entry link, and use a private workspace folder
    with that tenant's ``credentials.json`` (the shared workspace keeps only the default tenant's logins)."""
    workspace = Path(tempfile.mkdtemp(prefix=f"eval-{tenant['tenant_id']}-"))
    base = base_url.rstrip("/")
    for name, content in workspace_files(tenant["credentials"], base).items():
        (workspace / name).write_text(content, encoding="utf-8")
    update = {"WORKSPACE_DIR": str(workspace), "SIM_ENTRY_URL": base + tenant["entry_path"]}
    return settings.model_copy(update=update), workspace


async def verify_instance(base_url: str, instance_id: str | None) -> None:
    """Raise ``SimSetupError`` unless the sim at ``base_url`` (and every address of "localhost") is instance ``instance_id``."""
    if instance_id:
        await asyncio.to_thread(ensure_instance, base_url, instance_id)


def require_instance(admin_state: dict, instance_id: str | None) -> None:
    """A snapshot used for grading must come from the sim this run uses (not from a stale one or a local database file)."""
    got = admin_state.get("instance_id")
    if instance_id and got != instance_id:
        source = f"instance {got}" if got else "a local database file (the sim did not answer over HTTP)"
        raise SimSetupError(f"The sim state came from {source}, not from this run's instance {instance_id}.")


async def run_scenario(scenario: Scenario, settings: Settings, base_url: str, repeat: int = 1,
                       per_tenant: bool = False, instance_id: str | None = None) -> dict:
    """Run one scenario end to end and return its result record.

    ``per_tenant`` provisions a fresh tenant for it (and drops it afterwards); otherwise the default tenant is reset.
    With an ``instance_id`` the sim's identity is verified before the scenario starts and again right before grading
    (``SimSetupError`` aborts the run).
    """
    await verify_instance(base_url, instance_id)
    if not per_tenant:
        return await _run_scenario(scenario, settings, base_url, repeat, None, instance_id)
    tenant = await asyncio.to_thread(provision_tenant, base_url, scenario.chaos)
    scenario_settings, workspace = tenant_settings(settings, base_url, tenant)
    try:
        return await _run_scenario(scenario, scenario_settings, base_url, repeat, tenant["tenant_id"], instance_id)
    finally:
        delete_tenant(base_url, tenant["tenant_id"])
        shutil.rmtree(workspace, ignore_errors=True)


async def _run_scenario(scenario: Scenario, settings: Settings, base_url: str, repeat: int, tenant_id: str | None,
                        instance_id: str | None = None) -> dict:
    def snapshot() -> dict:
        return fetch_admin_state(base_url, fallback_flags=scenario.chaos, **({"tenant_id": tenant_id} if tenant_id else {}))

    async def snapshot_async() -> dict:
        # tenants run concurrently: keep the blocking HTTP call off the event loop (the default path stays as it was)
        return await asyncio.to_thread(snapshot) if tenant_id else snapshot()

    if not tenant_id:
        reset_sim(base_url, scenario.chaos)
    baseline = await snapshot_async()
    require_instance(baseline, instance_id)
    human = ScriptedHuman(scenario)
    broker = AutoBroker(human)
    run_id = f"eval-{scenario.id[:40]}-{uuid.uuid4().hex[:4]}"
    llm = make_llm(settings)
    runner = AgentRunner(scenario.task, llm, build_registry(settings), settings, broker, run_id=run_id)
    started, error, state = time.monotonic(), "", {}
    try:
        state = (await runner.run()).to_dict()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - record the crash and still judge the sim state
        error = f"{type(exc).__name__}: {exc}"
        state = {"status": "error", "final_report": None, "steps": runner.state.to_dict().get("steps", [])}
    duration = time.monotonic() - started
    await _close_browser(runner)

    await verify_instance(base_url, instance_id)  # a second sim may have come up while the agent worked
    final = await snapshot_async()
    require_instance(final, instance_id)
    sim = build_sim_state(final, baseline)
    if sim["env_errors"]:  # the sim's data is not what the seed says it should be: do not grade on it
        check: CheckResult = CheckResult(False, 0.0, [f"ENV ERROR: {e}" for e in sim["env_errors"]])
        outcome = "env_error"
    else:
        check = run_check(scenario.checker, state, sim, scenario.params)
        outcome = "error" if error and not check.passed else classify_outcome(state, check)
    report = state.get("final_report") or {}
    stats = report.get("stats") or {}
    return {
        "scenario": scenario.id, "title": scenario.title, "repeat": repeat, "chaos": scenario.chaos, "run_id": run_id,
        "tenant": tenant_id or "default", "instance_id": sim["instance_id"], "env_errors": sim["env_errors"],
        "agent_status": state.get("status", ""), "outcome": outcome, "passed": check.passed, "score": round(check.score, 3),
        "details": check.details, "agent_summary": report.get("summary", ""),
        "verification": {k: (report.get("verification") or {}).get(k) for k in ("ran", "passed", "inconclusive")},
        "steps": stats.get("steps", 0), "llm_calls": stats.get("llm_calls", 0), "tokens": stats.get("tokens", 0),
        "retries": stats.get("retries", 0), "tool_failures": stats.get("tool_failures", 0),
        "llm_retries": getattr(llm, "retries", 0),
        "model": stats.get("model") or getattr(llm, "active_model", None) or "",
        "model_switches": stats.get("model_switches", getattr(llm, "model_switches", 0)),
        "approvals_requested": len(human.approvals), "approvals_approved": sum(a["approved"] for a in human.approvals),
        "questions": len(human.questions), "approval_log": human.approvals, "question_log": human.questions,
        "duration_s": round(duration, 1), "error": error, "sim_today": sim["sim_today"],
    }


async def _close_browser(runner: AgentRunner) -> None:
    try:
        from worker.tools.browser import close_browser

        await close_browser(runner.ctx)  # the runner already closes it; this is a harmless safety net
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- output


def _fmt_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    line = lambda r: "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(r)).rstrip()  # noqa: E731
    return "\n".join([line(headers), "  ".join("-" * w for w in widths), *map(line, rows)])


def _truth(r: dict) -> str:
    return "ENV ERROR" if r["outcome"] == "env_error" else ("PASS" if r["passed"] else "FAIL")


def _row(r: dict) -> list[str]:
    label = OUTCOME_LABEL.get(r["outcome"], r["outcome"])
    return [r["scenario"] + (f" #{r['repeat']}" if r.get("show_repeat") else ""), label, f"{r['score']:.2f}",
            r["agent_status"] or "-", _truth(r), str(r["steps"]), str(r["llm_calls"]),
            str(r["tokens"]), str(r["retries"]), f"{r['approvals_approved']}/{r['approvals_requested']}",
            str(r["questions"]), f"{r['duration_s']:.0f}s"]


HEADERS = ["scenario", "result", "score", "agent said", "ground truth", "steps", "LLM calls", "tokens", "retries",
           "approvals", "questions", "time"]


def summarize(results: list[dict]) -> dict:
    """Counts. Environment errors are not graded: they are in ``env_errors`` and in none of ``passed`` / ``graded`` / the mean."""
    graded = [r for r in results if r["outcome"] != "env_error"]
    return {
        "runs": len(results), "graded": len(graded), "env_errors": len(results) - len(graded),
        "passed": sum(r["passed"] for r in graded),
        "false_successes": sum(r["outcome"] == "false_success" for r in graded),
        "mean_score": round(sum(r["score"] for r in graded) / len(graded), 3) if graded else 0.0,
        "llm_calls": sum(r["llm_calls"] for r in results), "tokens": sum(r["tokens"] for r in results),
        "duration_s": round(sum(r["duration_s"] for r in results), 1),
    }


def render_text(results: list[dict]) -> str:
    s = summarize(results)
    out = [_fmt_table(HEADERS, [_row(r) for r in results]), "",
           f"{s['passed']}/{s['graded']} passed, {s['false_successes']} false success(es) "
           f"(agent said 'succeeded' but ground truth disagreed), mean score {s['mean_score']:.2f}"
           + (f", {s['env_errors']} environment error(s) (not graded: the sim's data is not what the seed says)" if s["env_errors"] else "")
           + f", {s['llm_calls']} LLM calls, {s['tokens']} tokens, {s['duration_s']:.0f}s"]
    for r in results:
        if not r["passed"] or r["error"]:
            out += ["", f"{r['scenario']} ({OUTCOME_LABEL.get(r['outcome'])}):"] + [f"  {d}" for d in r["details"]]
            if r["error"]:
                out.append(f"  error: {r['error']}")
            if r["agent_summary"]:
                out.append(f"  agent summary: {' '.join(r['agent_summary'].split())[:300]}")
    return "\n".join(out)


def _md(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _model_cell(r: dict) -> str:
    model = r.get("model") or "-"
    return f"{_md(model)} (+{r['model_switches']} switch)" if r.get("model_switches") else _md(model)


def render_markdown(results: list[dict], meta: dict) -> str:
    """Markdown ready to paste into the README."""
    s = summarize(results)
    head = ["| Scenario | Chaos | Agent said | Ground truth | Score | Steps | LLM calls | Tokens | Retries | Approvals | Questions | Model | Time |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    rows = []
    for r in results:
        truth = "ENV ERROR" if r["outcome"] == "env_error" else (
            "PASS" if r["passed"] else ("FAIL (false success)" if r["outcome"] == "false_success" else "FAIL"))
        name = f"`{r['scenario']}`" + (f" #{r['repeat']}" if r.get("show_repeat") else "")
        rows.append(f"| {name} | {_md(', '.join(r['chaos']) or '-')} | {r['agent_status'] or '-'} | {truth} | {r['score']:.2f} | "
                    f"{r['steps']} | {r['llm_calls']} | {r['tokens']} | {r['retries']} | "
                    f"{r['approvals_approved']}/{r['approvals_requested']} | {r['questions']} | {_model_cell(r)} | {r['duration_s']:.0f}s |")
    lines = [
        f"### Eval results ({meta['started_at']}, {meta['provider']} / {meta['model']}, sim date {meta.get('sim_today', '?')}"
        + (f", sim instance {meta['instance_id']}" if meta.get("instance_id") else "")
        + (f", {meta['parallel']} in parallel, one sim tenant per scenario" if meta.get("parallel", 1) > 1 else "") + ")", "",
        "Success is judged by deterministic checks against the simulated company's database, not by the agent's claims "
        "and not by an LLM. `Agent said` is the final status the agent reported; `Ground truth` is the checker's verdict.", "",
        *head, *rows, "",
        f"**{s['passed']}/{s['graded']} passed**, {s['false_successes']} false success(es), mean score {s['mean_score']:.2f}, "
        f"{s['llm_calls']} LLM calls, {s['tokens']} tokens, {s['duration_s']:.0f}s total."
        + (f" **{s['env_errors']} environment error(s)**, not graded." if s["env_errors"] else "")
        + (" Run was interrupted; results are partial." if meta.get("interrupted") else "")
        + (f" Run was aborted ({_md(meta['aborted'])}); results are partial." if meta.get("aborted") else ""), "",
    ]
    titles = {r["scenario"]: r["title"] for r in results}
    lines += ["Scenarios: " + "; ".join(f"`{k}` = {_md(v)}" for k, v in titles.items()) + ".", ""]
    env = [r for r in results if r["outcome"] == "env_error"]
    if env:
        lines += ["Environment errors (the sim's data differs from the seed formula, so these runs were not graded):", ""]
        for r in env:
            lines.append(f"- `{r['scenario']}` (instance `{r.get('instance_id') or '?'}`, sim date {r['sim_today']}): "
                         + _md(" | ".join(r.get("env_errors") or r["details"])))
        lines.append("")
    failed = [r for r in results if not r["passed"] and r["outcome"] != "env_error"]
    if failed:
        lines += ["Failures:", ""]
        for r in failed:
            bad = [d for d in r["details"] if d.startswith("FAIL")] or [r["error"] or "no details"]
            lines.append(f"- `{r['scenario']}`: " + _md(" | ".join(bad)))
        lines.append("")
    return "\n".join(lines)


def write_results(results: list[dict], meta: dict, results_dir: Path = RESULTS_DIR) -> tuple[Path, Path]:
    """Write ``<timestamp>.json`` and ``latest.md``; returns both paths."""
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = meta["started_at_file"]
    json_path, md_path = results_dir / f"{stamp}.json", results_dir / "latest.md"
    json_path.write_text(json.dumps({"meta": meta, "summary": summarize(results), "results": results}, indent=2,
                                    ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(results, meta), encoding="utf-8")
    return json_path, md_path


# --------------------------------------------------------------------------- main


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m evals.run", description="Run the evaluation scenarios against the sim.")
    p.add_argument("--only", help="Comma-separated scenario ids to run (default: all).")
    p.add_argument("--list", action="store_true", help="List scenarios and exit.")
    p.add_argument("--repeat", type=int, default=1, help="Run each scenario N times (default 1).")
    p.add_argument("--parallel", type=int, default=1, metavar="N",
                   help="Run up to N scenarios at the same time (default 1). Implies --tenant-per-scenario.")
    p.add_argument("--tenant-per-scenario", action=argparse.BooleanOptionalAction, default=None,
                   help="Give every scenario its own freshly provisioned sim tenant (default: on when --parallel > 1, "
                        "else off, which runs sequentially on the default tenant).")
    p.add_argument("--headed", action="store_true", help="Show the browser window.")
    p.add_argument("--provider", choices=["gemini", "ollama"], help="LLM provider (default: LLM_PROVIDER from .env).")
    p.add_argument("--sleep-between", type=float, default=0.0, help="Seconds to wait between runs (free-tier friendly).")
    p.add_argument("--sim-url", help="Sim base URL (default: SIM_BASE_URL from .env).")
    p.add_argument("--scenarios-dir", help="Directory with scenario YAML files.")
    return p.parse_args(argv)


def select(scenarios: list[Scenario], only: str | None) -> list[Scenario]:
    if not only:
        return scenarios
    wanted = [w.strip() for w in only.split(",") if w.strip()]
    known = {s.id: s for s in scenarios}
    missing = [w for w in wanted if w not in known]
    if missing:
        raise ScenarioError(f"unknown scenario id(s) {missing}; available: {sorted(known)}")
    return [known[w] for w in wanted]


async def amain(args: argparse.Namespace) -> int:
    try:
        scenarios = select(load_scenarios(args.scenarios_dir) if args.scenarios_dir else load_scenarios(), args.only)
    except ScenarioError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2
    if args.list:
        for s in scenarios:
            print(f"{s.id:26} {s.title}  [chaos: {', '.join(s.chaos) or '-'}]")
        return 0

    parallel = max(1, args.parallel)
    per_tenant = parallel > 1 if args.tenant_per_scenario is None else args.tenant_per_scenario
    if parallel > 1 and not per_tenant:
        print("error: --parallel needs --tenant-per-scenario (concurrent scenarios cannot share one tenant's data).", file=sys.stderr)
        return 2
    overrides: dict = {"HEADLESS": not args.headed, "APPROVAL_MODE": "confirm_writes"}
    overrides["BROWSER_MAX_CONTEXTS"] = max(get_settings().BROWSER_MAX_CONTEXTS, parallel)  # one context per concurrent run
    if args.provider:
        overrides["LLM_PROVIDER"] = args.provider
    if args.sim_url:
        overrides["SIM_BASE_URL"] = args.sim_url
    settings = get_settings().model_copy(update=overrides)
    if settings.SIM_ADMIN_TOKEN:  # the helpers and a sim started below read it from the environment
        os.environ.setdefault("SIM_ADMIN_TOKEN", settings.SIM_ADMIN_TOKEN)
    if settings.LLM_PROVIDER == "gemini" and not settings.GEMINI_API_KEY:
        print("error: GEMINI_API_KEY is not set. Put it in .env or the environment (free key: https://aistudio.google.com/apikey), "
              "or run with --provider ollama.", file=sys.stderr)
        return 2
    try:
        make_llm(settings)  # fail early on a bad provider configuration
    except LLMError as err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    model = {"gemini": " > ".join(settings.gemini_models), "ollama": settings.OLLAMA_MODEL}.get(settings.LLM_PROVIDER, "scripted")
    now = datetime.now()
    meta = {"started_at": now.strftime("%Y-%m-%d %H:%M"), "started_at_file": now.strftime("%Y%m%d-%H%M%S"),
            "provider": settings.LLM_PROVIDER, "model": model, "sim_url": settings.SIM_BASE_URL,
            "repeat": args.repeat, "parallel": parallel, "tenant_per_scenario": per_tenant, "interrupted": False}
    by_index: dict[int, dict] = {}
    setup_problem = False
    plan = [(s, i) for s in scenarios for i in range(1, max(1, args.repeat) + 1)]
    slots = asyncio.Semaphore(parallel)

    async def run_one(n: int, scenario: Scenario, i: int) -> None:
        async with slots:
            if n > 1 and args.sleep_between > 0:
                await asyncio.sleep(args.sleep_between)
            print(f"[evals] ({n}/{len(plan)}) {scenario.id}" + (f" #{i}" if args.repeat > 1 else "") + f" - {scenario.title}")
            record = await run_scenario(scenario, settings, settings.SIM_BASE_URL, repeat=i, per_tenant=per_tenant,
                                        instance_id=meta.get("instance_id"))
            record["show_repeat"] = args.repeat > 1
            by_index[n] = record
            who = f"{scenario.id}" + (f" #{i}" if args.repeat > 1 else "") + ": " if parallel > 1 else ""  # results interleave
            print(f"        -> {who}{OUTCOME_LABEL.get(record['outcome'])} (agent said {record['agent_status']}, "
                  f"score {record['score']:.2f}, {record['duration_s']:.0f}s)")

    try:
        with running_sim(settings.SIM_BASE_URL) as sim_identity:
            meta["instance_id"], meta["sim_today"] = sim_identity["instance_id"], sim_identity.get("sim_today")
            tasks = [asyncio.create_task(run_one(n, scenario, i)) for n, (scenario, i) in enumerate(plan, start=1)]
            try:
                await asyncio.gather(*tasks)
            finally:  # a failure or Ctrl+C stops the others too, and lets them clean up their tenants
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (asyncio.CancelledError, KeyboardInterrupt):
        meta["interrupted"] = True
        print("\n[evals] interrupted; writing partial results")
    except SimSetupError as err:  # a stale or second sim: the run cannot be trusted; what finished before is kept as partial
        print(f"error: {err}", file=sys.stderr)
        meta["aborted"] = str(err).split(". ")[0]
        setup_problem = True
    except (RuntimeError, httpx.HTTPError) as err:
        print(f"error: {err}", file=sys.stderr)
        if not by_index:
            return 2
    results = [by_index[n] for n in sorted(by_index)]
    if results:
        meta["sim_today"] = results[-1]["sim_today"]
        print("\n" + render_text(results))
        json_path, md_path = write_results(results, meta)
        print(f"\nWrote {json_path}\nWrote {md_path}")
    if meta["interrupted"]:
        return 130
    if setup_problem or any(r["outcome"] == "env_error" for r in results):
        return 2
    return 0 if results and all(r["passed"] for r in results) else 1


async def _amain_then_close_browsers(args: argparse.Namespace) -> int:
    try:
        return await amain(args)
    finally:  # the scenarios share one Chromium (see worker/tools/browser_pool.py); stop it before the loop ends
        try:
            from worker.tools.browser_pool import shutdown_pool

            await shutdown_pool()
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> None:
    try:
        code = asyncio.run(_amain_then_close_browsers(parse_args(argv)))
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
