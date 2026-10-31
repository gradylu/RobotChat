"""OpenAI 兼容 LLM 驱动单测：SSE 流式、tool call、分档超时、本机地址策略（T-2.3、T-2.11）。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from robotchat.providers.base import ChatMessage, ChatRequest
from robotchat.providers.llm.openai_compat import OpenAICompatLLM, endpoint_tier
from robotchat.tools.base import ToolDefinition


class NeverCancelled:
    @property
    def cancelled(self) -> bool:
        return False

    async def wait(self) -> None:
        return None


def _sse(chunks: list[dict[str, Any]]) -> bytes:
    body = b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks)
    return body + b"data: [DONE]\n\n"


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_chat_stream_yields_tokens_and_tool_call() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        content = _sse(
            [
                {"choices": [{"delta": {"content": "好的，"}}]},
                {"choices": [{"delta": {"content": "马上"}}]},
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {
                                            "name": "set_ac",
                                            "arguments": '{"temperature":',
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": " 26}"}}]}}
                    ]
                },
            ]
        )
        return httpx.Response(200, content=content)

    client = _client(handler)
    llm = OpenAICompatLLM(
        {"base_url": "http://10.0.0.12:8000/v1", "model": "qwen"}, client=client
    )
    tool = ToolDefinition(
        name="set_ac",
        description="设置空调温度",
        parameters={"type": "object", "properties": {"temperature": {"type": "number"}}},
    )
    events = [
        event
        async for event in llm.chat_stream(
            ChatRequest(
                messages=[ChatMessage(role="user", content="把空调调到26度")],
                tools=[tool],
            ),
            NeverCancelled(),
        )
    ]
    await client.aclose()

    assert [event.type for event in events] == ["token", "token", "tool_call", "done"]
    assert "".join(event.text for event in events if event.type == "token") == "好的，马上"
    call = events[2].tool_call
    assert call is not None
    assert call["name"] == "set_ac"
    assert call["arguments"] == {"temperature": 26}
    payload = captured["payload"]
    assert payload["stream"] is True
    assert payload["tools"][0]["function"]["name"] == "set_ac"
    assert payload["tool_choice"] == "auto"


async def test_http_error_becomes_error_event() -> None:
    client = _client(lambda request: httpx.Response(500, content=b"boom"))
    llm = OpenAICompatLLM(
        {"base_url": "http://10.0.0.12:8000/v1", "model": "qwen"}, client=client
    )
    events = [
        event
        async for event in llm.chat_stream(
            ChatRequest(messages=[ChatMessage(role="user", content="hi")]), NeverCancelled()
        )
    ]
    await client.aclose()
    assert [event.type for event in events] == ["error"]
    assert "500" in (events[0].error or "")


async def test_tools_omitted_when_capability_disabled() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, content=_sse([{"choices": [{"delta": {"content": "hi"}}]}]))

    client = _client(handler)
    llm = OpenAICompatLLM(
        {
            "base_url": "http://10.0.0.12:8000/v1",
            "model": "qwen",
            "supports_tools": False,
        },
        client=client,
    )
    tool = ToolDefinition(name="set_ac", description="设置空调")
    _ = [
        event
        async for event in llm.chat_stream(
            ChatRequest(messages=[ChatMessage(role="user", content="hi")], tools=[tool]),
            NeverCancelled(),
        )
    ]
    await client.aclose()
    assert "tools" not in captured["payload"]


def test_endpoint_tier_by_location() -> None:
    assert endpoint_tier("127.0.0.1") == "local"
    assert endpoint_tier("localhost") == "local"
    assert endpoint_tier("10.0.0.12") == "edge"
    assert endpoint_tier("192.168.1.5") == "edge"
    assert endpoint_tier("8.8.8.8") == "cloud"
    assert endpoint_tier("dashscope.aliyuncs.com") == "cloud"


def test_timeout_tiers_differ_by_location() -> None:
    edge = OpenAICompatLLM({"base_url": "http://10.0.0.12:8000/v1", "model": "qwen"})
    cloud = OpenAICompatLLM(
        {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen"}
    )
    assert edge.tier == "edge"
    assert cloud.tier == "cloud"
    assert edge.timeouts["total"] == 30000
    assert cloud.timeouts["total"] == 60000
    assert edge.timeouts["first_token"] == 800


def test_local_llm_rejected_by_default() -> None:
    with pytest.raises(ValueError, match="allow_local_llm"):
        OpenAICompatLLM({"base_url": "http://127.0.0.1:8000/v1", "model": "qwen"})


def test_local_llm_allowed_with_explicit_flag() -> None:
    llm = OpenAICompatLLM(
        {
            "base_url": "http://127.0.0.1:8000/v1",
            "model": "qwen",
            "allow_local_llm": True,
            "local_llm_min_memory_gb": 0,
        }
    )
    assert llm.tier == "local"


def test_missing_base_url_rejected() -> None:
    with pytest.raises(ValueError, match="base_url"):
        OpenAICompatLLM({"base_url": "${LLM_BASE_URL}", "model": "qwen"})
