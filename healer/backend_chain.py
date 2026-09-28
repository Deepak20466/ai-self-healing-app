"""Terminal-only v1.0 Step 4: ordered AI fallback chains for fixes and chat.

Design: rather than rewriting `healer/runtime_agent.py`/`healer/ci_agent.py`
(the actual fix loop -- "don't change core healing logic"), `gemini_api`/
`groq_api` are implemented as thin `AnthropicClientLike` adapters
(`healer/api_adapters.py`) over those SAME unchanged modules. A "chain" is
just an ordered list of backend names; this module holds the parts that
don't belong to any one backend: cooldown state, error classification, and
picking which backend a given heal_job should run with next.

Cooldown state is in-memory only (reset on a healer-pod restart) -- the
same tradeoff `healer/chat_agent.py`'s `_pending` confirmation dict already
makes for similar "this is fine to lose, the user just retries" state; a
lost cooldown just means one extra real request to a backend that turns
out to still be limited, not a correctness problem.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from core.config import settings

logger = structlog.get_logger(__name__)

CLI_BACKENDS = frozenset({"claude_cli", "codex_cli", "gemini_cli"})
API_KEY_BACKENDS = frozenset({"gemini_api", "groq_api", "openrouter_api"})
ALL_BACKENDS = CLI_BACKENDS | API_KEY_BACKENDS | frozenset({"api"})


class BackendCooldownError(Exception):
    """Raised by an API-key backend adapter for a quota/rate-limit/auth
    failure -- classifies as "try the next backend", not a normal failure.
    """

    def __init__(self, message: str, *, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


_cooldowns: dict[str, datetime] = {}


def mark_cooldown(name: str, *, retry_after_seconds: int | None = None) -> None:
    seconds = retry_after_seconds or settings.backend_cooldown_default_seconds
    until = datetime.now(UTC) + timedelta(seconds=seconds)
    _cooldowns[name] = until
    logger.warning("backend_chain.cooldown", backend=name, until=until.isoformat())


def cooldown_until(name: str) -> datetime | None:
    until = _cooldowns.get(name)
    if until is None:
        return None
    if until <= datetime.now(UTC):
        del _cooldowns[name]
        return None
    return until


def is_cooling_down(name: str) -> bool:
    return cooldown_until(name) is not None


def clear_cooldowns() -> None:
    """Test-only escape hatch (also useful for a manual `selfheal` recovery)."""
    _cooldowns.clear()


def has_key_for(name: str) -> bool:
    """Whether this backend can even be attempted right now."""
    if name == "gemini_api":
        return bool(settings.gemini_api_key)
    if name == "groq_api":
        return bool(settings.groq_api_key)
    if name == "openrouter_api":
        return bool(settings.anthropic_api_key)
    if name == "api":
        return bool(settings.anthropic_api_key)
    if name in CLI_BACKENDS:
        return True  # CLI backends use interactive login, not an API key
    return False


@dataclass(frozen=True)
class BackendState:
    name: str
    role: str  # "fix" or "chat"
    has_key: bool
    cooling_down_until: datetime | None

    @property
    def active(self) -> bool:
        return self.has_key and self.cooling_down_until is None


def chain_states(chain: list[str], *, role: str) -> list[BackendState]:
    return [
        BackendState(
            name=name, role=role, has_key=has_key_for(name), cooling_down_until=cooldown_until(name)
        )
        for name in chain
    ]


def first_eligible(chain: list[str]) -> str | None:
    """The first backend in `chain` that has a key and isn't cooling down --
    "the chain returns to the first choice" once it recovers, by construction
    (this always starts scanning from index 0)."""
    for name in chain:
        if has_key_for(name) and not is_cooling_down(name):
            return name
    return None


def next_eligible_after(chain: list[str], tried: set[str]) -> str | None:
    """The first backend in `chain` not yet tried for this job and not
    cooling down -- used when resurrecting a job an earlier backend
    cooldown-paused (see `healer/worker.py`)."""
    for name in chain:
        if name in tried:
            continue
        if has_key_for(name) and not is_cooling_down(name):
            return name
    return None
