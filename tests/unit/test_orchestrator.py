"""T-3.6 / T-3.7 编排器单测：文本输入全链路、逐句合成、落库 timings、复位与后台任务保留。"""

from __future__ import annotations

import asyncio
import time
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from robotchat.audio.file_io import FileAudioTransport
from robotchat.audio.frame import AudioChunk, AudioFrame
from robotchat.config.loader import load_settings
from robotchat.core.orchestrator import DialogOrchestrator
from robotchat.dialog.session import SessionStore
from robotchat.observability.metrics import E2E_FIRST_RESPONSE, LLM_FIRST_TOKEN, MetricsCollector
from robotchat.providers.base import (
    ASRCapabilities,
    ASREvent,
    ChatRequest,
    LLMCapabilities,
    LLMEvent,
    TTSCapabilities,
    TTSRequest,
    VadEvent,
)
from robotchat.providers.registry import ProviderRegistry

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
RATE = 16000
FRAME = 320


class FakeTransport:
    name = "fake"

    def __init__(self, frames: int = 40) -> None:
        self.frames = frames
        self.played: list[AudioChunk] = []
        self.flush_count = 0
        self.opened = False
        self.closed = False

    async def open(self) -> None:
        self.opened = True

    def capture(self):
        async def generator():
            for seq in range(self.frames):
                yield AudioFrame(
                    data=b"\x00\x00" * FRAME, sample_rate=RATE, seq=seq, ts_device=seq * 0.02
                )
                await asyncio.sleep(0.01)

        return generator()

    async def play(self, chunk: AudioChunk) -> None:
        self.played.append(chunk)

    async def flush(self) -> None:
        self.flush_count += 1

    async def close(self) -> None:
        self.closed = True


class FakeVad:
    name = "fake_vad"

    def process(self, frame: AudioFrame) -> None:
        return None

    def reset(self) -> None:
        return None


class ScriptedVad:
    """按帧序号产出起止音事件：文件音频端到端验证用（T-3.6）。"""

    name = "scripted_vad"

    def __init__(self, start_seq: int = 3, end_seq: int = 20) -> None:
        self.start_seq = start_seq
        self.end_seq = end_seq

    def process(self, frame: AudioFrame) -> VadEvent | None:
        if frame.seq == self.start_seq:
            return VadEvent(type="speech_start", energy_db=-18.0)
        if frame.seq == self.end_seq:
            return VadEvent(type="speech_end", energy_db=-40.0)
        return None

    def reset(self) -> None:
        return None


class FakeASRStream:
    """feed 计数 + 关闭后产出 final，用于验证音频帧确实流入 ASR。"""

    name = "fake_asr"
    capabilities = ASRCapabilities(
        streaming=True, languages=frozenset({"zh"}), sample_rates=(RATE,)
    )

    def __init__(self, text: str) -> None:
        self.text = text
        self.frames = 0
        self.closed = asyncio.Event()

    async def open_stream(
        self, session_id: str, cancel: Any, lang: str = ""
    ) -> FakeASRStream:
        del session_id, cancel, lang
        self.closed.clear()
        return self

    async def feed(self, frame: AudioFrame) -> None:
        self.frames += 1

    def events(self) -> AsyncIterator[ASREvent]:
        async def generator() -> AsyncIterator[ASREvent]:
            yield ASREvent(type="partial", text=self.text[:3], lang="zh")
            await self.closed.wait()
            yield ASREvent(type="final", text=self.text, lang="zh")

        return generator()

    async def close(self) -> None:
        self.closed.set()


class FakeLLM:
    name = "fake_llm"
    capabilities = LLMCapabilities(streaming=True, tool_call=False)

    def __init__(self, pieces: list[str]) -> None:
        self.pieces = pieces
        self.requests: list[ChatRequest] = []

    async def chat_stream(
        self, req: ChatRequest, cancel: Any
    ) -> AsyncIterator[LLMEvent]:
        self.requests.append(req)
        for piece in self.pieces:
            await asyncio.sleep(0)
            yield LLMEvent(type="token", text=piece)
        yield LLMEvent(type="done")


class FakeTTS:
    name = "fake_tts"
    capabilities = TTSCapabilities(sample_rate=RATE)

    def __init__(self) -> None:
        self.requests: list[TTSRequest] = []

    async def synthesize_stream(
        self, req: TTSRequest, cancel: Any
    ) -> AsyncIterator[AudioChunk]:
        self.requests.append(req)
        await asyncio.sleep(0)
        yield AudioChunk(data=b"\x00\x00" * FRAME, sample_rate=RATE)


