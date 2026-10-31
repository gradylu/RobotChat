"""采集契约测试（T-1A.5 / C-162）：帧率、时间戳单调、seq、断线重连、两种机型接口。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from robotchat.audio.adapters.base_ros_audio import BaseRosAudioAdapter
from robotchat.audio.frame import AudioFrame
from robotchat.config.schema import DeviceProfileSchema

from .conftest import BASE_PROFILE, SECOND_PROFILE, FakeBackend, make_profile, sine_payload

FRAME_MS = 20


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


def _capture_adapter(
    profile: DeviceProfileSchema,
    backend: FakeBackend,
    **kwargs: Any,
) -> BaseRosAudioAdapter:
    return BaseRosAudioAdapter(profile, backend=backend, **kwargs)


async def test_frame_rate_and_timestamps_30s() -> None:
    """连续 30s（虚拟时间）应产出 1500 帧，帧率偏差 ≤ 1%，时间戳严格递增。"""
    profile = make_profile(BASE_PROFILE)
    backend = FakeBackend()
    adapter = _capture_adapter(profile, backend, queue_frames=2000)
    await adapter.open()

    payload = sine_payload(24000, 2, 16, FRAME_MS, amplitude=0.1)
    # 1500 × 20ms = 30s；时间戳以 Profile 声明的 ns 为单位递增
    for index in range(1500):
        backend.emit(payload, stamp=index * FRAME_MS * 1_000_000)
    await asyncio.sleep(0)

    frames = await _take(adapter.capture.stream(), 1500, timeout_s=10.0)
    await adapter.close()

    assert len(frames) == 1500
    expected_seconds = 30.0
    actual_seconds = len(frames) * FRAME_MS / 1000.0
    assert abs(actual_seconds - expected_seconds) / expected_seconds <= 0.01

    assert [frame.seq for frame in frames] == list(range(1, 1501))
    assert all(frame.sample_rate == 16000 for frame in frames)
    assert all(frame.channels == 1 for frame in frames)
    assert all(len(frame.data) == 640 for frame in frames)  # 320 samples × 16bit

    stamps = [frame.ts_device for frame in frames]
    assert stamps == sorted(stamps)
    assert stamps[-1] - stamps[0] >= 30.0 * 0.99
    diffs = [b - a for a, b in zip(stamps[:-1], stamps[1:], strict=True)]
    assert max(diffs) <= 0.0201


async def test_timestamp_gap_reveals_lost_audio() -> None:
    """采集端丢包（时间戳跳变）可被检出；正常段 seq 连续。"""
    profile = make_profile(BASE_PROFILE)
    backend = FakeBackend()
    adapter = _capture_adapter(profile, backend, queue_frames=200)
    await adapter.open()

    payload = sine_payload(24000, 2, 16, FRAME_MS, amplitude=0.1)
    indices = [*range(50), *range(60, 110)]
    for index in indices:
        backend.emit(payload, stamp=index * FRAME_MS * 1_000_000)
    await asyncio.sleep(0)

    frames = await _take(adapter.capture.stream(), len(indices), timeout_s=5.0)
    await adapter.close()

    pairs = list(zip(frames[:-1], frames[1:], strict=True))
    gap = max(later.ts_device - earlier.ts_device for earlier, later in pairs)
    assert gap >= 0.2  # 丢失 10 帧
    assert all(later.seq == earlier.seq + 1 for earlier, later in pairs)


async def test_queue_overflow_counts_dropped_frames() -> None:
    """背压有界：队列满时丢弃最旧帧并计数，不阻塞 DDS 回调。"""
    profile = make_profile(BASE_PROFILE)
    backend = FakeBackend()
    adapter = _capture_adapter(profile, backend, queue_frames=30)
    await adapter.open()

    payload = sine_payload(24000, 2, 16, FRAME_MS, amplitude=0.1)
    for index in range(60):
        backend.emit(payload, stamp=index * FRAME_MS * 1_000_000)
    await asyncio.sleep(0)

    assert adapter.snapshot()["dropped_frames"] == 30
    frames = await _take(adapter.capture.stream(), 30, timeout_s=5.0)
    await adapter.close()
    assert len(frames) == 30


async def test_capture_timeout_rebuilds_subscription() -> None:
    """采集停滞超过阈值 → 重建订阅并计数 reconnects（FR-16.8）。"""
    profile = make_profile(BASE_PROFILE)
    backend = FakeBackend()
    adapter = _capture_adapter(profile, backend, capture_timeout_s=0.05)
    await adapter.open()

    frames: list[AudioFrame] = []

    async def consume() -> None:
        async for frame in adapter.capture.stream():
            frames.append(frame)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.25)  # 触发多次超时重建
    assert adapter.reconnects >= 1
    assert len(backend.subscriptions) >= 2

    backend.emit(sine_payload(24000, 2, 16, FRAME_MS), stamp=0.0)
    await asyncio.sleep(0.05)
    assert frames

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await adapter.close()


async def test_second_profile_with_nested_fields() -> None:
    """不同采样率 / 位深 / 消息结构（嵌套字段）的 Profile 同样跑通采集。"""
    profile = make_profile(SECOND_PROFILE)
    backend = FakeBackend()
    adapter = _capture_adapter(profile, backend, queue_frames=200)
    await adapter.open()

    spec = backend.subscriptions[0]
    assert spec.topic == "/contract/mic48"
    assert spec.msg_type == "vendor_audio_msgs/msg/RawAudio"
    assert spec.fields == {"data": "audio.data"}
    assert spec.qos["depth"] == 40
    assert spec.qos["reliability"] == "reliable"

    payload = sine_payload(48000, 1, 32, FRAME_MS)
    for _ in range(25):
        backend.emit(payload)
    await asyncio.sleep(0)

    frames = await _take(adapter.capture.stream(), 25, timeout_s=5.0)
    await adapter.close()

    assert len(frames) == 25
    assert frames[0].sample_rate == 16000
    assert len(frames[0].data) == 640
    # arrival 时间戳退化为到达时间，仍须单调
    stamps = [frame.ts_device for frame in frames]
    assert stamps == sorted(stamps)
