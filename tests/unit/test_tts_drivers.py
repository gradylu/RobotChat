"""TTS 驱动单测：PCM16LE 输出、音色切换、云端 base64 / 二进制音频（T-2.4、T-2.5）。"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from robotchat.providers.base import TTSRequest
from robotchat.providers.tts.dashscope_tts import DashScopeTTS
from robotchat.providers.tts.qwen3_tts import Qwen3TTSLocal, TTSUnavailableError


class NeverCancelled:
    @property
    def cancelled(self) -> bool:
        return False

    async def wait(self) -> None:
        return None


class FakeWS:
    def __init__(self, incoming: list[Any]) -> None:
        self.sent: list[Any] = []
        self._incoming = list(incoming)
        self.closed = False

    async def send(self, data: Any) -> None:
        self.sent.append(data)

    async def recv(self) -> Any:
        if not self._incoming:
            raise ConnectionError("closed")
        return self._incoming.pop(0)

    async def close(self) -> None:
        self.closed = True


async def test_qwen3_tts_streams_pcm16le_and_switches_voice() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, content=b"\x01\x02\x03\x04" * 50)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tts = Qwen3TTSLocal(
        {"base_url": "http://127.0.0.1:8091", "voice": "zh_female", "sample_rate": 16000},
        client=client,
    )
    chunks = [
        chunk
        async for chunk in tts.synthesize_stream(
            TTSRequest(text="你好", voice="en_female", sample_rate=24000), NeverCancelled()
        )
    ]
    await client.aclose()

    assert chunks
    assert all(chunk.channels == 1 and chunk.bit_depth == 16 for chunk in chunks)
    assert all(chunk.sample_rate == 24000 for chunk in chunks)
    assert b"".join(chunk.data for chunk in chunks) == b"\x01\x02\x03\x04" * 50
    assert captured["payload"]["voice"] == "en_female"
    assert captured["payload"]["format"] == "pcm"
    assert captured["payload"]["sample_rate"] == 24000


async def test_qwen3_tts_uses_default_voice_when_request_unspecified() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, content=b"\x00\x00")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tts = Qwen3TTSLocal({"base_url": "http://127.0.0.1:8091", "voice": "zh_female"}, client=client)
    _ = [
        chunk
        async for chunk in tts.synthesize_stream(
            TTSRequest(text="你好", voice="default"), NeverCancelled()
        )
    ]
    await client.aclose()
    assert captured["payload"]["voice"] == "zh_female"


async def test_qwen3_tts_http_error_raises() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(503, content=b"down"))
    )
    tts = Qwen3TTSLocal({"base_url": "http://127.0.0.1:8091"}, client=client)
    with pytest.raises(TTSUnavailableError, match="503"):
        _ = [
            chunk
            async for chunk in tts.synthesize_stream(
                TTSRequest(text="你好"), NeverCancelled()
            )
        ]
    await client.aclose()


async def test_dashscope_tts_binary_and_base64_audio() -> None:
    payload = base64.b64encode(b"\x05\x06").decode()
    ws = FakeWS(
        [
            b"\x01\x02",
            json.dumps(
                {
                    "header": {"event": "result-generated"},
                    "payload": {"output": {"audio": {"data": payload}}},
                }
            ),
            json.dumps({"header": {"event": "task-finished"}}),
        ]
    )

    async def connect(url: str, headers: dict[str, str]) -> FakeWS:
        return ws

    tts = DashScopeTTS(
        {"voice": "zh_female", "api_key_env": "DASHSCOPE_API_KEY"},
        connect=connect,
        environ={"DASHSCOPE_API_KEY": "test-key"},
    )
    chunks = [
        chunk
        async for chunk in tts.synthesize_stream(
            TTSRequest(text="你好", sample_rate=16000), NeverCancelled()
        )
    ]

    assert [chunk.data for chunk in chunks] == [b"\x01\x02", b"\x05\x06"]
    # 与本地驱动输出格式一致，可直接互换（C-52）
    assert all(chunk.sample_rate == 16000 and chunk.channels == 1 for chunk in chunks)
    assert all(chunk.bit_depth == 16 for chunk in chunks)
    assert ws.closed


async def test_dashscope_tts_requires_api_key() -> None:
    async def connect(url: str, headers: dict[str, str]) -> FakeWS:
        raise AssertionError("缺少凭据时不应建立连接")

    tts = DashScopeTTS({}, connect=connect, environ={})
    with pytest.raises(TTSUnavailableError, match="DASHSCOPE_API_KEY"):
        _ = [
            chunk
            async for chunk in tts.synthesize_stream(
                TTSRequest(text="你好"), NeverCancelled()
            )
        ]


async def test_dashscope_tts_task_failed_raises() -> None:
    ws = FakeWS(
        [
            json.dumps(
                {"header": {"event": "task-failed", "error_message": "quota exceeded"}}
            )
        ]
    )

    async def connect(url: str, headers: dict[str, str]) -> FakeWS:
        return ws

    tts = DashScopeTTS(
        {}, connect=connect, environ={"DASHSCOPE_API_KEY": "test-key"}
    )
    with pytest.raises(TTSUnavailableError, match="quota exceeded"):
        _ = [
            chunk
            async for chunk in tts.synthesize_stream(
                TTSRequest(text="你好"), NeverCancelled()
            )
        ]