@pytest.fixture()
def settings(tmp_path: Path):
    return load_settings(
        config_dir=REPO_CONFIG,
        data_dir=tmp_path,
        environ={
            "ROBOTCHAT__RESILIENCE__OFFLINE_PROBE_ON_START": "false",
            "ROBOTCHAT__STATE__HEARTBEAT_MS": "5000",
            "ROBOTCHAT__PROVIDERS__ASR__FALLBACK": "",
            "ROBOTCHAT__PROVIDERS__LLM__FALLBACK": "",
            "ROBOTCHAT__PROVIDERS__TTS__FALLBACK": "",
        },
    )


async def test_text_turn_runs_full_pipeline(settings, tmp_path: Path) -> None:
    transport = FakeTransport()
    llm = FakeLLM(["你好。", "我是", "机器人助手。"])
    tts = FakeTTS()
    store = SessionStore(tmp_path / "session.sqlite3")
    metrics = MetricsCollector()
    orchestrator = DialogOrchestrator(
        settings,
        ProviderRegistry(),
        metrics,
        store,
        transport=transport,
        vad=FakeVad(),
        providers={"llm": llm, "tts": tts},
    )

    await orchestrator.start()
    try:
        result = await orchestrator.handle_text("今天天气怎么样？")
        assert result is not None
        assert result["turn_id"] == 1
        assert result["state"] == "ACTIVE_IDLE"

        assert llm.requests and llm.requests[0].messages[-1].content == "今天天气怎么样？"
        # 按句切分：两个完整句各送一次 TTS，非等待整段
        assert [req.text for req in tts.requests] == ["你好。", "我是机器人助手。"]
        assert len(transport.played) == 2

        turns = store.turns("default")
        assert len(turns) == 1
        assert turns[0]["status"] == "completed"
        assert turns[0]["user_text"] == "今天天气怎么样？"
        assert turns[0]["assistant_text"] == "你好。我是机器人助手。"
        assert {"llm_first", "tts_first", "e2e"} <= set(turns[0]["timings"])

        stages = metrics.snapshot()
        assert LLM_FIRST_TOKEN in stages
        assert E2E_FIRST_RESPONSE in stages
    finally:
        await orchestrator.shutdown()
        store.close()


