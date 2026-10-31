"""播放契约与 flush 策略测试（T-1A.5 / C-163）。"""

from __future__ import annotations

import numpy as np
import pytest

from robotchat.audio.adapters.base_ros_audio import BaseRosAudioAdapter, decode_pcm
from robotchat.audio.buffer import float_to_pcm16
from robotchat.audio.frame import AudioChunk
from robotchat.config.schema import DeviceProfileSchema

from .conftest import BASE_PROFILE, FakeBackend, StepClock, flush_profile, make_profile


def _adapter(profile: DeviceProfileSchema, backend: FakeBackend) -> BaseRosAudioAdapter:
    return BaseRosAudioAdapter(profile, backend=backend)


def _chunk(duration_ms: float = 200.0, rate: int = 16000) -> AudioChunk:
    count = int(rate * duration_ms / 1000)
    t = np.arange(count, dtype=np.float64) / rate
    tone = 0.5 * np.sin(2.0 * np.pi * 440.0 * t)
    return AudioChunk(data=float_to_pcm16(tone), sample_rate=rate, channels=1)


async def test_play_converts_and_emits_to_profile_topic() -> None:
    """内部 16kHz 单声道 → Profile 24kHz 双声道，负载长度与声道正确。"""
    profile = make_profile(BASE_PROFILE)
    backend = FakeBackend()
    adapter = _adapter(profile, backend)
    await adapter.open()

    await adapter.playback.play(_chunk())
    await adapter.close()

    spec, payload = backend.published[-1]
    assert spec.topic == "/contract/speaker"
    assert spec.msg_type == "audio_common_msgs/msg/AudioData"
    assert spec.field == "data"
    assert len(payload) == 4800 * 2 * 2  # 4800 帧 × 2 声道 × 2 字节
    samples = decode_pcm(payload, 16, 2)
    assert samples.shape == (4800, 2)
    assert np.allclose(samples[:, 0], samples[:, 1], atol=1e-4)
    assert adapter.played_chunks == 1


async def test_play_before_open_is_rejected() -> None:
    profile = make_profile(BASE_PROFILE)
    adapter = _adapter(profile, FakeBackend())
    with pytest.raises(RuntimeError):
        await adapter.playback.play(_chunk())


@pytest.mark.parametrize("kind", ["service", "topic_msg", "stream_stop", "volume_to_zero"])
async def test_flush_strategies_within_budget(kind: str) -> None:
    """四种 flush 策略均调用正确接口，且耗时可计入 ≤ 100ms 预算。"""
    profile = flush_profile(kind)
    backend = FakeBackend()
    adapter = BaseRosAudioAdapter(profile, backend=backend, clock=StepClock(0.05))
    await adapter.open()

    await adapter.playback.flush()

    assert adapter.flush_count == 1
    assert adapter.last_flush_ms == pytest.approx(50.0)
    assert adapter.last_flush_ms <= 100.0

    if kind == "service":
        assert [call.service for call in backend.service_calls] == ["/contract/stop"]
        assert not backend.published
    elif kind == "topic_msg":
        spec, payload = backend.published[-1]
        assert spec.topic == "/contract/stop_cmd"
        assert spec.msg_type == "std_msgs/msg/Empty"
        assert payload == b""
    elif kind == "stream_stop":
        spec, payload = backend.published[-1]
        assert spec.topic == "/contract/speaker"
        assert payload == b""
    else:
        spec, value = backend.float_published[-1]
        assert spec.topic == "/contract/volume"
        assert value == 0.0

    await adapter.close()


async def test_volume_restored_after_volume_flush() -> None:
    """volume_to_zero flush 后第一次播放先恢复原音量（避免一直静音）。"""
    profile = flush_profile("volume_to_zero")
    backend = FakeBackend()
    adapter = BaseRosAudioAdapter(profile, backend=backend, clock=StepClock(0.01))
    await adapter.open()

    await adapter.playback.flush()
    await adapter.playback.play(_chunk(20.0))
    await adapter.close()

    levels = [value for _, value in backend.float_published]
    assert levels[0] == 0.0
    assert levels[-1] == 1.0
    assert backend.published  # 恢复音量后确实发布了音频


async def test_set_volume_validates_profile_range() -> None:
    profile = make_profile(BASE_PROFILE)
    backend = FakeBackend()
    adapter = _adapter(profile, backend)
    await adapter.open()

    await adapter.playback.set_volume(0.4)
    await adapter.close()
    assert backend.float_published[-1][1] == 0.4

    profile_no_volume = make_profile(
        BASE_PROFILE,
        profile="contract_no_volume",
        capabilities={**BASE_PROFILE["capabilities"], "volume_control": False},
        volume=None,
    )
    adapter2 = _adapter(profile_no_volume, FakeBackend())
    await adapter2.open()
    with pytest.raises(RuntimeError):
        await adapter2.playback.set_volume(0.5)
    await adapter2.close()
