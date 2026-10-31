"""T-1.1 音频缓冲与格式转换单测。"""

from __future__ import annotations

import numpy as np
import pytest

from robotchat.audio.buffer import (
    JitterBuffer,
    RingBuffer,
    convert_channels,
    float_to_pcm16,
    pcm16_to_float,
    resample_float,
    resample_pcm16,
)
from robotchat.audio.frame import AudioFrame


def _tone(freq: float, rate: int, seconds: float, amp: float = 0.5) -> np.ndarray:
    samples = int(rate * seconds)
    t = np.arange(samples) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_pcm16_roundtrip() -> None:
    samples = _tone(1000, 16000, 0.05)
    data = float_to_pcm16(samples)
    restored = pcm16_to_float(data)
    assert restored.shape == samples.shape
    assert float(np.max(np.abs(restored - samples))) < 1e-3


def test_convert_channels_roundtrip() -> None:
    mono = np.arange(10, dtype=np.float32) / 10.0
    stereo = convert_channels(mono, 1, 2)
    assert stereo.shape == (10, 2)
    assert np.allclose(stereo[:, 0], mono)
    assert np.allclose(stereo[:, 1], mono)
    back = convert_channels(stereo, 2, 1)
    assert np.allclose(back, mono)


@pytest.mark.parametrize("src,dst", [(48000, 16000), (16000, 48000), (44100, 16000)])
def test_resample_preserves_tone_with_anti_alias(src: int, dst: int) -> None:
    tone = _tone(1000, src, 0.5)
    output = resample_float(tone, src, dst)
    reference = _tone(1000, dst, 0.5)
    length = min(output.size, reference.size)
    margin = int(dst * 0.05)
    error = output[margin : length - margin] - reference[margin : length - margin]
    ratio = float(
        np.sqrt(np.mean(error**2)) / np.sqrt(np.mean(reference[margin : length - margin] ** 2))
    )
    # T-1A.5：重采样 1kHz 正弦 THD+N ≤ 1%，禁止最近邻 / 直接抽取
    assert ratio < 0.01


def test_resample_pcm16_identity() -> None:
    data = float_to_pcm16(_tone(1000, 16000, 0.02))
    assert resample_pcm16(data, 16000, 16000) == data


def test_ring_buffer_drops_oldest() -> None:
    buffer = RingBuffer(capacity_bytes=100)
    dropped: list[AudioFrame] = []
    for seq in range(5):
        dropped.extend(buffer.push(AudioFrame(data=b"\x00" * 40, sample_rate=16000, seq=seq)))
    # 100B 容量只能容纳 2 帧（40B/帧），后 3 帧入队各挤掉一帧最旧帧
    assert len(dropped) == 3
    assert [frame.seq for frame in dropped] == [0, 1, 2]
    assert buffer.bytes_used <= 100
    remaining = buffer.pop(100)
    assert [frame.seq for frame in remaining] == [3, 4]


def test_ring_buffer_drops_newest() -> None:
    buffer = RingBuffer(capacity_bytes=100, drop_policy="newest")
    for seq in range(5):
        # newest 策略不替换旧帧，溢出帧直接丢弃
        assert buffer.push(AudioFrame(data=b"\x00" * 40, sample_rate=16000, seq=seq)) == []
    assert buffer.dropped_frames == 3
    remaining = buffer.pop(100)
    assert [frame.seq for frame in remaining] == [0, 1]


def test_ring_buffer_reports_overflow() -> None:
    calls: list[tuple[str, int]] = []
    buffer = RingBuffer(
        capacity_bytes=64, on_overflow=lambda policy, size: calls.append((policy, size))
    )
    for seq in range(4):
        buffer.push(AudioFrame(data=b"\x00" * 32, sample_rate=16000, seq=seq))
    assert calls and calls[0][0] == "oldest"


def _frame(seq: int) -> AudioFrame:
    return AudioFrame(data=b"\x00\x00", sample_rate=16000, seq=seq)


def test_jitter_buffer_reorders() -> None:
    buffer = JitterBuffer(target_depth=3)
    out: list[AudioFrame] = []
    for seq in (3, 1, 2):
        out.extend(buffer.push(_frame(seq)))
    assert [frame.seq for frame in out] == [1, 2, 3]


def test_jitter_buffer_buffer_to_target_depth() -> None:
    buffer = JitterBuffer(target_depth=3)
    out: list[AudioFrame] = []
    for seq in (1, 2, 3):
        out.extend(buffer.push(_frame(seq)))
    assert [frame.seq for frame in out] == [1, 2, 3]

    # 攒够目标深度前不输出；补齐连号后一次性吐出（避免额外延迟）
    assert buffer.push(_frame(5)) == []
    assert [frame.seq for frame in buffer.push(_frame(4))] == [4, 5]
    assert buffer.push(_frame(7)) == []
    assert [frame.seq for frame in buffer.push(_frame(6))] == [6, 7]


def test_jitter_buffer_skips_gap_and_counts_loss() -> None:
    buffer = JitterBuffer(target_depth=3)
    for seq in (1, 2, 3):
        buffer.push(_frame(seq))
    # 4/5/6 丢失：缓冲再次达到目标深度后跳过缺口，避免整条链路卡死
    buffer.push(_frame(7))
    buffer.push(_frame(8))
    out = buffer.push(_frame(9))
    assert [frame.seq for frame in out] == [7, 8, 9]
    assert buffer.lost_frames == 3


def test_jitter_buffer_flush_returns_sorted_remainder() -> None:
    buffer = JitterBuffer(target_depth=3)
    buffer.push(_frame(2))
    buffer.push(_frame(1))
    remainder = buffer.flush()
    assert [frame.seq for frame in remainder] == [1, 2]
    assert len(buffer) == 0


def test_jitter_buffer_ignores_duplicates() -> None:
    buffer = JitterBuffer(target_depth=2)
    buffer.push(_frame(1))
    buffer.push(_frame(1))
    assert buffer.duplicates == 1
