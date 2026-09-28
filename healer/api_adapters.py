"""`gemini_api`/`groq_api`: real tool-calling backends via each provider's own
free-tier HTTP API (not a CLI, not the Anthropic SDK).

These are `AnthropicClientLike` ADAPTERS (see `healer/anthropic_client.py`'s
narrow structural Protocol) over Groq's OpenAI-compatible chat-completions
API and Google's Gemini `generateContent` function-calling API. Wrapping the
wire format this way -- rather than writing a second tool-call loop -- means
`healer/runtime_agent.py:run_heal_job` and `healer/ci_agent.py:
run_ci_heal_job` run **completely unchanged** for these two new backends:
every guardrail (sandboxed write scope, patch anti-cheat, budget caps,
circuit breakers, the fail-before-pass proof) is enforced by those same
modules exactly as it is for Claude/Codex/Gemini-CLI/API-mode. This was a
deliberate reuse decision, not an oversight -- rewriting the tool loop per
backend would each need its own re-verification of every guardrail.

Both raise `healer.backend_chain.BackendCooldownError` for a 429/401/403
response (quota, rate limit, or auth failure) so `healer/worker.py`'s chain
logic can mark that backend cooling down and move to the next one, and a
plain `RuntimeError` for anything else (a normal failure, per the task's
"other errors are normal failures").
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from core.config import settings
from healer.backend_chain import BackendCooldownError
from sentinel.scrubber import scrub_text

GROQ_API_BASE = "https://api.groq.com/openai/v1"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"


@dataclass
class _Block:
    type: str
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class _Response:
    content: list[_Block]
    usage: _Usage
    stop_reason: str = "end_turn"


def _system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    parts = []
    for block in system or []:
        text = block.get("text") if isinstance(block, dict) else None
        if text:
            parts.append(text)
    return "\n".join(parts)


def _retry_after_seconds(response: httpx.Response) -> int | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _raise_for_status_classified(response: httpx.Response, *, backend: str) -> None:
    if response.status_code in (401, 403, 429):
        raise BackendCooldownError(
            f"{backend} returned {response.status_code}: {response.text[:500]}",
            retry_after_seconds=_retry_after_seconds(response),
        )
    if response.status_code >= 400:
        raise RuntimeError(f"{backend} returned {response.status_code}: {response.text[:500]}")


class _GroqMessages:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def _to_openai_messages(
        self, system: Any, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": _system_text(system)}]
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            if isinstance(content, str):
                out.append({"role": role, "content": content})
                continue
            if role == "assistant":
                text_parts = []
                tool_calls = []
                for block in content:
                    block_type = block.type if isinstance(block, _Block) else block.get("type")
                    if block_type == "text":
                        text = block.text if isinstance(block, _Block) else block.get("text")
                        if text:
                            text_parts.append(text)
                    elif block_type == "tool_use":
                        block_id = block.id if isinstance(block, _Block) else block.get("id")
                        name = block.name if isinstance(block, _Block) else block.get("name")
                        args = block.input if isinstance(block, _Block) else block.get("input", {})
                        tool_calls.append(
                            {
                                "id": block_id,
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)},
                            }
                        )
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": " ".join(text_parts) or None,
                }
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                out.append(assistant_msg)
            else:
                # A "user" turn whose content is a list of tool_result blocks
                # (Anthropic shape) -> one OpenAI "tool" message per result.
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        out.append(
                            {
                                "role": "tool",
                                "tool_call_id": item["tool_use_id"],
                                "content": str(item.get("content", "")),
                            }
                        )
                    elif isinstance(item, dict) and item.get("type") == "text":
                        out.append({"role": "user", "content": item.get("text", "")})
        # Privacy guard, defense-in-depth (terminal-only v1.0 Step 5): scrub
        # every plain-text message content field right before it goes out
        # over the wire to Groq. Tool RESULTS are already scrubbed once at
        # the MCP layer (mcp_server/audit.py) -- this is a second, cheap
        # pass on the final HTTP payload, not the only line of defense.
        for m in out:
            if isinstance(m.get("content"), str):
                m["content"] = scrub_text(m["content"])
        return out

    @staticmethod
    def _to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]

    async def create(self, **kwargs: Any) -> _Response:
        system = kwargs.get("system")
        messages = kwargs["messages"]
        tools = kwargs.get("tools", [])
        max_tokens = kwargs.get("max_tokens", 4096)

        payload: dict[str, Any] = {
            "model": settings.groq_api_model,
            "messages": self._to_openai_messages(system, messages),
            "max_tokens": max_tokens,
            # The default model (openai/gpt-oss-120b) is a reasoning model:
            # verified live that with the default (higher) reasoning effort
            # it can burn the entire max_tokens budget on its hidden
            # `reasoning` field and return empty `content` (finish_reason
            # "length") -- "low" keeps that overhead small and predictable
            # for a tool-calling agent loop.
            "reasoning_effort": "low",
        }
        if tools:
            payload["tools"] = self._to_openai_tools(tools)
            payload["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{GROQ_API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
            )
        _raise_for_status_classified(response, backend="groq_api")
        body = response.json()

        message = body["choices"][0]["message"]
        blocks: list[_Block] = []
        if message.get("content"):
            blocks.append(_Block(type="text", text=message["content"]))
        for call in message.get("tool_calls") or []:
            try:
                args = json.loads(call["function"]["arguments"])
            except (json.JSONDecodeError, TypeError):
                args = {}
            blocks.append(
                _Block(type="tool_use", id=call["id"], name=call["function"]["name"], input=args)
            )

        usage = body.get("usage", {})
        return _Response(
            content=blocks,
            usage=_Usage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
            ),
        )


class GroqClient:
    """`AnthropicClientLike` -- pass to `runtime_agent.run_heal_job`/
    `ci_agent.run_ci_heal_job` exactly like the real Anthropic client."""

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or settings.groq_api_key
        if not key:
            raise RuntimeError("GROQ_API_KEY is not configured")
        self._messages = _GroqMessages(key)

    @property
    def messages(self) -> _GroqMessages:
        return self._messages


class _GeminiMessages:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def _to_gemini_contents(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            if isinstance(content, str):
                out.append(
                    {"role": "user" if role == "user" else "model", "parts": [{"text": content}]}
                )
                continue
            if role == "assistant":
                parts: list[dict[str, Any]] = []
                for block in content:
                    block_type = block.type if isinstance(block, _Block) else block.get("type")
                    if block_type == "text":
                        text = block.text if isinstance(block, _Block) else block.get("text")
                        if text:
                            parts.append({"text": text})
                    elif block_type == "tool_use":
                        name = block.name if isinstance(block, _Block) else block.get("name")
                        args = block.input if isinstance(block, _Block) else block.get("input", {})
                        parts.append({"functionCall": {"name": name, "args": args}})
                out.append({"role": "model", "parts": parts})
            else:
                parts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        parts.append(
                            {
                                "functionResponse": {
                                    "name": item.get("tool_use_id", ""),
                                    "response": {"content": str(item.get("content", ""))},
                                }
                            }
                        )
                    elif isinstance(item, dict) and item.get("type") == "text":
                        parts.append({"text": item.get("text", "")})
                out.append({"role": "user", "parts": parts})
        # Privacy guard, defense-in-depth (terminal-only v1.0 Step 5): see
        # the matching comment in _GroqMessages._to_openai_messages above.
        for m in out:
            for part in m.get("parts", []):
                if isinstance(part.get("text"), str):
                    part["text"] = scrub_text(part["text"])
                response = part.get("functionResponse", {}).get("response")
                if isinstance(response, dict) and isinstance(response.get("content"), str):
                    response["content"] = scrub_text(response["content"])
        return out

    @staticmethod
    def _to_gemini_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        declarations = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            }
            for t in tools
        ]
        return [{"functionDeclarations": declarations}]

    async def create(self, **kwargs: Any) -> _Response:
        system = kwargs.get("system")
        messages = kwargs["messages"]
        tools = kwargs.get("tools", [])
        max_tokens = kwargs.get("max_tokens", 4096)

        payload: dict[str, Any] = {
            "contents": self._to_gemini_contents(messages),
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
        system_text = scrub_text(_system_text(system))
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        if tools:
            payload["tools"] = self._to_gemini_tools(tools)

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{GEMINI_API_BASE}/models/{settings.gemini_api_model}:generateContent",
                headers={"x-goog-api-key": self._api_key},
                json=payload,
            )
        _raise_for_status_classified(response, backend="gemini_api")
        body = response.json()

        candidates = body.get("candidates") or []
        if not candidates:
            return _Response(content=[], usage=_Usage())
        parts = candidates[0].get("content", {}).get("parts", [])
        blocks: list[_Block] = []
        call_index = 0
        for part in parts:
            if "text" in part:
                blocks.append(_Block(type="text", text=part["text"]))
            elif "functionCall" in part:
                call = part["functionCall"]
                call_index += 1
                blocks.append(
                    _Block(
                        type="tool_use",
                        id=f"call_{call_index}",
                        name=call["name"],
                        input=dict(call.get("args", {})),
                    )
                )

        usage_meta = body.get("usageMetadata", {})
        return _Response(
            content=blocks,
            usage=_Usage(
                input_tokens=usage_meta.get("promptTokenCount", 0),
                output_tokens=usage_meta.get("candidatesTokenCount", 0),
            ),
        )


class GeminiApiClient:
    """`AnthropicClientLike` adapter over Gemini's `generateContent` API.

    Note on key format/transport: since 2026-05-28 Google AI Studio issues
    "auth keys" starting with `AQ.` instead of the legacy `AIzaSy...`
    standard keys, and auth keys are rejected by the `?key=` query-param
    style -- they must go in the `x-goog-api-key` header (which Google's
    docs say works for legacy `AIzaSy` keys too), confirmed via
    https://ai.google.dev/gemini-api/docs/api-key and Google's own AI
    Developer forum threads reporting the exact 401 this project's
    original `?key=` implementation hit. Don't switch back to `?key=`.

    Note on tool_use ids: Gemini's `functionCall`/`functionResponse` protocol
    matches calls by function *name*, not by an id the way Anthropic/OpenAI
    do -- this adapter invents a `call_N` id per response purely so
    `runtime_agent.py`'s existing "match tool_result.tool_use_id to the
    block that requested it" bookkeeping has something to key on; it is
    never sent back to Gemini (only the function *name* is, in
    `functionResponse.name`). Fine for this loop's single-call-then-respond
    pattern; would need revisiting if Gemini ever calls the same tool twice
    in one turn (not observed in this project's own live check call).
    """

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or settings.gemini_api_key
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        self._messages = _GeminiMessages(key)

    @property
    def messages(self) -> _GeminiMessages:
        return self._messages
