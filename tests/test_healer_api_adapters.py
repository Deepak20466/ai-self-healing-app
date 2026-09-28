"""healer/api_adapters.py: GroqClient/GeminiApiClient/OpenRouterClient, the
AnthropicClientLike adapters over each provider's real free-tier HTTP API.
All HTTP mocked via respx -- see CLAUDE.md "Anthropic and GitHub are always
mocked in tests", extended here to the three API-key backends.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from healer.api_adapters import (
    GEMINI_API_BASE,
    GROQ_API_BASE,
    OPENROUTER_API_BASE,
    GeminiApiClient,
    GroqClient,
    OpenRouterClient,
)
from healer.backend_chain import BackendCooldownError

TOOLS = [
    {
        "name": "get_error",
        "description": "Get an error by id",
        "input_schema": {
            "type": "object",
            "properties": {"error_id": {"type": "integer"}},
            "required": ["error_id"],
        },
    }
]


async def test_groq_text_only_response(respx_mock: Any) -> None:
    respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "hello there"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )
    client = GroqClient(api_key="gsk_fake")
    response = await client.messages.create(
        model=None,
        max_tokens=100,
        system=[{"type": "text", "text": "be helpful"}],
        tools=[],
        messages=[{"role": "user", "content": "hi"}],
    )
    assert len(response.content) == 1
    assert response.content[0].type == "text"
    assert response.content[0].text == "hello there"
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 5


async def test_groq_tool_call_response_and_round_trip(respx_mock: Any) -> None:
    route = respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_error",
                                        "arguments": '{"error_id": 7}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
    )
    client = GroqClient(api_key="gsk_fake")
    response = await client.messages.create(
        model=None,
        max_tokens=100,
        system=[{"type": "text", "text": "sys"}],
        tools=TOOLS,
        messages=[{"role": "user", "content": "get error 7"}],
    )
    block = response.content[0]
    assert block.type == "tool_use"
    assert block.name == "get_error"
    assert block.input == {"error_id": 7}

    # Now round-trip: append the assistant turn + a tool_result, as
    # runtime_agent.py's real loop does, and confirm the translator produces
    # a valid OpenAI-shaped tool message.
    messages = [
        {"role": "user", "content": "get error 7"},
        {"role": "assistant", "content": response.content},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": '{"exception_type": "KeyError"}',
                    "is_error": False,
                }
            ],
        },
    ]
    route.mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "It's a KeyError."}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2},
            },
        )
    )
    second = await client.messages.create(
        model=None,
        max_tokens=100,
        system=[{"type": "text", "text": "sys"}],
        tools=TOOLS,
        messages=messages,
    )
    assert second.content[0].text == "It's a KeyError."
    sent_body = route.calls.last.request.content
    assert b"tool_call_id" in sent_body
    assert b"KeyError" in sent_body


async def test_groq_429_raises_cooldown_with_retry_after(respx_mock: Any) -> None:
    respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(429, headers={"retry-after": "120"}, text="rate limited")
    )
    client = GroqClient(api_key="gsk_fake")
    with pytest.raises(BackendCooldownError) as exc_info:
        await client.messages.create(
            model=None,
            max_tokens=10,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
        )
    assert exc_info.value.retry_after_seconds == 120


async def test_groq_401_raises_cooldown(respx_mock: Any) -> None:
    respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(401, text="invalid api key")
    )
    client = GroqClient(api_key="gsk_fake")
    with pytest.raises(BackendCooldownError):
        await client.messages.create(
            model=None,
            max_tokens=10,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
        )


async def test_groq_500_raises_plain_runtime_error(respx_mock: Any) -> None:
    respx_mock.post(f"{GROQ_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(500, text="server error")
    )
    client = GroqClient(api_key="gsk_fake")
    with pytest.raises(RuntimeError):
        await client.messages.create(
            model=None,
            max_tokens=10,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
        )


def test_groq_client_requires_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import settings as core_settings

    monkeypatch.setattr(core_settings, "groq_api_key", None)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        GroqClient(api_key=None)


async def test_gemini_text_only_response(respx_mock: Any) -> None:
    route = respx_mock.post(f"{GEMINI_API_BASE}/models/gemini-2.0-flash:generateContent").mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "hi from gemini"}]}}],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4},
            },
        )
    )
    client = GeminiApiClient(api_key="fake-key")
    response = await client.messages.create(
        model=None,
        max_tokens=100,
        system=[{"type": "text", "text": "be helpful"}],
        tools=[],
        messages=[{"role": "user", "content": "hi"}],
    )
    assert response.content[0].type == "text"
    assert response.content[0].text == "hi from gemini"
    assert response.usage.input_tokens == 3
    assert response.usage.output_tokens == 4
    assert route.calls.last.request.headers["x-goog-api-key"] == "fake-key"
    assert "key" not in route.calls.last.request.url.params


async def test_gemini_function_call_response(respx_mock: Any) -> None:
    respx_mock.post(f"{GEMINI_API_BASE}/models/gemini-2.0-flash:generateContent").mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"functionCall": {"name": "get_error", "args": {"error_id": 9}}}
                            ]
                        }
                    }
                ],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
            },
        )
    )
    client = GeminiApiClient(api_key="fake-key")
    response = await client.messages.create(
        model=None,
        max_tokens=100,
        system=[{"type": "text", "text": "sys"}],
        tools=TOOLS,
        messages=[{"role": "user", "content": "get error 9"}],
    )
    block = response.content[0]
    assert block.type == "tool_use"
    assert block.name == "get_error"
    assert block.input == {"error_id": 9}


async def test_gemini_429_raises_cooldown(respx_mock: Any) -> None:
    respx_mock.post(f"{GEMINI_API_BASE}/models/gemini-2.0-flash:generateContent").mock(
        return_value=httpx.Response(429, headers={"retry-after": "30"}, text="quota exceeded")
    )
    client = GeminiApiClient(api_key="fake-key")
    with pytest.raises(BackendCooldownError) as exc_info:
        await client.messages.create(
            model=None,
            max_tokens=10,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
        )
    assert exc_info.value.retry_after_seconds == 30


def test_gemini_client_requires_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import settings as core_settings

    monkeypatch.setattr(core_settings, "gemini_api_key", None)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        GeminiApiClient(api_key=None)


async def test_openrouter_text_only_response(
    respx_mock: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.config import settings as core_settings

    monkeypatch.setattr(core_settings, "anthropic_model", "openrouter/free")
    respx_mock.post(f"{OPENROUTER_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "hello there"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )
    client = OpenRouterClient(api_key="sk-or-fake")
    response = await client.messages.create(
        model=None,
        max_tokens=100,
        system=[{"type": "text", "text": "be helpful"}],
        tools=[],
        messages=[{"role": "user", "content": "hi"}],
    )
    assert len(response.content) == 1
    assert response.content[0].type == "text"
    assert response.content[0].text == "hello there"
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 5


async def test_openrouter_tool_call_response(respx_mock: Any) -> None:
    route = respx_mock.post(f"{OPENROUTER_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "get_error",
                                        "arguments": '{"error_id": 7}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
    )
    client = OpenRouterClient(api_key="sk-or-fake")
    response = await client.messages.create(
        model=None,
        max_tokens=100,
        system=[{"type": "text", "text": "sys"}],
        tools=TOOLS,
        messages=[{"role": "user", "content": "get error 7"}],
    )
    block = response.content[0]
    assert block.type == "tool_use"
    assert block.name == "get_error"
    assert block.input == {"error_id": 7}
    assert route.called


async def test_openrouter_429_raises_cooldown_with_retry_after(respx_mock: Any) -> None:
    respx_mock.post(f"{OPENROUTER_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(429, headers={"retry-after": "60"}, text="rate limited")
    )
    client = OpenRouterClient(api_key="sk-or-fake")
    with pytest.raises(BackendCooldownError) as exc_info:
        await client.messages.create(
            model=None,
            max_tokens=10,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
        )
    assert exc_info.value.retry_after_seconds == 60


async def test_openrouter_500_raises_plain_runtime_error(respx_mock: Any) -> None:
    respx_mock.post(f"{OPENROUTER_API_BASE}/chat/completions").mock(
        return_value=httpx.Response(500, text="server error")
    )
    client = OpenRouterClient(api_key="sk-or-fake")
    with pytest.raises(RuntimeError):
        await client.messages.create(
            model=None,
            max_tokens=10,
            system=[],
            tools=[],
            messages=[{"role": "user", "content": "hi"}],
        )


def test_openrouter_client_requires_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config import settings as core_settings

    monkeypatch.setattr(core_settings, "anthropic_api_key", None)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        OpenRouterClient(api_key=None)
