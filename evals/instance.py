"""Which simulator is this? Guards the evals against a stale or second simulator answering on the same port.

The sim reports an instance id (random per process start, or ``SIM_INSTANCE_ID``) on ``/healthz`` together with its
``sim_today`` and data dir. "localhost" can resolve to both 127.0.0.1 and ::1, and a browser may reach a different
process than the harness's HTTP client does, so ``scan`` asks every address "localhost" stands for, plus the base URL
itself. ``find_running_sim`` decides what to do before the harness starts its own sim, and ``ensure_instance`` is run
before every scenario and again right before grading: any address that answers with another instance id (or that is not a
simulator at all) raises ``SimSetupError``, which the harness turns into a setup problem (exit code 2).
"""
from __future__ import annotations

import os
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

PROBE_TIMEOUT_S = 3.0
CONNECT_TIMEOUT_S = 1.0  # a loopback listener accepts at once; Windows takes ~2 s to report a refused connection
LOOPBACK = ("127.0.0.1", "::1")
LOCAL_HOSTS = ("localhost", *LOOPBACK)


class SimSetupError(RuntimeError):
    """The simulator environment is not what the evals need (another or unidentifiable server on the sim's port)."""


@dataclass(frozen=True)
class Probe:
    """What answered at one address: ``state`` is ``refused`` (nothing listens), ``sim`` (a simulator that reports its
    identity in ``info``) or ``foreign`` (something answered, but it is not an identifiable simulator)."""

    address: str
    url: str
    state: str
    info: dict = field(default_factory=dict)
    detail: str = ""

    @property
    def instance_id(self) -> str:
        return str(self.info.get("instance_id") or "")

    def describe(self) -> str:
        """``instance abc (pid 4242, sim_today 2026-10-16, started ..., data dir ...)`` for messages."""
        bits = [f"pid {self.info['pid']}" if self.info.get("pid") else "", f"sim_today {self.info.get('sim_today', '?')}",
                f"started {self.info['started_at']}" if self.info.get("started_at") else "",
                f"data dir {self.info['data_dir']}" if self.info.get("data_dir") else ""]
        return f"instance {self.instance_id} ({', '.join(b for b in bits if b)})"


def _netloc(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def probe(host: str, port: int, scheme: str = "http", timeout: float = PROBE_TIMEOUT_S) -> Probe:
    """Ask ``/healthz`` at one host or address. Never raises. A connection that is refused, or not accepted within
    ``CONNECT_TIMEOUT_S``, means nothing listens there (``refused``)."""
    url = f"{scheme}://{_netloc(host, port)}"
    try:
        resp = httpx.get(url + "/healthz", timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT_S), trust_env=False)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return Probe(host, url, "refused")
    except httpx.HTTPError as exc:
        return Probe(host, url, "foreign", detail=f"{type(exc).__name__}: it accepted the connection but did not answer HTTP")
    try:
        body = resp.json()
    except ValueError:
        body = None
    if resp.status_code == 200 and isinstance(body, dict) and body.get("instance_id"):
        return Probe(host, url, "sim", info=body)
    return Probe(host, url, "foreign", detail=f"HTTP {resp.status_code} without a simulator instance id "
                 "(an older simulator, or another program)")


def local_addresses(host: str, port: int) -> list[str]:
    """Every address ``host`` stands for. For a local host: whatever ``socket.getaddrinfo`` gives for it and for
    "localhost", plus both loopback addresses, so an IPv4-only or IPv6-only listener cannot hide."""
    names = {host, "localhost"} if host in LOCAL_HOSTS else {host}
    found: list[str] = []
    for name in sorted(names):
        try:
            for family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(name, port, type=socket.SOCK_STREAM):
                if family in (socket.AF_INET, socket.AF_INET6):
                    found.append(str(sockaddr[0]).split("%")[0])
        except socket.gaierror:
            continue
    if host in LOCAL_HOSTS:
        found += LOOPBACK
    return list(dict.fromkeys(found))


