"""The evals must know which simulator they talk to: a stale or second one on the same port aborts the run (exit code 2).

Fake simulators are tiny stdlib HTTP servers on 127.0.0.1 and ::1 (ports 8901-8999) that answer ``/healthz`` with an
instance id; one test starts the real simulator as a subprocess through ``evals.run.running_sim``.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from evals import instance
from evals import run as eval_run
from evals.instance import SimSetupError, ensure_instance, find_running_sim, probe
from worker.config import Settings

PORTS = range(8901, 9000)


def _ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as sock:
            sock.bind(("::1", 0))
        return True
    except OSError:
        return False


IPV6 = _ipv6_loopback()
needs_ipv6 = pytest.mark.skipif(not IPV6, reason="no IPv6 loopback address on this machine")


def free_port() -> int:
    """A port in 8901-8999 that is free on 127.0.0.1 and (when there is one) on ::1."""
    for port in PORTS:
        try:
            for family, host in [(socket.AF_INET, "127.0.0.1"), *([(socket.AF_INET6, "::1")] if IPV6 else [])]:
                with socket.socket(family) as sock:
                    sock.bind((host, port))
            return port
        except OSError:
            continue
    pytest.skip("no free port in 8901-8999")


class _V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


@contextmanager
def serving(host: str, port: int, instance_id: str | None = "A", sim_today: str = "2026-10-16", status: int = 200):
    """A fake simulator: ``/healthz`` answers with ``instance_id`` (no id at all when ``None``)."""
    body = {"status": "ok", "sim_today": sim_today, "data_dir": "C:/fake/data", "pid": 4242, "started_at": "2026-10-16T08:00:00+00:00"}
    if instance_id:
        body["instance_id"] = instance_id

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            payload = json.dumps(body if self.path == "/healthz" else {"detail": "Not Found"}).encode()
            self.send_response(status if self.path == "/healthz" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = (_V6Server if ":" in host else ThreadingHTTPServer)((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --------------------------------------------------------------------------- probes and checks


def test_probe_tells_refused_sim_and_foreign_servers_apart():
    port = free_port()
    assert probe("127.0.0.1", port).state == "refused"
    with serving("127.0.0.1", port, "A", sim_today="2026-10-16"):
        found = probe("127.0.0.1", port)
        assert found.state == "sim" and found.instance_id == "A" and found.info["sim_today"] == "2026-10-16"
        assert "pid 4242" in found.describe() and "2026-10-16" in found.describe()
    with serving("127.0.0.1", port, None):  # answers /healthz, but without an instance id (an older sim, or another program)
        assert probe("127.0.0.1", port).state == "foreign"
    with serving("127.0.0.1", port, "A", status=503):
        assert probe("127.0.0.1", port).state == "foreign"


def test_local_addresses_cover_both_loopback_addresses():
    addresses = instance.local_addresses("localhost", 8901)
    assert "127.0.0.1" in addresses and "::1" in addresses
    assert instance.local_addresses("127.0.0.1", 8901).count("127.0.0.1") == 1
    assert instance.local_addresses("sim.example.invalid", 8901) == []


def test_nothing_running_finds_no_sim():
    port = free_port()
    assert find_running_sim(f"http://localhost:{port}") is None


def test_one_identifiable_sim_is_adopted_and_refusing_addresses_are_fine():
    port = free_port()
    with serving("127.0.0.1", port, "A", sim_today="2026-10-19"):
        found = find_running_sim(f"http://localhost:{port}")
        assert found["instance_id"] == "A" and found["sim_today"] == "2026-10-19"
        assert ensure_instance(f"http://localhost:{port}", "A")["instance_id"] == "A"  # ::1 refuses, which is fine
        with pytest.raises(SimSetupError, match="instance A"):
            ensure_instance(f"http://localhost:{port}", "B")


@needs_ipv6
def test_same_instance_on_both_loopback_addresses_is_fine():
    port = free_port()
    with serving("127.0.0.1", port, "A"), serving("::1", port, "A"):
        assert ensure_instance(f"http://localhost:{port}", "A")["instance_id"] == "A"


@needs_ipv6
def test_a_second_simulator_on_the_other_address_fails_the_identity_check():
    """The evidence case: the harness's client reaches one process, the browser (via 'localhost' -> ::1) another."""
    port = free_port()
    with serving("127.0.0.1", port, "A", sim_today="2026-10-19"), serving("::1", port, "B", sim_today="2026-10-16"):
        with pytest.raises(SimSetupError) as err:
            ensure_instance(f"http://127.0.0.1:{port}", "A")
        message = str(err.value)
        assert f"[::1]:{port}" in message and "instance B" in message and "sim_today 2026-10-16" in message
        assert "pid 4242" in message and "this run uses instance A" in message
        assert "netstat -ano" in message and f":{port}" in message  # how to find and stop it
        # the same holds when the harness itself reaches the other process through the 'localhost' name
        with pytest.raises(SimSetupError):
            ensure_instance(f"http://localhost:{port}", "A")
        with pytest.raises(SimSetupError, match="Two different simulators"):
            find_running_sim(f"http://localhost:{port}")


