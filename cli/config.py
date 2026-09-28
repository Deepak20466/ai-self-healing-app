"""CLI-local state: session token and pod PIDs, both outside the repo.

The session token is never written to the repo or to `.env` -- it lives in
the OS user config dir (`platformdirs`), same principle CLAUDE.md's
Environment section already applies to secrets: never checked in, never
logged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

APP_NAME = "selfheal"


def config_dir() -> Path:
    d = Path(user_config_dir(APP_NAME))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path() -> Path:
    return config_dir() / "state.json"


def _read_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: dict[str, Any]) -> None:
    _state_path().write_text(json.dumps(state, indent=2), encoding="utf-8")


@dataclass
class Session:
    base_url: str
    token: str


def save_session(*, base_url: str, token: str) -> None:
    state = _read_state()
    state["base_url"] = base_url
    state["token"] = token
    _write_state(state)


def load_session() -> Session | None:
    state = _read_state()
    token = state.get("token")
    base_url = state.get("base_url")
    if not isinstance(token, str) or not isinstance(base_url, str):
        return None
    return Session(base_url=base_url, token=token)


def clear_session() -> None:
    state = _read_state()
    state.pop("token", None)
    state.pop("base_url", None)
    _write_state(state)


def save_pod_pids(pids: dict[str, int]) -> None:
    state = _read_state()
    state["pod_pids"] = pids
    _write_state(state)


def load_pod_pids() -> dict[str, int]:
    state = _read_state()
    pids = state.get("pod_pids")
    if not isinstance(pids, dict):
        return {}
    return {str(k): int(v) for k, v in pids.items()}


def clear_pod_pids() -> None:
    state = _read_state()
    state.pop("pod_pids", None)
    _write_state(state)


def save_tunnel_pid(pid: int | None) -> None:
    state = _read_state()
    if pid is None:
        state.pop("tunnel_pid", None)
    else:
        state["tunnel_pid"] = pid
    _write_state(state)


def load_tunnel_pid() -> int | None:
    state = _read_state()
    pid = state.get("tunnel_pid")
    return int(pid) if isinstance(pid, int) else None