def scan(base_url: str) -> list[Probe]:
    """Probe the base URL's own host name first (what the harness's HTTP client uses), then every address behind it."""
    parsed = urlparse(base_url)
    host, port, scheme = parsed.hostname or "localhost", parsed.port or (443 if parsed.scheme == "https" else 80), parsed.scheme or "http"
    hosts = [host, *(addr for addr in local_addresses(host, port) if addr != host)]
    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:  # refused connections can take a while to report
        return list(pool.map(lambda h: probe(h, port, scheme), hosts))


def listeners(port: int) -> list[str]:
    """Best effort: ``127.0.0.1:8001 (pid 1234 python.exe)`` for each process listening on ``port``."""
    out: list[str] = []
    try:
        import psutil

        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == "LISTEN" and conn.laddr and conn.laddr.port == port:
                try:
                    name = psutil.Process(conn.pid).name() if conn.pid else ""
                except psutil.Error:
                    name = ""
                out.append(f"{conn.laddr.ip}:{port} (pid {conn.pid} {name})".replace(" )", ")"))
    except Exception:  # noqa: BLE001 - psutil missing or not allowed to list sockets: try netstat
        pass
    if not out and os.name == "nt":
        try:
            text = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            text = ""
        for line in text.splitlines():
            cols = line.split()
            if len(cols) >= 5 and cols[3] == "LISTENING" and cols[1].rsplit(":", 1)[-1] == str(port):
                out.append(f"{cols[1]} (pid {cols[4]})")
    return list(dict.fromkeys(out))


def how_to_stop(port: int) -> str:
    found = listeners(port)
    where = f" Listening on port {port}: {'; '.join(found)}." if found else ""
    return (f"{where} Find the process with `netstat -ano | findstr :{port}` (Windows; the last column is the PID) or "
            f"`lsof -i :{port}` (Linux/macOS), stop that PID (`taskkill /PID <pid> /F` or `kill <pid>`), then run the evals again.")


def _by_address(probes: list[Probe]) -> list[Probe]:
    """The per-address probes first, the base URL's own host name last, so a message names the address that answered."""
    return [*probes[1:], probes[0]]


def _problem(p: Probe, expected_id: str | None) -> str | None:
    if p.state == "foreign":
        return f"something that is not an identifiable simulator answers at {p.url}: {p.detail}."
    if p.state == "sim" and expected_id and p.instance_id != expected_id:
        return (f"a different simulator answers at {p.url}: {p.describe()}, but this run uses instance {expected_id}. "
                "A browser or client that reaches it would see other data than the checkers grade.")
    return None


def ensure_instance(base_url: str, expected_id: str) -> dict:
    """Raise ``SimSetupError`` unless the server answering at ``base_url`` is instance ``expected_id`` and every address
    of "localhost" either refuses the connection or answers with the same id. Returns the base URL's identity info."""
    probes = scan(base_url)
    port = urlparse(base_url).port or 80
    for p in _by_address(probes):
        problem = _problem(p, expected_id)
        if problem:
            raise SimSetupError(f"Simulator identity check failed: {problem}{how_to_stop(port)}")
    primary = probes[0]
    if primary.state != "sim":
        raise SimSetupError(f"Simulator identity check failed: instance {expected_id} no longer answers at {base_url}.")
    return primary.info


def find_running_sim(base_url: str) -> dict | None:
    """Identity info of the one simulator already answering at ``base_url`` (and nowhere else), or ``None`` when nothing
    answers on any address. Raises ``SimSetupError`` when something unidentifiable answers, or two instances do."""
    probes = scan(base_url)
    port = urlparse(base_url).port or 80
    for p in _by_address(probes):
        if p.state == "foreign":
            raise SimSetupError(f"Cannot start the simulator: {_problem(p, None)}{how_to_stop(port)}")
    sims = [p for p in _by_address(probes) if p.state == "sim"]
    if not sims:
        return None
    first = sims[0]
    for other in sims[1:]:
        if other.instance_id != first.instance_id:
            raise SimSetupError(f"Two different simulators answer on port {port}: {first.describe()} at {first.url} and "
                                f"{other.describe()} at {other.url}.{how_to_stop(port)}")
    if probes[0].state != "sim":
        raise SimSetupError(f"A simulator ({first.describe()}) answers at {first.url} but nothing answers at {base_url}, "
                            f"so the agent's browser and the harness could reach different servers.{how_to_stop(port)}")
    return probes[0].info
