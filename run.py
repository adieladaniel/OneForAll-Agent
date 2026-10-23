"""Start the simulated company (port 8001) and the worker server + dashboard (port 8000) together.

Usage: ``python run.py [--open] [--workers N]``. Ctrl+C stops everything cleanly.

In database mode (``DATABASE_URL`` set) the API process only queues runs, so this also starts N worker processes
(``python -m worker.jobs``, default 1) that claim and execute them.
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
SIM_PORT, WORKER_PORT = 8001, 8000
WORKER_URL = f"http://127.0.0.1:{WORKER_PORT}"
SIM_URL = f"http://127.0.0.1:{SIM_PORT}"
IS_WINDOWS = os.name == "nt"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)  # show progress immediately when output is piped


def port_free(port: int) -> bool:
    """True when nothing is listening on 127.0.0.1:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def spawn(name: str, module: str, *args: str) -> subprocess.Popen:
    """Start ``python -m module`` in its own process group so Ctrl+C can be forwarded deliberately."""
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    print(f"[run] starting {name}: python -m {module} {' '.join(args)}".rstrip())
    return subprocess.Popen([sys.executable, "-m", module, *args], cwd=ROOT, creationflags=flags)


def database_mode() -> bool:
    """Will the API run in database mode (users and a job queue in PostgreSQL)?"""
    sys.path.insert(0, str(ROOT))
    from worker.config import get_settings

    return get_settings().db_auth


def wait_healthy(url: str, name: str, proc: subprocess.Popen, timeout: float = 45.0) -> None:
    """Block until ``url`` answers 200, the process dies, or ``timeout`` passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"{name} exited early with code {proc.returncode}")
        try:
            if httpx.get(url, timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.4)
    raise RuntimeError(f"{name} did not become healthy within {timeout:.0f}s ({url})")


def stop(proc: subprocess.Popen, name: str, grace: float = 8.0) -> None:
    """Ask the process to shut down gracefully, then terminate its whole tree if it lingers."""
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # uvicorn treats SIGBREAK like SIGINT
        else:
            proc.send_signal(signal.SIGINT)
        proc.wait(timeout=grace)
        return
    except (subprocess.TimeoutExpired, OSError):
        pass
    print(f"[run] {name} did not stop in {grace:.0f}s, killing it")
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the simulated company and the OneForAll Agent dashboard.")
    parser.add_argument("--open", action="store_true", help="Open the dashboard in the default browser.")
    parser.add_argument("--workers", type=int, default=1, metavar="N",
                        help="Worker processes that execute queued runs (database mode only; default 1).")
    args = parser.parse_args()

    def _interrupt(signum, frame):  # Ctrl+Break / SIGTERM take the same clean-shutdown path as Ctrl+C
        raise KeyboardInterrupt

    for name in ("SIGBREAK", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _interrupt)

    busy = [p for p in (SIM_PORT, WORKER_PORT) if not port_free(p)]
    if busy:
        print(f"[run] port(s) {', '.join(map(str, busy))} already in use. Stop whatever is listening there first.")
        return 1

    procs: list[tuple[str, subprocess.Popen]] = []
    try:
        sim = spawn("simulated company", "company_sim")
        procs.append(("company_sim", sim))
        wait_healthy(f"{SIM_URL}/healthz", "company_sim", sim)
        print(f"[run] simulated company ready at {SIM_URL}  (mail, vendor portal, ERP)")
        if database_mode():
            # Each org has its own simulated company. Its data can be lost (a wiped data dir, a fresh checkout), so make
            # sure every org's environment exists and its logins in the vault match, as deploy/start.py does.
            print("[run] checking every organization's simulated company: python -m worker.provisioning resync")
            synced = subprocess.run([sys.executable, "-m", "worker.provisioning", "resync"], cwd=ROOT, timeout=120)
            if synced.returncode != 0:
                print("[run] warning: resync failed; runs may use the default simulated company. "
                      "Retry with: python -m worker.provisioning resync")

        worker = spawn("worker server", "worker.server")
        procs.append(("worker", worker))
        wait_healthy(f"{WORKER_URL}/api/health", "worker server", worker)
        if database_mode():
            for number in range(1, max(args.workers, 0) + 1):
                name = f"job worker {number}"
                procs.append((name, spawn(name, "worker.jobs", "--worker-id", f"local-{number}")))
            print(f"[run] {max(args.workers, 0)} job worker(s) started: the API only queues runs, the workers execute them")
        print(f"\n[run] OneForAll Agent dashboard: {WORKER_URL}\n[run] press Ctrl+C to stop everything\n")
        if args.open:
            webbrowser.open(WORKER_URL)

        while True:
            for name, proc in procs:
                if proc.poll() is not None:
                    print(f"[run] {name} exited with code {proc.returncode}; shutting down")
                    return 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[run] stopping...")
        return 0
    except RuntimeError as err:
        print(f"[run] {err}")
        return 1
    finally:
        for name, proc in reversed(procs):
            stop(proc, name)
        print("[run] stopped")


if __name__ == "__main__":
    sys.exit(main())