@needs_ipv6
def test_an_unidentifiable_server_blocks_the_start_of_a_new_sim(monkeypatch):
    port = free_port()
    started = []
    monkeypatch.setattr(eval_run.subprocess, "Popen", lambda *a, **kw: started.append(a))
    with serving("::1", port, None):
        with pytest.raises(SimSetupError) as err, eval_run.running_sim(f"http://localhost:{port}"):
            pass
    assert f"[::1]:{port}" in str(err.value) and "not an identifiable simulator" in str(err.value)
    assert "netstat -ano" in str(err.value) and not started  # nothing was started next to it


def test_how_to_stop_names_the_listening_process():
    port = free_port()
    with serving("127.0.0.1", port, "A"):
        hint, listing = instance.how_to_stop(port), instance.listeners(port)
    assert f"findstr :{port}" in hint and "taskkill" in hint
    assert listing, "the listening process should be found (psutil or netstat)"
    assert f"pid {os.getpid()}" in hint  # this test process is the listener


def test_a_sim_that_vanishes_after_the_start_fails_the_check():
    port = free_port()
    with pytest.raises(SimSetupError, match="no longer answers"):
        ensure_instance(f"http://localhost:{port}", "A")


# --------------------------------------------------------------------------- the CLI: exit code 2


def _record(sid: str) -> dict:
    return {"scenario": sid, "title": sid, "repeat": 1, "chaos": [], "run_id": "r", "tenant": "default", "instance_id": "A",
            "env_errors": [], "agent_status": "succeeded", "outcome": "pass", "passed": True, "score": 1.0, "details": ["PASS: ok"],
            "agent_summary": "", "steps": 1, "llm_calls": 1, "tokens": 1, "retries": 0, "tool_failures": 0, "llm_retries": 0,
            "approvals_requested": 0, "approvals_approved": 0, "questions": 0, "approval_log": [], "question_log": [],
            "duration_s": 0.1, "error": "", "sim_today": "2026-10-19"}


@pytest.fixture()
def cli(monkeypatch, tmp_path):
    monkeypatch.setattr(eval_run, "get_settings", lambda: Settings(LLM_PROVIDER="fake", RUNS_DIR=str(tmp_path), _env_file=None))
    monkeypatch.setattr(eval_run, "make_llm", lambda _settings: None)
    written: list[tuple[list[dict], dict]] = []
    monkeypatch.setattr(eval_run, "write_results", lambda results, meta: written.append((results, meta)) or (tmp_path / "r.json", tmp_path / "latest.md"))

    def run(port: int, only: str = "invoice_happy,deny_write") -> int:
        args = eval_run.parse_args(["--sim-url", f"http://localhost:{port}", "--only", only])
        return asyncio.run(eval_run.amain(args))

    return run, written


@needs_ipv6
def test_cli_exits_with_2_when_two_different_sims_answer_at_startup(cli, capsys):
    run, written = cli
    port = free_port()
    with serving("127.0.0.1", port, "A", sim_today="2026-10-19"), serving("::1", port, "B", sim_today="2026-10-16"):
        assert run(port) == 2
    err = capsys.readouterr().err
    assert "Two different simulators" in err and "sim_today 2026-10-16" in err and f"[::1]:{port}" in err
    assert written == []  # nothing ran


