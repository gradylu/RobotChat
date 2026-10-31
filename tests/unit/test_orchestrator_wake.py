"""T-3A.4 / T-3A.5 / T-3A.9 编排器唤醒闭环单测：待机唤醒、活跃窗口超时、常开聆听与待机结果。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import numpy as np

from robotchat.audio.frame import AudioChunk, AudioFrame
from robotchat.config.loader import load_settings
from robotchat.core.orchestrator import DialogOrchestrator
from robotchat.core.state_machine import State
from robotchat.dialog.session import SessionStore
from robotchat.observability.metrics import (
    ACTIVE_WINDOW_EXPIRED,
    OPEN_EAR_CLOUD_AUDIO,
    STANDBY_RESULT_CACHED,
    WAKE_COUNT,
    WAKE_LATENCY,
    MetricsCollector,
)
from robotchat.providers.base import (
    ASRCapabilities,
    ASREvent,
    TTSCapabilities,
    TTSRequest,
)
from robotchat.providers.kws.openwakeword import OpenWakeWordKws
from robotchat.providers.registry import ProviderRegistry

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"
RATE = 16000
FRAME = 320
KWS_FRAME_SAMPLES = RATE * 80 // 1000


class SilentTransport:
    name = "silent"

    def __init__(self) -> None:
        self.played: list[AudioChunk] = []
        self.flush_count = 0

    async def open(self) -> None:
        return None

    def capture(self):
        async def generator():
            while False:  # pragma: no cover - 永不产出，唤醒由测试手动喂帧
                yield AudioFrame(data=b"", sample_rate=RATE)

        return generator()

    async def play(self, chunk: AudioChunk) -> None:
        self.played.append(chunk)

    async def flush(self) -> None:
        self.flush_count += 1

    async def close(self) -> None:
        return None


class NoopVad:
    name = "noop_vad"

    def process(self, frame: AudioFrame) -> None:
        return None

    def reset(self) -> None:
        return None


class AlwaysHighInference:
    def __init__(self, phrase: str = "你好机器人", score: float = 0.95) -> None:
        self.phrase = phrase
        self.score = score

    def __call__(self, pcm: np.ndarray) -> dict[str, float]:
        del pcm
        return {self.phrase: self.score}


class WakeASRStream:
    def __init__(self) -> None:
        self.frames = 0
        self.closed = asyncio.Event()
        self.is_closed = False

    async def feed(self, frame: AudioFrame) -> None:
        del frame
        self.frames += 1

    def events(self) -> AsyncIterator[ASREvent]:
        async def generator() -> AsyncIterator[ASREvent]:
            await self.closed.wait()
            yield ASREvent(type="final", text="", lang="zh")

        return generator()

    async def close(self) -> None:
        self.is_closed = True
        self.closed.set()


class WakeASR:
    name = "fake_asr"
    capabilities = ASRCapabilities(
        streaming=True, languages=frozenset({"zh"}), sample_rates=(RATE,)
    )

    def __init__(self) -> None:
        self.stream: WakeASRStream | None = None
        self.opens = 0

    async def open_stream(
        self, session_id: str, cancel: Any, lang: str = ""
    ) -> WakeASRStream:
        del session_id, cancel, lang
        self.stream = WakeASRStream()
        self.opens += 1
        return self.stream


class FakeTTS:
    name = "fake_tts"
    capabilities = TTSCapabilities(sample_rate=RATE)

    def __init__(self) -> None:
        self.requests: list[TTSRequest] = []

    async def synthesize_stream(
        self, req: TTSRequest, cancel: Any
    ) -> AsyncIterator[AudioChunk]:
        del cancel
        self.requests.append(req)
        await asyncio.sleep(0)
        yield AudioChunk(data=b"\x00\x00" * FRAME, sample_rate=RATE)


def _settings(tmp_path: Path, **env: str):
    base = {
        "ROBOTCHAT__RESILIENCE__OFFLINE_PROBE_ON_START": "false",
        "ROBOTCHAT__STATE__HEARTBEAT_MS": "5000",
        "ROBOTCHAT__PROVIDERS__ASR__FALLBACK": "",
        "ROBOTCHAT__PROVIDERS__LLM__FALLBACK": "",
        "ROBOTCHAT__PROVIDERS__TTS__FALLBACK": "",
    }
    base.update(env)
    return load_settings(config_dir=REPO_CONFIG, data_dir=tmp_path, environ=base)


def _kws_frame() -> AudioFrame:
    return AudioFrame(data=b"\x00\x00" * KWS_FRAME_SAMPLES, sample_rate=RATE)


def _wake_kws() -> OpenWakeWordKws:
    return OpenWakeWordKws(
        {"wake_phrases": ["你好机器人"], "threshold": 0.5},
        inference=AlwaysHighInference(),
    )


def _build(settings, tmp_path: Path, transport, providers) -> tuple[DialogOrchestrator, Any]:
    store = SessionStore(tmp_path / "session.sqlite3")
    orchestrator = DialogOrchestrator(
        settings,
        ProviderRegistry(),
        MetricsCollector(),
        store,
        transport=transport,
        vad=NoopVad(),
        providers=providers,
    )
    return orchestrator, store


async def test_wake_closed_loop_reaches_active_idle(tmp_path: Path) -> None:
    settings = _settings(tmp_path, ROBOTCHAT__PROVIDERS__WAKEWORD__ACTIVE_WINDOW_MS="5000")
    transport = SilentTransport()
    tts = FakeTTS()
    asr = WakeASR()
    orchestrator, store = _build(
        settings, tmp_path, transport, {"kws": _wake_kws(), "asr": asr, "tts": tts}
    )

    await orchestrator.start()
    try:
        assert orchestrator.machine.state == State.IDLE
        assert orchestrator._kws is not None and orchestrator._kws.running

        await orchestrator._kws.feed(_kws_frame())
        await asyncio.sleep(0.1)

        assert orchestrator.machine.state == State.ACTIVE_IDLE
        # KWS 进入活跃态即停止（INV-9），ASR 已预连接
        assert orchestrator._kws is not None and orchestrator._kws.running is False
        assert asr.stream is not None
        assert [req.text for req in tts.requests][-1] == "我在"
        assert orchestrator.metrics.counter(WAKE_COUNT) == 1
        assert WAKE_LATENCY in orchestrator.metrics.snapshot()

        snapshot = orchestrator.wake_snapshot()
        assert snapshot["state"] == "ACTIVE_IDLE"
        assert snapshot["active_window_remaining_s"] is not None
        assert snapshot["active_window_remaining_s"] > 0
        assert snapshot["pending_standby_results"] == 0
    finally:
        await orchestrator.shutdown()
        store.close()


async def test_active_window_timeout_releases_asr(tmp_path: Path) -> None:
    settings = _settings(tmp_path, ROBOTCHAT__PROVIDERS__WAKEWORD__ACTIVE_WINDOW_MS="100")
    transport = SilentTransport()
    asr = WakeASR()
    orchestrator, store = _build(
        settings, tmp_path, transport, {"kws": _wake_kws(), "asr": asr, "tts": FakeTTS()}
    )

    await orchestrator.start()
    try:
        await orchestrator._kws.feed(_kws_frame())
        await asyncio.sleep(0.1)
        assert orchestrator.machine.state == State.ACTIVE_IDLE

        stream = asr.stream
        assert stream is not None
        await asyncio.sleep(0.4)

        assert orchestrator.machine.state == State.IDLE
        assert orchestrator.metrics.counter(ACTIVE_WINDOW_EXPIRED) == 1
        assert orchestrator._asr_stream is None
        assert stream.is_closed is True
        assert orchestrator._kws is not None and orchestrator._kws.running
    finally:
        await orchestrator.shutdown()
        store.close()


async def test_open_ear_stays_active_without_window_timeout(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        ROBOTCHAT__PROVIDERS__WAKEWORD__ENABLED="false",
        ROBOTCHAT__PROVIDERS__WAKEWORD__ACTIVE_WINDOW_MS="100",
    )
    transport = SilentTransport()
    asr = WakeASR()
    orchestrator, store = _build(
        settings, tmp_path, transport, {"asr": asr, "tts": FakeTTS()}
    )

    await orchestrator.start()
    try:
        assert orchestrator.machine.state == State.ACTIVE_IDLE
        assert asr.stream is not None
        await asyncio.sleep(0.3)

        # 关闭唤醒词：不启用窗口超时，ASR 保持可用（FR-14.8）
        assert orchestrator.machine.state == State.ACTIVE_IDLE
        assert asr.stream is not None and asr.stream.is_closed is False

        result = await orchestrator.announce_task_result("充电完成")
        assert result["announced"] is True
        assert result["reason"] == "open_ear"
        assert len(orchestrator._pending_standby) == 0
    finally:
        await orchestrator.shutdown()
        store.close()


async def test_open_ear_cloud_asr_counts_uploaded_audio(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        ROBOTCHAT__PROVIDERS__WAKEWORD__ENABLED="false",
        ROBOTCHAT__PROVIDERS__WAKEWORD__OPEN_EAR__CLOUD_ASR_ACK="true",
        ROBOTCHAT__PROVIDERS__ASR__ACTIVE="dashscope_stream",
    )
    transport = SilentTransport()
    asr = WakeASR()
    orchestrator, store = _build(
        settings, tmp_path, transport, {"asr": asr, "tts": FakeTTS()}
    )

    await orchestrator.start()
    try:
        assert orchestrator._asr_cloud is True
        await orchestrator._on_frame(AudioFrame(data=b"\x00\x00" * FRAME, sample_rate=RATE), None)
        assert orchestrator.metrics.counter(OPEN_EAR_CLOUD_AUDIO) > 0
    finally:
        await orchestrator.shutdown()
        store.close()


async def test_standby_result_cached_then_flushed_on_wake(tmp_path: Path) -> None:
    settings = _settings(tmp_path, ROBOTCHAT__PROVIDERS__WAKEWORD__ACTIVE_WINDOW_MS="5000")
    transport = SilentTransport()
    tts = FakeTTS()
    orchestrator, store = _build(
        settings, tmp_path, transport, {"kws": _wake_kws(), "asr": WakeASR(), "tts": tts}
    )

    await orchestrator.start()
    try:
        assert orchestrator.machine.state == State.IDLE
        result = await orchestrator.announce_task_result("充电完成")
        assert result["cached"] is True
        assert orchestrator.metrics.counter(STANDBY_RESULT_CACHED) == 1
        assert len(transport.played) > 0  # 待机态提示音
        assert "充电完成" not in [req.text for req in tts.requests]

        await orchestrator._kws.feed(_kws_frame())
        await asyncio.sleep(0.1)

        assert orchestrator.machine.state == State.ACTIVE_IDLE
        assert len(orchestrator._pending_standby) == 0
        assert "充电完成" in [req.text for req in tts.requests]
    finally:
        await orchestrator.shutdown()
        store.close()
