"""音频缓冲与格式转换（T-1.1）。

- PCM16 ↔ float32 转换、声道转换
- 抗混叠重采样（Kaiser 窗 sinc 多相抽取），禁止最近邻 / 直接抽取（T-1A.5）
- 有界环形缓冲（超限按策略丢弃并告警，INV-7）
- 抖动缓冲（按 seq 重排、缺帧跳过并计数，design §4）
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from math import gcd
from typing import Literal

import numpy as np

from robotchat.audio.frame import AudioFrame

_EPS = 1e-9


# ---------------------------------------------------------------- PCM 格式转换


def pcm16_to_float(data: bytes, channels: int = 1) -> np.ndarray:
    """PCM16LE → float32，单声道返回 1-D，多声道返回 (n, channels)。"""
    samples = np.frombuffer(data, dtype="<i2").astype(np.float64) / 32768.0
    if channels > 1:
        return samples.reshape(-1, channels).astype(np.float32)
    return samples.astype(np.float32)


def float_to_pcm16(samples: np.ndarray) -> bytes:
    """float32 → PCM16LE（带削顶保护）。"""
    clipped = np.clip(np.asarray(samples, dtype=np.float64), -1.0, 1.0)
    return np.round(clipped * 32767.0).astype("<i2").tobytes()


def convert_channels(samples: np.ndarray, src: int, dst: int) -> np.ndarray:
    """声道数转换：单声道复制 / 多声道取均值。"""
    if src == dst:
        return samples
    if src <= 0 or dst <= 0:
        raise ValueError(f"非法声道数：src={src}, dst={dst}")
    data = samples.reshape(-1, src) if samples.ndim == 1 else samples
    mono = data.mean(axis=1, keepdims=True)
    if dst == 1:
        return mono.ravel()
    return np.repeat(mono, dst, axis=1)


def rms(values: np.ndarray) -> float:
    if values.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(np.asarray(values, dtype=np.float64)))))


def rms_dbfs(values: np.ndarray) -> float:
    level = rms(values)
    return -120.0 if level <= _EPS else float(20.0 * np.log10(level))


# ---------------------------------------------------------------- 重采样


def _lowpass_kernel(cutoff: float, num_taps: int, beta: float = 8.6) -> np.ndarray:
    """归一化截止频率（相对上采样后 Nyquist）的 Kaiser 窗 sinc 低通。"""
    n = np.arange(num_taps, dtype=np.float64)
    center = (num_taps - 1) / 2.0
    kernel = 2.0 * cutoff * np.sinc(2.0 * cutoff * (n - center))
    kernel *= np.kaiser(num_taps, beta)
    total = kernel.sum()
    if total == 0:
        raise ValueError("低通滤波器设计失败（截止频率过低）")
    return kernel / total


def _rational_ratio(src_rate: int, dst_rate: int) -> tuple[int, int]:
    """返回 (上采样倍数, 下采样倍数)；取精确有理比，保证输出速率无漂移。"""
    common = gcd(src_rate, dst_rate)
    return dst_rate // common, src_rate // common


def _decimate_after_interpolate(
    data: np.ndarray, up: int, down: int, delay: int, kernel: np.ndarray, out_len: int
) -> np.ndarray:
    """整数比场景（up==1 或 down==1）直接卷积，C 实现，无需逐点循环。"""
    num_in, channels = data.shape
    upsampled = np.zeros((num_in * up, channels), dtype=np.float64)
    upsampled[::up] = data
    indices = delay + np.arange(out_len, dtype=np.int64) * down
    out = np.zeros((out_len, channels), dtype=np.float64)
    for ch in range(channels):
        filtered = np.convolve(upsampled[:, ch], kernel)
        valid = indices < filtered.size
        out[valid, ch] = filtered[indices[valid]]
    return out


def _polyphase_resample(
    data: np.ndarray, up: int, down: int, delay: int, kernel: np.ndarray, out_len: int
) -> np.ndarray:
    """多相分解（up/down 均 > 1 时避免超长零填充卷积）。

    output[n] = Σ_j h_p[j] · x[m_start - j]，其中 p = (n·down + delay) % up、
    m_start = (n·down + delay) // up；每个相位的子滤波器抽头数为 num_taps/up。
    """
    num_in, channels = data.shape
    out = np.zeros((out_len, channels), dtype=np.float64)
    indices = np.arange(out_len, dtype=np.int64) * down + delay
    phases = (indices % up).astype(np.int64)
    centers = indices // up
    sub_filters = [kernel[p::up] for p in range(up)]

    for n in range(out_len):
        center = int(centers[n])
        taps = sub_filters[int(phases[n])]
        # 有效抽头范围：既要 x 下标 >= 0（j <= center），也要 <= num_in-1（j >= center-num_in+1）
        j_start = max(0, center - num_in + 1)
        j_stop = min(taps.size - 1, center)
        if j_stop < j_start:
            continue
        segment = data[center - j_stop : center - j_start + 1][::-1]
        weights = taps[j_start : j_stop + 1]
        if channels == 1:
            out[n, 0] = float(np.dot(weights, segment[:, 0]))
        else:
            out[n] = weights @ segment
    return out


def resample_float(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """对 float 音频整体重采样；形状保持（1-D 或 (n, channels)）。"""
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError(f"非法采样率：src={src_rate}, dst={dst_rate}")
    if src_rate == dst_rate:
        return samples

    mono = samples.ndim == 1
    data = samples.reshape(-1, 1) if mono else samples
    num_in, channels = data.shape
    if num_in == 0:
        return samples

    up, down = _rational_ratio(src_rate, dst_rate)
    # 抗混叠截止：取两端较低 Nyquist 的 90%，换算到上采样域
    cutoff = 0.9 * min(src_rate, dst_rate) / (2.0 * src_rate * up)
    # 滤波器延迟对齐到 decimation 栅格，保证输出与原信号同相位（T-1A.5 质量要求）
    base_delay = 16 * max(up, down)
    delay = (base_delay // down) * down if down > 1 else base_delay
    # 零填充后平均功率下降 up 倍，故滤波器增益须乘 up 以保持幅度
    kernel = _lowpass_kernel(cutoff, 2 * delay + 1) * up

    out_len = int(round(num_in * dst_rate / src_rate))
    if up == 1 or down == 1:
        out = _decimate_after_interpolate(data, up, down, delay, kernel, out_len)
    else:
        out = _polyphase_resample(data, up, down, delay, kernel, out_len)

    return out.ravel() if mono else out


def resample_pcm16(
    data: bytes, src_rate: int, dst_rate: int, channels: int = 1
) -> bytes:
    """PCM16 重采样（抗混叠），采样率一致时原样返回。"""
    if src_rate == dst_rate:
        return data
    samples = pcm16_to_float(data, channels)
    resampled = resample_float(samples, src_rate, dst_rate)
    if channels > 1 and resampled.ndim == 1:
        resampled = resampled.reshape(-1, channels)
    return float_to_pcm16(resampled)


# ---------------------------------------------------------------- 环形缓冲


class RingBuffer:
    """按字节容量有界的帧缓冲（INV-7：超限按策略丢弃并告警）。"""

    def __init__(
        self,
        capacity_bytes: int,
        drop_policy: Literal["oldest", "newest"] = "oldest",
        on_overflow: Callable[[str, int], None] | None = None,
    ) -> None:
        if capacity_bytes <= 0:
            raise ValueError(f"容量必须为正数：{capacity_bytes}")
        if drop_policy not in ("oldest", "newest"):
            raise ValueError(f"未知丢弃策略：{drop_policy}")
        self.capacity_bytes = capacity_bytes
        self.drop_policy = drop_policy
        self.on_overflow = on_overflow
        self.dropped_frames = 0
        self._frames: deque[AudioFrame] = deque()
        self._bytes = 0

    def push(self, frame: AudioFrame) -> list[AudioFrame]:
        """入队；返回被丢弃的帧（可能为空）。"""
        size = len(frame.data)
        if self.drop_policy == "newest" and self._bytes + size > self.capacity_bytes:
            self.dropped_frames += 1
            self._notify(size)
            return []
        dropped: list[AudioFrame] = []
        self._frames.append(frame)
        self._bytes += size
        while self._bytes > self.capacity_bytes and self._frames:
            old = self._frames.popleft()
            self._bytes -= len(old.data)
            self.dropped_frames += 1
            dropped.append(old)
        if dropped:
            self._notify(sum(len(item.data) for item in dropped))
        return dropped

    def pop(self, max_bytes: int | None = None) -> list[AudioFrame]:
        """取出帧（FIFO）；max_bytes 为空时取出一帧。"""
        if max_bytes is None:
            if not self._frames:
                return []
            frame = self._frames.popleft()
            self._bytes -= len(frame.data)
            return [frame]
        out: list[AudioFrame] = []
        used = 0
        while self._frames and used + len(self._frames[0].data) <= max_bytes:
            frame = self._frames.popleft()
            self._bytes -= len(frame.data)
            used += len(frame.data)
            out.append(frame)
        return out

    def clear(self) -> list[AudioFrame]:
        out = list(self._frames)
        self._frames.clear()
        self._bytes = 0
        return out

    def _notify(self, dropped_bytes: int) -> None:
        if self.on_overflow is not None:
            self.on_overflow(self.drop_policy, dropped_bytes)

    @property
    def bytes_used(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._frames)


class JitterBuffer:
    """按 seq 重排的抖动缓冲：缓冲到目标深度后按序输出，缺帧跳过并计数。"""

    def __init__(
        self,
        target_depth: int,
        max_depth: int | None = None,
        on_loss: Callable[[int], None] | None = None,
    ) -> None:
        if target_depth < 1:
            raise ValueError(f"target_depth 必须 >= 1：{target_depth}")
        self.target_depth = target_depth
        self.max_depth = max_depth or target_depth * 3
        self.on_loss = on_loss
        self.lost_frames = 0
        self.duplicates = 0
        self._pending: dict[int, AudioFrame] = {}
        self._next_seq: int | None = None
        self._started = False

    def push(self, frame: AudioFrame) -> list[AudioFrame]:
        """入队并返回可就绪的帧；起始序号取「攒够目标深度后的最小 seq」。"""
        if frame.seq in self._pending:
            self.duplicates += 1
            return []
        if self._started and self._next_seq is not None and frame.seq < self._next_seq:
            self.duplicates += 1
            return []

        self._pending[frame.seq] = frame
        if not self._started:
            if len(self._pending) < self.target_depth:
                return []
            self._started = True
            self._next_seq = min(self._pending)

        out = self._drain()
        self._skip_gap()
        out.extend(self._drain())
        self._trim_overflow()
        return out

    def _drain(self) -> list[AudioFrame]:
        out: list[AudioFrame] = []
        if self._next_seq is None:
            return out
        while self._next_seq in self._pending:
            out.append(self._pending.pop(self._next_seq))
            self._next_seq += 1
        return out

    def _skip_gap(self) -> None:
        """缓冲已达目标深度且存在缺口时，跳号继续输出，避免整条链路卡在缺帧上。"""
        if self._next_seq is None or not self._pending:
            return
        lowest = min(self._pending)
        if lowest <= self._next_seq:
            return
        if len(self._pending) >= self.target_depth:
            self.lost_frames += lowest - self._next_seq
            if self.on_loss is not None:
                self.on_loss(lowest - self._next_seq)
            self._next_seq = lowest

    def _trim_overflow(self) -> None:
        if self._next_seq is None or len(self._pending) <= self.max_depth:
            return
        for seq in sorted(self._pending)[: len(self._pending) - self.max_depth]:
            self._pending.pop(seq)
            self.lost_frames += 1
        self._next_seq = max(self._next_seq, min(self._pending, default=self._next_seq))

    def flush(self) -> list[AudioFrame]:
        """清空并返回剩余帧（按 seq 升序）；重置起始判定，重新攒目标深度。"""
        remainder = [self._pending[seq] for seq in sorted(self._pending)]
        self._pending.clear()
        self._next_seq = None
        self._started = False
        return remainder

    def __len__(self) -> int:
        return len(self._pending)
