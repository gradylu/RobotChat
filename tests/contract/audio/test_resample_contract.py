"""重采样 / 声道转换保真契约测试（T-1A.5 / C-164）：THD+N ≤ 1%，抗混叠。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import numpy as np

from robotchat.audio.adapters.base_ros_audio import BaseRosAudioAdapter, decode_pcm
from robotchat.audio.buffer import float_to_pcm16
from robotchat.audio.frame import AudioChunk, AudioFrame

from .conftest import (
    BASE_PROFILE,
    SECOND_PROFILE,
    FakeBackend,
    make_profile,
    sine_payload,
    thd_plus_n,
)

FRAME_MS = 20
FRAME_SAMPLES_24K = 480


async def _take(
    stream: AsyncIterator[AudioFrame], count: int, timeout_s: float
) -> list[AudioFrame]:
    frames: list[AudioFrame] = []

    async def collect() -> None:
        async for frame in stream:
            frames.append(frame)
            if len(frames) >= count:
                return

    await asyncio.wait_for(collect(), timeout=timeout_s)
    return frames


async def test_downsample_24k_stereo_to_16k_thd_within_1pct() -> None:
    """24kHz 双声道 1kHz 正弦 → 内部 16kHz 单声道，THD+N ≤ 1%。"""
    profile = make_profile(BASE_PROFILE)
    backend = FakeBackend()
    adapter = BaseRosAudioAdapter(profile, backend=backend, queue_frames=200)
    await adapter.open()

    payloads = 100  # 2s
    for index in range(payloads):
        backend.emit(
            sine_payload(
                24000,
                2,
                16,
                FRAME_MS,
                freq=1000.0,
                amplitude=0.5,
                start_index=index * FRAME_SAMPLES_24K,
            ),
            stamp=index * FRAME_MS * 1_000_000,
        )
    await asyncio.sleep(0)

    frames = await _take(adapter.capture.stream(), payloads, timeout_s=10.0)
    await adapter.close()

    assert len(frames) == payloads
    # 跳过首尾各 0.2s，避开重采样滤波器边缘
    signal = np.concatenate(
        [decode_pcm(frame.data, 16, 1) for frame in frames[10:-10]]
    ).astype(np.float64)
    thd = thd_plus_n(signal, 16000, 1000.0)
    assert thd <= 0.01, f"THD+N={thd:.4%} 超出 1% 预算"

    # 幅度保真：0.5 幅度正弦的 RMS ≈ 0.3536
    rms = float(np.sqrt(np.mean(signal**2)))
    assert 0.30 <= rms <= 0.40


async def test_channel_average_stereo_to_mono() -> None:
    """双声道输入按均值合并为单声道（音量减半而非取单边）。"""
    profile = make_profile(BASE_PROFILE)
    backend = FakeBackend()
    adapter = BaseRosAudioAdapter(profile, backend=backend, queue_frames=50)
    await adapter.open()

    samples = 480
    t = np.arange(samples, dtype=np.float64) / 24000
    mono = 0.8 * np.sin(2.0 * np.pi * 1000.0 * t)
    stereo = np.stack([mono, np.zeros_like(mono)], axis=1)
    payload = np.round(np.clip(stereo, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    backend.emit(payload, stamp=0)
    await asyncio.sleep(0)

    frames = await _take(adapter.capture.stream(), 1, timeout_s=5.0)
    await adapter.close()

    frame = decode_pcm(frames[0].data, 16, 1)
    peak = float(np.max(np.abs(frame)))
    assert 0.3 <= peak <= 0.5  # 0.8 / 2 = 0.4


async def test_upsample_on_playback_16k_to_48k_mono32() -> None:
    """播放侧 16kHz → 48kHz 32bit 单声道转换，长度与位深正确。"""
    profile = make_profile(SECOND_PROFILE)
    backend = FakeBackend()
    adapter = BaseRosAudioAdapter(profile, backend=backend)
    await adapter.open()

    count = 3200  # 200ms @16kHz
    t = np.arange(count, dtype=np.float64) / 16000
    chunk = AudioChunk(data=float_to_pcm16(0.5 * np.sin(2 * np.pi * 1000 * t)), sample_rate=16000)
    await adapter.playback.play(chunk)
    await adapter.close()

    spec, payload = backend.published[-1]
    assert spec.topic == "/contract/speaker48"
    assert len(payload) == 9600 * 4  # 4800 帧 × 4 字节（32bit 单声道）
    samples = decode_pcm(payload, 32, 1)
    assert samples.shape == (9600,)
    assert np.max(np.abs(samples)) <= 0.55
