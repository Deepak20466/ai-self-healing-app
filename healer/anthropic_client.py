"""Factory for the real Anthropic client the healer talks to in production.

Tests never call `build_anthropic_client()` — they inject a small hand-built
fake satisfying `AnthropicClientLike` instead of constructing the real SDK
class (see CLAUDE.md "Anthropic and GitHub are always mocked in tests").
Scripting the exact multi-turn tool-use wire format through `respx` is far
more work than it's worth for the handful of deterministic scenarios
`runtime_agent.py`'s tests need, so `runtime_agent.run_heal_job` depends only
on this module's narrow structural Protocol, not on `anthropic.AsyncAnthropic`
itself.

`anthropic` itself is only imported inside `build_anthropic_client()`, not at
module level: it's an optional extra (`pip install .[api]`, see pyproject.toml)
needed only for API mode (`USE_CLAUDE_CODE=false`). Free mode (the default)
never calls this function, so a free-mode install shouldn't need the package
at all.
"""

from __future__ import annotations

from typing import Any, Protocol, cast

from core.config import settings


class AnthropicMessagesLike(Protocol):
    """The one method `healer/runtime_agent.py` needs from `client.messages`."""

    async def create(self, **kwargs: Any) -> Any: ...


class AnthropicClientLike(Protocol):
    """Structural type for the real client or a test fake.

    `runtime_agent.py` only ever calls `client.messages.create(...)`, so this
    is the entire surface it depends on.
    """

    @property
    def messages(self) -> AnthropicMessagesLike: ...


def build_anthropic_client() -> AnthropicClientLike:
    """Construct the real Anthropic client. Only called by `healer/worker.py`
    in API mode (`USE_CLAUDE_CODE=false`)."""
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not configured (required in API mode, i.e. USE_CLAUDE_CODE=false)"
        )
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "the `anthropic` package is not installed; run `pip install .[api]` to use "
            "API mode (USE_CLAUDE_CODE=false)"
        ) from exc

    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key,
        base_url=settings.anthropic_base_url,
    )
    return cast(AnthropicClientLike, client)
