"""ASR 驱动单测：partial / final 语义、热词、两驱动结果等价（T-2.1、T-2.2）。

测试使用假的 WebSocket 连接，不依赖真实 FunASR / DashScope 服务。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from robotchat.audio.frame import AudioFrame
from robotchat.providers.asr.dashscope_stream import DashScopeASR
from robotchat.providers.asr.funasr_dialect import FunASRDialectASR
from robotchat.providers.asr.funasr_local import FunASRLocalASR
from robotchat.providers.base import ASREvent


class NeverCancelled:
    @property
    def cancelled(self) -> bool:
        return False

    async def wait(self) -> None:
        return None


class FakeWS:
    def __init__(self, incoming: list[Any] | None = None) -> None:
        self.sent: list[Any] = []
        self.closed = False
        self._incoming = list(incoming or [])

    async def send(self, data: Any) -> None:
        self.sent.append(data)

    async def recv(self) -> Any:
        if not self._incoming:
            raise ConnectionError("closed")
        return self._incoming.pop(0)

    async def close(self) -> None:
        self.closed = True


async def _collect(driver: Any, ws: FakeWS) -> list[ASREvent]:
    async def connect(url: str, headers: dict[str, str]) -> FakeWS:
        return ws

    driver._connect = connect
    stream = await driver.open_stream("session-1", NeverCancelled())
    await stream.feed(AudioFrame(data=b"\x00\x00", sample_rate=16000))
    events = [event async for event in stream.events()]
    await stream.close()
    return events


async def test_funasr_partial_then_final_with_hotwords() -> None:
    ws = FakeWS(
        [
            json.dumps({"text": "打开", "mode": "online"}),
            json.dumps({"text": "打开空调", "mode": "offline"}),
        ]
    )
    driver = FunASRLocalASR(
        {"endpoint": "http://127.0.0.1:10095", "hotwords": ["充电桩", "A点"]}
    )
    events = await _collect(driver, ws)

    assert [event.type for event in events] == ["partial", "final"]
    assert events[1].text == "打开空调"
    config = json.loads(ws.sent[0])
    assert config["mode"] == "2pass"
    assert config["hotwords"] == "充电桩,A点"
    assert config["wav_format"] == "pcm"
    assert json.loads(ws.sent[-1]) == {"is_speaking": False}


async def test_funasr_message_without_text_ignored() -> None:
    ws = FakeWS(
        [
            json.dumps({"text": "", "mode": "online"}),
            json.dumps({"text": "你好", "mode": "2pass-offline"}),
        ]
    )
    driver = FunASRLocalASR({"endpoint": "127.0.0.1:10095"})
    events = await _collect(driver, ws)
    assert [event.type for event in events] == ["final"]


async def test_dashscope_stream_partial_final_and_hotwords() -> None:
    ws = FakeWS(
        [
            json.dumps(
                {
                    "header": {"event": "result-generated"},
                    "payload": {"output": {"sentence": {"text": "打开", "sentence_end": False}}},
                }
            ),
            json.dumps(
                {
                    "header": {"event": "result-generated"},
                    "payload": {
                        "output": {"sentence": {"text": "打开空调。", "sentence_end": True}}
                    },
                }
            ),
        ]
    )
    driver = DashScopeASR(
        {"hotwords": ["充电桩"], "api_key_env": "DASHSCOPE_API_KEY"},
        environ={"DASHSCOPE_API_KEY": "test-key"},
    )
    events = await _collect(driver, ws)

    assert [event.type for event in events] == ["partial", "final"]
    assert events[1].text == "打开空调。"
    run_task = json.loads(ws.sent[0])
    assert run_task["header"]["action"] == "run-task"
    assert run_task["payload"]["parameters"]["hot_words"] == ["充电桩"]
    assert json.loads(ws.sent[-1])["header"]["action"] == "finish-task"


async def test_dashscope_requires_api_key() -> None:
    driver = DashScopeASR({}, environ={})

    async def connect(url: str, headers: dict[str, str]) -> FakeWS:
        raise AssertionError("缺少凭据时不应建立连接")

    driver._connect = connect
    with pytest.raises(RuntimeError, match="DASHSCOPE_API_KEY"):
        await driver.open_stream("session-1", NeverCancelled())


async def test_two_drivers_produce_equivalent_events() -> None:
    """同一段转写文本，本地与云端驱动输出 partial / final 序列一致（C-52）。"""
    funasr_ws = FakeWS(
        [
            json.dumps({"text": "今天天气不错", "mode": "online"}),
            json.dumps({"text": "今天天气不错。", "mode": "offline"}),
        ]
    )
    dashscope_ws = FakeWS(
        [
            json.dumps(
                {
                    "header": {"event": "result-generated"},
                    "payload": {
                        "output": {"sentence": {"text": "今天天气不错", "sentence_end": False}}
                    },
                }
            ),
            json.dumps(
                {
                    "header": {"event": "result-generated"},
                    "payload": {
                        "output": {"sentence": {"text": "今天天气不错。", "sentence_end": True}}
                    },
                }
            ),
        ]
    )

    local = await _collect(FunASRLocalASR({"endpoint": "127.0.0.1:10095"}), funasr_ws)
    cloud = await _collect(
        DashScopeASR({}, environ={"DASHSCOPE_API_KEY": "test-key"}), dashscope_ws
    )

    assert [(event.type, event.text) for event in local] == [
        (event.type, event.text) for event in cloud
    ]


async def test_funasr_local_passes_language_to_service() -> None:
    ws = FakeWS([json.dumps({"text": "hello", "mode": "offline"})])
    driver = FunASRLocalASR({"endpoint": "127.0.0.1:10095"})

    async def connect(url: str, headers: dict[str, str]) -> FakeWS:
        return ws

    driver._connect = connect
    stream = await driver.open_stream("session-1", NeverCancelled(), lang="en")
    await stream.close()
    assert json.loads(ws.sent[0])["lang"] == "en"


async def test_funasr_dialect_selects_endpoint_and_model() -> None:
    ws = FakeWS([json.dumps({"text": "整挺好", "mode": "offline"})])
    connected: list[str] = []

    async def connect(url: str, headers: dict[str, str]) -> FakeWS:
        connected.append(url)
        return ws

    driver = FunASRDialectASR(
        {
            "endpoints": {"dongbei": "http://127.0.0.1:10096"},
            "models": {"dongbei": "paraformer-dongbei"},
            "hotwords": ["充电桩"],
        },
        connect=connect,
    )
    stream = await driver.open_stream("session-1", NeverCancelled(), lang="dongbei")
    await stream.feed(AudioFrame(data=b"\x00\x00", sample_rate=16000))
    events = [event async for event in stream.events()]
    await stream.close()

    assert [event.type for event in events] == ["final"]
    assert connected == ["ws://127.0.0.1:10096"]
    start = json.loads(ws.sent[0])
    assert start["lang"] == "dongbei"
    assert start["model"] == "paraformer-dongbei"
    assert driver.capabilities.languages == frozenset({"dongbei"})


async def test_funasr_dialect_unknown_language_raises() -> None:
    driver = FunASRDialectASR({"endpoints": {"dongbei": "http://127.0.0.1:10096"}})
    with pytest.raises(RuntimeError, match="未配置语言"):
        await driver.open_stream("session-1", NeverCancelled(), lang="wuhan")


async def test_dashscope_language_hint_follows_requested_lang() -> None:
    ws = FakeWS([])
    driver = DashScopeASR(
        {"languages": ["zh", "en", "yue"], "language_hints": ["zh"]},
        environ={"DASHSCOPE_API_KEY": "test-key"},
    )

    async def connect(url: str, headers: dict[str, str]) -> FakeWS:
        return ws

    driver._connect = connect
    stream = await driver.open_stream("session-1", NeverCancelled(), lang="yue")
    await stream.close()
    run_task = json.loads(ws.sent[0])
    assert run_task["payload"]["parameters"]["language_hints"] == ["yue"]
