"""T-1.5 VAD 单测：自适应噪声底、起音多帧确认、尾点检测、最大时长保护。"""

from __future__ import annotations

import numpy as np
import pytest

from robotchat.audio.buffer import float_to_pcm16
from robotchat.audio.frame import AudioFrame
from robotchat.audio.vad import AdaptiveNoiseFloor, EnergyVad, SileroVad
from robotchat.config.schema import VadSettings

FRAME = 320
RATE = 16000


def _settings(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = VadSettings(backend="energy", silence_ms=200).model_dump()
    base.update(overrides)
    return base


def _frame(samples: np.ndarray, seq: int) -> AudioFrame:
    return AudioFrame(
        data=float_to_pcm16(samples),
        sample_rate=RATE,
        channels=1,
        seq=seq,
        ts_device=seq * 0.02,
    )


def _tone(frames: int, amplitude: float = 0.2, freq: float = 440.0) -> list[np.ndarray]:
    signal = amplitude * np.sin(2 * np.pi * freq * np.arange(FRAME * frames) / RATE)
    return [signal[i * FRAME : (i + 1) * FRAME] for i in range(frames)]


def _silence(frames: int) -> list[np.ndarray]:
    return [np.zeros(FRAME, dtype=np.float32) for _ in range(frames)]


def _run(vad: EnergyVad, frames: list[np.ndarray]) -> list[tuple[int, str]]:
    events: list[tuple[int, str]] = []
    for index, samples in enumerate(frames):
        event = vad.process(_frame(samples, index))
        if event is not None:
            events.append((index, event.type))
    return events


def test_adaptive_noise_floor_uses_low_percentile() -> None:
    floor = AdaptiveNoiseFloor(window=30, percentile=10.0)
    for level in [-60.0] * 28 + [-20.0, -18.0]:
        floor.update(level)
    assert floor.floor_db == pytest.approx(-60.0)


def test_silence_produces_no_events() -> None:
    vad = EnergyVad(_settings())
    assert _run(vad, _silence(40)) == []


def test_steady_background_tone_is_not_speech() -> None:
    # 标定后噪声底与稳态电平一致，裕量为 0，不应误判为语音
    vad = EnergyVad(_settings())
    assert _run(vad, _tone(40, amplitude=0.01)) == []


def test_onset_confirmation_and_trailing_silence() -> None:
    vad = EnergyVad(_settings())
    frames = _silence(12) + _tone(5, amplitude=0.2) + _silence(15)
    events = _run(vad, frames)

    assert events[0] == (14, "speech_start")  # 12 帧标定 + 3 帧确认
    assert events[1] == (26, "speech_end")  # 起音后 200ms 静音（10 帧）
    assert len(events) == 2


def test_max_utterance_forces_endpoint() -> None:
    vad = EnergyVad(_settings(max_utterance_ms=100))
    events = _run(vad, _silence(12) + _tone(25, amplitude=0.2))

    ends = [index for index, kind in events if kind == "speech_end"]
    # 语音仍在继续时也必须按最长时长强制端点
    assert ends and ends[0] == 16
    assert len(events) >= 3


def test_silero_backend_requires_model_asset(tmp_path) -> None:
    with pytest.raises((FileNotFoundError, RuntimeError)):
        SileroVad({"model_path": str(tmp_path / "missing.onnx")})