@needs_ipv6
def test_cli_aborts_with_2_when_a_second_sim_appears_between_scenarios(cli, monkeypatch, capsys):
    run, written = cli
    port = free_port()
    calls = []

    async def fake_run_scenario(scenario, settings, base_url, repeat=1, per_tenant=False, instance_id=None):
        calls.append((scenario.id, instance_id))
        await eval_run.verify_instance(base_url, instance_id)  # the real check, as run_scenario does first
        if len(calls) == 1:
            return _record(scenario.id)
        with serving("::1", port, "B", sim_today="2026-10-16"):  # a stale sim comes up while the agent works
            await eval_run.verify_instance(base_url, instance_id)  # ... and is caught right before grading
        raise AssertionError("the identity check should have raised")

    monkeypatch.setattr(eval_run, "run_scenario", fake_run_scenario)
    with serving("127.0.0.1", port, "A", sim_today="2026-10-19"):
        assert run(port) == 2
    assert calls == [("invoice_happy", "A"), ("deny_write", "A")]
    err = capsys.readouterr().err
    assert "different simulator" in err and "instance B" in err and "sim_today 2026-10-16" in err
    (results, meta), = written  # the scenario that finished before the abort is kept, marked partial
    assert [r["scenario"] for r in results] == ["invoice_happy"]
    assert meta["instance_id"] == "A" and "different simulator" in meta["aborted"]


def test_cli_passes_the_adopted_instance_id_to_every_scenario(cli, monkeypatch):
    run, written = cli
    port = free_port()
    seen = []

    async def fake_run_scenario(scenario, settings, base_url, repeat=1, per_tenant=False, instance_id=None):
        seen.append(instance_id)
        return _record(scenario.id)

    monkeypatch.setattr(eval_run, "run_scenario", fake_run_scenario)
    with serving("127.0.0.1", port, "A", sim_today="2026-10-19"):
        assert run(port) == 0
    assert seen == ["A", "A"] and written[0][1]["instance_id"] == "A"


def test_cli_exits_with_2_when_a_scenario_ends_in_an_environment_error(cli, monkeypatch):
    run, written = cli
    port = free_port()

    async def fake_run_scenario(scenario, settings, base_url, repeat=1, per_tenant=False, instance_id=None):
        return {**_record(scenario.id), "outcome": "env_error", "passed": False, "score": 0.0,
                "env_errors": ["invoice INI-20502: due_date is '2026-11-01' in the sim's data, the seed formula gives '2026-11-04'"],
                "details": ["ENV ERROR: invoice INI-20502 ..."]}

    monkeypatch.setattr(eval_run, "run_scenario", fake_run_scenario)
    with serving("127.0.0.1", port, "A"):
        assert run(port, only="reply_payment_date") == 2
    assert written[0][0][0]["outcome"] == "env_error"


# --------------------------------------------------------------------------- the real simulator, as a subprocess


def test_live_simulator_started_by_the_harness_has_a_fresh_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SIM_INSTANCE_ID", raising=False)
    monkeypatch.delenv("SIM_ADMIN_TOKEN", raising=False)
    port = free_port()
    url = f"http://localhost:{port}"
    with eval_run.running_sim(url) as info:
        health = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=5).json()
        assert info["instance_id"] == health["instance_id"] and len(info["instance_id"]) == 12
        assert health["data_dir"] == str(tmp_path) and health["pid"] and health["sim_today"] == info["sim_today"]
        state = httpx.get(f"http://127.0.0.1:{port}/admin/state", timeout=10).json()
        assert state["instance_id"] == info["instance_id"] and len(state["invoices"]) == 14 and len(state["inbox"]) == 8
        assert ensure_instance(url, info["instance_id"])["pid"] == health["pid"]
        with pytest.raises(SimSetupError, match="a different simulator"):
            ensure_instance(url, "someone-else")
        if IPV6:
            with serving("::1", port, "intruder", sim_today="2026-10-16"), pytest.raises(SimSetupError, match=r"\[::1\]"):
                ensure_instance(url, info["instance_id"])
        assert find_running_sim(url)["instance_id"] == info["instance_id"]  # a second harness would adopt, not duplicate it
    assert probe("127.0.0.1", port).state == "refused"  # and it was stopped again
