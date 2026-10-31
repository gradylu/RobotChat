"""T-4.3 ~ T-4.7 编排层打断单测（C-03、C-05、C-06、C-08、C-09、C-10、C-72）。

覆盖：打断执行序列（停播 / 取消 / 归档 / 递增 turn_id / 回 LISTENING）、
已播报片段归档与 `spoken_ratio`、未播出文本只进事件日志、ASR 热会话复用、
参数热更新，以及连续 20 次打断不错乱。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from robotchat.audio.barge_in import BargeInDecision
from robotchat.audio.frame import AudioChunk, AudioFrame
from robotchat.config.loader import load_settings
from robotchat.core.orchestrator import DialogOrchestrator
from robotchat.core.state_machine import State
from robotchat.dialog.session import SessionStore
from robotchat.observability.metrics import BARGE_IN, MetricsCollector
from robotchat.providers.base import (
    ASREvent,
    ChatRequest,
    LLMCapabilities,
    LLMEvent,
    TTSCapabilities,
    TTSRequest,
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

    async def open(self) -> None:
        return None

    def capture(self):
        async def generator():
            for seq in range(self.frames):
                yield AudioFrame(
                    data=b"\x00\x00" * FRAME,
                    sample_rate=RATE,
                    seq=seq,
                    ts_device=seq * 0.02,
                )
                await asyncio.sleep(0.01)

        return generator()

    async def play(self, chunk: AudioChunk) -> None:
        self.played.append(chunk)

    async def flush(self) -> None:
        self.flush_count += 1

    async def close(self) -> None:
        return None


class FakeVad:
    name = "fake_vad"

    def process(self, frame: AudioFrame) -> None:
        return None

    def reset(self) -> None:
        return None


class FakeASRStream:
    """播报期热流：打开后只产出 partial，关闭后才产出 final。"""

    name = "fake_asr"

    def __init__(self, text: str = "你好") -> None:
        self.text = text
        self.frames = 0
        self.closed = asyncio.Event()
        self.opened = 0

    async def open_stream(
        self, session_id: str, cancel: Any, lang: str = ""
    ) -> FakeASRStream:
        del session_id, cancel, lang
        self.opened += 1
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


class GatedLLM:
    """全部 token 立即产出、`done` 前挂起，制造「已生成但未入队」的尾部文本。"""

    name = "gated_llm"
    capabilities = LLMCapabilities(streaming=True, tool_call=False)

    def __init__(self, pieces: list[str]) -> None:
        self.pieces = pieces
        self.gate = asyncio.Event()
        self.started = asyncio.Event()

    def release(self) -> None:
        self.gate.set()

    async def chat_stream(
        self, req: ChatRequest, cancel: Any
    ) -> AsyncIterator[LLMEvent]:
        del req, cancel
        self.started.set()
        for piece in self.pieces:
            yield LLMEvent(type="token", text=piece)
        # 尾部无标点句仅在整个流结束后才入队（`splitter.flush`），此处挂起制造未播文本
        await self.gate.wait()
        yield LLMEvent(type="done")


class ImmediateLLM:
    name = "immediate_llm"
    capabilities = LLMCapabilities(streaming=True, tool_call=False)

    def __init__(self, text: str = "收到。") -> None:
        self.text = text
        self.calls = 0

    async def chat_stream(
        self, req: ChatRequest, cancel: Any
    ) -> AsyncIterator[LLMEvent]:
        del req, cancel
        self.calls += 1
        yield LLMEvent(type="token", text=self.text)
        yield LLMEvent(type="done")


class SlowTTS:
    """每块之间留出播报窗口，保证测试能稳定在 SPEAKING 状态触发打断。"""

    name = "slow_tts"
    capabilities = TTSCapabilities(sample_rate=RATE)

    def __init__(self, chunks: int = 3, interval_s: float = 0.02) -> None:
        self.chunks = chunks
        self.interval_s = interval_s
        self.requests: list[TTSRequest] = []

    async def synthesize_stream(
        self, req: TTSRequest, cancel: Any
    ) -> AsyncIterator[AudioChunk]:
        del cancel
        self.requests.append(req)
        for _ in range(self.chunks):
            await asyncio.sleep(self.interval_s)
            yield AudioChunk(data=b"\x00\x00" * FRAME, sample_rate=RATE)


def _settings(tmp_path: Path, *, wakeword: bool = False):
    return load_settings(
        config_dir=REPO_CONFIG,
        data_dir=tmp_path,
        environ={
            "ROBOTCHAT__RESILIENCE__OFFLINE_PROBE_ON_START": "false",
            "ROBOTCHAT__STATE__HEARTBEAT_MS": "5000",
            "ROBOTCHAT__PROVIDERS__ASR__FALLBACK": "",
            "ROBOTCHAT__PROVIDERS__LLM__FALLBACK": "",
            "ROBOTCHAT__PROVIDERS__TTS__FALLBACK": "",
            "ROBOTCHAT__PROVIDERS__WAKEWORD__ENABLED": "true" if wakeword else "false",
        },
    )


async def _wait_until(predicate: Any, timeout: float = 3.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.005)
    raise AssertionError("等待条件超时")


async def test_barge_in_stops_playback_and_archives_spoken_text(tmp_path: Path) -> None:
    """打断全流程：先停声 → 取消 → 归档已播片段 → 递增 turn_id → 回 LISTENING。"""
    emitted: list[tuple[str, dict[str, Any]]] = []
    settings = _settings(tmp_path)
    transport = FakeTransport()
    asr = FakeASRStream()
    llm = GatedLLM(["第一句。", "第二句。", "第三句很长没有结束"])
    tts = SlowTTS()
    store = SessionStore(tmp_path / "session.sqlite3")
    metrics = MetricsCollector()
    orchestrator = DialogOrchestrator(
        settings,
        ProviderRegistry(),
        metrics,
        store,
        transport=transport,
        vad=FakeVad(),
        providers={"asr": asr, "llm": llm, "tts": tts},
        ui_sink=lambda kind, payload, turn_id: emitted.append((kind, payload)),
    )

    await orchestrator.start()
    try:
        task = asyncio.create_task(orchestrator.handle_text("你好"))

        def ready() -> DialogOrchestrator | None:
            turn = orchestrator._current_turn
            if (
                turn is not None
                and len(turn.spoken_fragments) >= 2
                and "第三句" in turn.assistant_text
                and orchestrator._asr_stream is not None
                and transport.played
            ):
                return orchestrator
            return None

        await _wait_until(ready)
        old_turn = orchestrator._current_turn
        assert old_turn is not None
        stream_before = orchestrator._asr_stream

        orchestrator._maybe_start_barge_in(
            BargeInDecision(accepted=True, trigger="energy", energy_db=-18.0)
        )
        barge_task = orchestrator._barge_task
        assert barge_task is not None
        await asyncio.wait_for(barge_task, timeout=2.0)

        assert old_turn.status == "interrupted"
        assert 0.0 < old_turn.spoken_ratio < 1.0
        assert orchestrator.machine.state == State.LISTENING
        # ① 先停声：播放队列被 flush（C-03 / C-06）
        assert transport.flush_count >= 1
        # ② 递增 turn_id，新轮次承接打断后的话语
        assert orchestrator.turns.current_turn_id == old_turn.turn_id + 1
        # ⑦ 复用热 ASR 会话，不重建连接（T-4.4）
        assert orchestrator._asr_stream is stream_before

        snapshot = orchestrator.barge_in_snapshot()
        last = snapshot["last"]
        assert last is not None
        assert {"flush", "cancel", "archive", "turn", "resume"} <= set(last["stage_latency"])
        assert BARGE_IN in metrics.snapshot()
        assert any(kind == "barge_in.accepted" for kind, _ in emitted)

        records = store.turns("default")
        assert records[0]["status"] == "interrupted"
        assert records[0]["barge_in"] == 1
        # 只归档已送入播放队列的片段，未播出内容不进入记忆（T-4.5 / C-72）
        assert records[0]["assistant_text"] == "第一句。第二句。"
        assert 0.0 < records[0]["spoken_ratio"] < 1.0

        event_types = {event["type"] for event in store.events("default")}
        assert "barge_in.dropped_text" in event_types
        dropped = next(
            event for event in store.events("default") if event["type"] == "barge_in.dropped_text"
        )
        assert dropped["payload"]["text"] == "第三句很长没有结束"

        # 放行旧轮次，确保取消后不再产生幽灵播报
        llm.release()
        await asyncio.wait_for(task, timeout=2.0)
        assert orchestrator.machine.state == State.LISTENING
        assert transport.played  # 首句确实播出过
    finally:
        llm.release()
        await orchestrator.shutdown()
        store.close()


async def test_set_barge_in_hot_update_and_rejects_invalid(tmp_path: Path) -> None:
    """T-4.6 / C-57：参数热更新立即生效，非法值被拒。"""
    settings = _settings(tmp_path)
    store = SessionStore(tmp_path / "session.sqlite3")
    orchestrator = DialogOrchestrator(
        settings,
        ProviderRegistry(),
        MetricsCollector(),
        store,
        transport=FakeTransport(),
        vad=FakeVad(),
        providers={},
    )
    try:
        snapshot = await orchestrator.set_barge_in(
            {"threshold_boost_db": 10.0, "confirm_frames": 2, "guard_ms": 50}
        )
        assert snapshot["threshold_boost_db"] == 10.0
        assert snapshot["confirm_frames"] == 2
        assert snapshot["guard_ms"] == 50
        assert orchestrator.settings.barge_in.guard_ms == 50

        with pytest.raises(ValidationError):
            await orchestrator.set_barge_in({"confirm_frames": 0})

        disabled = await orchestrator.set_barge_in({"mode": "disabled"})
        assert disabled["mode"] == "disabled"
        assert orchestrator.barge.enabled is False
    finally:
        store.close()


async def test_twenty_consecutive_barge_ins_keep_turns_consistent(tmp_path: Path) -> None:
    """T-4.7 / C-08：连续 20 次打断，轮次单调递增且每次都被正确归档。"""
    settings = _settings(tmp_path)
    store = SessionStore(tmp_path / "session.sqlite3")
    orchestrator = DialogOrchestrator(
        settings,
        ProviderRegistry(),
        MetricsCollector(),
        store,
        transport=FakeTransport(),
        vad=FakeVad(),
        providers={
            "asr": FakeASRStream(),
            "llm": ImmediateLLM("收到。"),
            "tts": SlowTTS(chunks=2, interval_s=0.015),
        },
    )

    await orchestrator.start()
    try:
        assert orchestrator.machine.state == State.ACTIVE_IDLE
        await orchestrator._on_speech_start()
        assert orchestrator.machine.state == State.LISTENING

        for index in range(20):
            turn_id = orchestrator.turns.current_turn_id
            assert turn_id is not None
            await orchestrator._on_asr_event(
                ASREvent(type="final", text=f"第{index + 1}次提问", lang="zh"), turn_id
            )
            await _wait_until(lambda: orchestrator.machine.state == State.SPEAKING)

            await orchestrator._on_barge_in(
                BargeInDecision(accepted=True, trigger="manual", energy_db=-12.0)
            )
            assert orchestrator.machine.state == State.LISTENING
            assert orchestrator.turns.current_turn_id == turn_id + 1

        records = store.turns("default")
        interrupted = [item for item in records if item["status"] == "interrupted"]
        assert len(interrupted) == 20
        assert all(item["barge_in"] == 1 for item in interrupted)
        assert all(item["assistant_text"] for item in interrupted)
        # turn_id 单调递增，无重复 / 错位（C-08）
        assert [item["seq"] for item in interrupted] == list(range(1, 21))
    finally:
        await orchestrator.shutdown()
        store.close()
