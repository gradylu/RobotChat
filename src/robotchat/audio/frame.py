"""音频基本数据结构（契约层，供采集 / 播放 / VAD / KWS / ASR 共用）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AudioFrame:
    """一帧采集音频。"""

    data: bytes
    sample_rate: int
    channels: int = 1
    bit_depth: int = 16
    ts_device: float = 0.0  # 设备时间戳，单调递增，供回声剔除与打断判定
    seq: int = 0            # 序号，供丢帧检测与回声对齐


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """一段待播放音频。"""

    data: bytes
    sample_rate: int
    channels: int = 1
    bit_depth: int = 16
