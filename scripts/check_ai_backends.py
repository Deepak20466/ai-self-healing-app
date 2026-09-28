"""One tiny, real, non-destructive HTTP call per free-tier AI backend that
has a key configured in `.env` (`GEMINI_API_KEY`/`GROQ_API_KEY`/
`ANTHROPIC_API_KEY` for `openrouter_api`) -- NOT a heal run, never spends a
real AI_CHAIN attempt against a real bug. Prints only PASS/FAIL per
backend, never the key itself (never write it to any file, log or commit
-- see CLAUDE.md's terminal-only v1.0 log entry).

Usage: `python scripts/check_ai_backends.py`
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import settings  # noqa: E402
from healer.api_adapters import GeminiApiClient, GroqClient, OpenRouterClient  # noqa: E402
from healer.backend_chain import BackendCooldownError  # noqa: E402

_PROBE_TOOLS: list[dict[str, object]] = []
_PROBE_MESSAGES = [{"role": "user", "content": "Reply with the single word: pong"}]


async def _probe(name: str, client_factory: object) -> str:
    try:
        client = client_factory()  # type: ignore[operator]
    except RuntimeError as exc:
        return f"{name}: SKIPPED (no key) -- {exc}"
    try:
        response = await client.messages.create(
            model=None,
            max_tokens=64,
            system=[{"type": "text", "text": "Reply with exactly one word."}],
            tools=_PROBE_TOOLS,
            messages=_PROBE_MESSAGES,
        )
    except BackendCooldownError as exc:
        return f"{name}: FAIL (cooldown/auth error) -- {exc}"
    except Exception as exc:  # noqa: BLE001 -- report any failure, don't crash the script
        return f"{name}: FAIL -- {exc}"
    text = " ".join(b.text for b in response.content if b.type == "text")
    return f"{name}: PASS -- verified live (real HTTP response: {text.strip()!r})"


async def main() -> int:
    results = []
    if settings.gemini_api_key:
        results.append(await _probe("gemini_api", GeminiApiClient))
    else:
        results.append("gemini_api: SKIPPED (GEMINI_API_KEY not set) -- untested live")
    if settings.groq_api_key:
        results.append(await _probe("groq_api", GroqClient))
    else:
        results.append("groq_api: SKIPPED (GROQ_API_KEY not set) -- untested live")
    if settings.anthropic_api_key:
        results.append(await _probe("openrouter_api", OpenRouterClient))
    else:
        results.append("openrouter_api: SKIPPED (ANTHROPIC_API_KEY not set) -- untested live")

    for line in results:
        print(line)
    return 0 if all("FAIL" not in line for line in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
