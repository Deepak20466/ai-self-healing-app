"""`selfheal up`/`down`/`status`: start/stop the 4 pods as local subprocesses
and report their health -- the terminal equivalent of the old
`start_public_demo.ps1`/`honcho start`, minus any web UI.

Every pod is started bound to 127.0.0.1 only (see Procfile). `--public`
additionally starts exactly one Cloudflare quick tunnel for the sentinel
webhook (port 8002) -- never the healer API -- matching CLAUDE.md's
"Terminal-only v1.0" guardrail.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

from cli.config import (
    config_dir,
    load_pod_pids,
    load_tunnel_pid,
    save_pod_pids,
    save_tunnel_pid,
)
from core.config import settings
from mcp_server.sandbox import REPO_ROOT

_VENV_PY = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
_VENV_UVICORN = REPO_ROOT / ".venv" / "Scripts" / "uvicorn.exe"

PODS: dict[str, tuple[list[str], str | None]] = {
    "app": (
        [str(_VENV_UVICORN), "apps.target_app.main:app", "--host", "127.0.0.1", "--port", "8001"],
        f"http://127.0.0.1:{settings.app_port}/healthz",
    ),
    "sentinel": (
        [str(_VENV_UVICORN), "sentinel.app:app", "--host", "127.0.0.1", "--port", "8002"],
        f"http://127.0.0.1:{settings.sentinel_port}/healthz",
    ),
    "mcp": (
        [str(_VENV_PY), "-m", "mcp_server.http_main"],
        None,  # mcp-pod has no /healthz -- see CLAUDE.md Phase 7 "Ambiguities resolved"
    ),
    "healer": (
        [
            str(_VENV_UVICORN),
            "healer.app:asgi_app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
        ],
        f"http://127.0.0.1:{settings.healer_port}/healthz",
    ),
}


def _log_path(name: str) -> Path:
    logs = config_dir() / "logs"
    logs.mkdir(exist_ok=True)
    return logs / f"{name}.log"


async def _healthz(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url)
            return resp.status_code == 200
    except httpx.HTTPError:
        return False


async def _mcp_reachable() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"http://127.0.0.1:{settings.mcp_port}/mcp")
            return resp.status_code < 500
    except httpx.HTTPError:
        return False


async def pod_is_up(name: str) -> bool:
    if name == "mcp":
        return await _mcp_reachable()
    _, url = PODS[name]
    assert url is not None
    return await _healthz(url)


def _spawn(name: str, cmd: list[str]) -> int:
    log = _log_path(name)
    with log.open("wb") as fh:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=REPO_ROOT,
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    return proc.pid


async def start_pods() -> dict[str, str]:
    """Starts every pod that isn't already up. Returns {name: 'started'|'already running'}."""
    results: dict[str, str] = {}
    pids = load_pod_pids()
    for name, (cmd, _url) in PODS.items():
        if await pod_is_up(name):
            results[name] = "already running"
            continue
        pids[name] = _spawn(name, cmd)
        results[name] = "started"
    save_pod_pids(pids)

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if all([await pod_is_up(n) for n in PODS]):
            break
        await asyncio.sleep(1)
    return results


def _pid_alive(pid: int) -> bool:
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except ImportError:  # pragma: no cover - psutil is a base dependency
        return True


def _terminate(pid: int) -> None:
    try:
        import psutil

        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except psutil.TimeoutExpired:
            proc.kill()
    except Exception:
        pass


def stop_pods() -> list[str]:
    pids = load_pod_pids()
    stopped = []
    for name, pid in pids.items():
        if _pid_alive(pid):
            _terminate(pid)
            stopped.append(name)
    save_pod_pids({})
    tunnel_pid = load_tunnel_pid()
    if tunnel_pid is not None and _pid_alive(tunnel_pid):
        _terminate(tunnel_pid)
        stopped.append("tunnel")
    save_tunnel_pid(None)
    return stopped


_TUNNEL_URL_RE = re.compile(r"https://[a-zA-Z0-9.-]+\.trycloudflare\.com")


def start_webhook_tunnel() -> str | None:
    """Starts a `cloudflared` quick tunnel for the sentinel webhook (port
    8002) ONLY -- never the healer API. Returns the public URL, or None if
    `cloudflared` isn't installed or no URL appeared within the timeout.
    """
    log = _log_path("tunnel")
    with log.open("wb") as fh:
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        try:
            proc = subprocess.Popen(  # noqa: S603, S607
                ["cloudflared", "tunnel", "--protocol", "http2", "--url", "http://localhost:8002"],
                cwd=REPO_ROOT,
                stdout=fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except FileNotFoundError:
            return None
    save_tunnel_pid(proc.pid)

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        text = log.read_text(errors="ignore")
        match = _TUNNEL_URL_RE.search(text)
        if match:
            return match.group(0)
        time.sleep(1)
    return None