async def test_file_audio_mode_runs_one_full_turn(tmp_path: Path) -> None:
    """T-3.6 验收：本地文件音频模式（WAV 回放 + WAV 录制）跑通完整一轮对话。"""
    settings = load_settings(
        config_dir=REPO_CONFIG,
        data_dir=tmp_path,
        environ={
            "ROBOTCHAT__RESILIENCE__OFFLINE_PROBE_ON_START": "false",
            "ROBOTCHAT__STATE__HEARTBEAT_MS": "5000",
            "ROBOTCHAT__PROVIDERS__ASR__FALLBACK": "",
            # 关闭唤醒词 → 启动即常开聆听 ACTIVE_IDLE，文件音频可直接起音
            "ROBOTCHAT__PROVIDERS__WAKEWORD__ENABLED": "false",
        },
    )
    in_path = tmp_path / "in.wav"
    out_path = tmp_path / "out.wav"
    with wave.open(str(in_path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(b"\x00\x00" * FRAME * 30)

    transport = FileAudioTransport(
        {"capture_path": str(in_path), "playback_path": str(out_path)}
    )
    asr = FakeASRStream("今天天气怎么样？")
    store = SessionStore(tmp_path / "session.sqlite3")
    orchestrator = DialogOrchestrator(
        settings,
        ProviderRegistry(),
        MetricsCollector(),
        store,
        transport=transport,
        vad=ScriptedVad(start_seq=3, end_seq=20),
        providers={
            "asr": asr,
            "llm": FakeLLM(["你好。", "我是", "机器人助手。"]),
            "tts": FakeTTS(),
        },
    )

    await orchestrator.start()
    try:
        assert orchestrator.machine.state.value == "ACTIVE_IDLE"
        turns = await _wait_turn_completed(store)
        assert turns[0]["status"] == "completed"
        assert turns[0]["user_text"] == "今天天气怎么样？"
        assert turns[0]["assistant_text"] == "你好。我是机器人助手。"
        assert asr.frames > 0
        assert orchestrator.machine.state.value == "ACTIVE_IDLE"
    finally:
        await orchestrator.shutdown()
        store.close()

    # 播放落盘：文件通道把 TTS 音频写入 WAV，证明全链路确实产出语音
    with wave.open(str(out_path), "rb") as reader:
        assert reader.getnframes() > 0
    assert transport.played_chunks >= 2


async def _wait_turn_completed(
    store: SessionStore, timeout: float = 5.0
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        turns = store.turns("default")
        if turns and turns[0]["status"] == "completed":
            return turns
        await asyncio.sleep(0.02)
    raise AssertionError(f"轮次未在 {timeout}s 内完成：{store.turns('default')}")


async def test_reset_returns_to_idle_and_clears_history(settings, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.sqlite3")
    orchestrator = DialogOrchestrator(
        settings,
        ProviderRegistry(),
        MetricsCollector(),
        store,
        transport=FakeTransport(),
        vad=FakeVad(),
        providers={"llm": FakeLLM(["好的。"]), "tts": FakeTTS()},
    )
    await orchestrator.start()
    try:
        await orchestrator.handle_text("你好")
        result = await orchestrator.reset()
        assert result["reset"] is True
        assert result["state"] == "IDLE"
        assert orchestrator.turns.current_turn_id is None
    finally:
        await orchestrator.shutdown()
        assert store.session("default")["state"] == "ended"
        store.close()


async def test_watchdog_recovery_keeps_background_tasks(settings, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.sqlite3")
    orchestrator = DialogOrchestrator(
        settings,
        ProviderRegistry(),
        MetricsCollector(),
        store,
        transport=FakeTransport(),
        vad=FakeVad(),
        providers={"llm": FakeLLM(["好的。"]), "tts": FakeTTS()},
    )
    await orchestrator.start()
    gate = asyncio.Event()

    async def long_task() -> str:
        await gate.wait()
        return "done"

    task = orchestrator.dispatch_background(long_task(), name="long-task")
    try:
        await orchestrator._on_stall("测试无进展")
        assert not task.cancelled()
        gate.set()
        assert await asyncio.wait_for(task, timeout=1.0) == "done"
        assert orchestrator.machine.state.value == "IDLE"
    finally:
        await orchestrator.shutdown()
        store.close()


async def test_dialect_turn_marks_tts_fallback(settings, tmp_path: Path) -> None:
    """方言提问 + 无方言音色：lang 保留、tts_lang=zh、tts_fallback=true（FR-15.14）。"""
    llm = FakeLLM(["好的。"])
    tts = FakeTTS()
    store = SessionStore(tmp_path / "session.sqlite3")
    orchestrator = DialogOrchestrator(
        settings,
        ProviderRegistry(),
        MetricsCollector(),
        store,
        transport=FakeTransport(),
        vad=FakeVad(),
        providers={"llm": llm, "tts": tts},
    )
    await orchestrator.start()
    try:
        await orchestrator.handle_text("帮我看看充电桩", lang="dongbei")

        system_prompt = llm.requests[0].messages[0].content
        assert "普通话书面语" in system_prompt
        assert tts.requests[0].voice == "default"
        assert tts.requests[0].lang == "zh"

        turn = store.turns("default")[0]
        assert turn["lang"] == "dongbei"
        assert turn["tts_lang"] == "zh"
        assert turn["tts_fallback"] == 1
        assert orchestrator.language.stats.tts_fallback_count == 1
    finally:
        await orchestrator.shutdown()
        store.close()


async def test_english_turn_uses_english_voice(settings, tmp_path: Path) -> None:
    llm = FakeLLM(["Hello."])
    tts = FakeTTS()
    store = SessionStore(tmp_path / "session.sqlite3")
    orchestrator = DialogOrchestrator(
        settings,
        ProviderRegistry(),
        MetricsCollector(),
        store,
        transport=FakeTransport(),
        vad=FakeVad(),
        providers={"llm": llm, "tts": tts},
    )
    await orchestrator.start()
    try:
        await orchestrator.handle_text("Hello there", lang="en")

        assert "English" in llm.requests[0].messages[0].content
        assert tts.requests[0].voice == "en_female"
        assert tts.requests[0].lang == "en"

        turn = store.turns("default")[0]
        assert turn["lang"] == "en"
        assert turn["tts_lang"] == "en"
        assert turn["tts_fallback"] == 0
    finally:
        await orchestrator.shutdown()
        store.close()


async def test_set_language_emits_change_event(settings, tmp_path: Path) -> None:
    emitted: list[tuple[str, dict[str, Any]]] = []
    store = SessionStore(tmp_path / "session.sqlite3")
    orchestrator = DialogOrchestrator(
        settings,
        ProviderRegistry(),
        MetricsCollector(),
        store,
        transport=FakeTransport(),
        vad=FakeVad(),
        providers={"llm": FakeLLM(["好的。"]), "tts": FakeTTS()},
        ui_sink=lambda kind, payload, turn_id: emitted.append((kind, payload)),
    )
    snapshot = await orchestrator.set_language("fixed", "en")
    store.close()

    assert snapshot["mode"] == "fixed"
    assert snapshot["current"] == "en"
    assert any(kind == "language.changed" for kind, _ in emitted)
