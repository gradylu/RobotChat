"""适配层契约测试公共设施（T-1A.5 / C-162 ~ C-164、C-167）。

提供：
- `FakeBackend`：模拟 DDS 后端，`emit()` 主动灌入采集数据，记录发布 / 服务调用；
- 两种差异明显的模拟 Profile（24kHz 双声道 / 48kHz 单声道 32bit，消息字段结构不同）；
- 正弦波生成与 THD+N 计算辅助函数。
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import numpy as np

from robotchat.audio.adapters.base_ros_audio import (
    PublishSpec,
    ServiceSpec,
    SubscribeSpec,
)
from robotchat.config.schema import DeviceProfileSchema


class FakeBackend:
    """按 Profile 记录订阅 / 发布 / 服务调用的模拟 DDS 后端。"""

    def __init__(self) -> None:
        self.opened = False
        self.closed = False
        self.subscriptions: list[SubscribeSpec] = []
        self.published: list[tuple[PublishSpec, bytes]] = []
        self.float_published: list[tuple[PublishSpec, float]] = []
        self.service_calls: list[ServiceSpec] = []
        self._current: SubscribeSpec | None = None

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def subscribe(self, spec: SubscribeSpec) -> None:
        self.subscriptions.append(spec)
        self._current = spec

    async def publish(self, spec: PublishSpec, payload: bytes) -> None:
        self.published.append((spec, payload))

    async def publish_float(self, spec: PublishSpec, value: float) -> None:
        self.float_published.append((spec, value))

    async def call_service(self, spec: ServiceSpec) -> None:
        self.service_calls.append(spec)

    def emit(self, payload: bytes, stamp: float | None = None) -> None:
        """模拟 DDS 回调（须在事件循环线程内调用）。"""
        assert self._current is not None, "not subscribed yet"
        self._current.callback(payload, stamp)


BASE_PROFILE: dict[str, Any] = {
    "profile": "contract_24k_stereo",
    "display_name": "契约测试：24kHz 双声道",
    "ros_distro": ["humble", "jazzy"],
    "robot_models": ["contract_24k"],
    "adapter": "base_ros_audio",
    "capabilities": {
        "capture": True,
        "playback": True,
        "echo_reference": True,
        "hardware_aec": False,
        "volume_control": True,
        "playback_flush": "service",
    },
    "capture": {
        "topic": "/contract/mic",
        "msg_type": "audio_common_msgs/msg/AudioData",
        "fields": {"data": "data"},
        "sample_rate": 24000,
        "channels": 2,
        "bit_depth": 16,
        "frame_ms": 20,
        "qos": {"reliability": "best_effort", "depth": 10},
        "timestamp": {"source": "header_stamp", "unit": "ns", "monotonic": True},
    },
    "playback": {
        "topic": "/contract/speaker",
        "msg_type": "audio_common_msgs/msg/AudioData",
        "fields": {"data": "data"},
        "sample_rate": 24000,
        "channels": 2,
        "bit_depth": 16,
        "latency_ms": 80,
        "flush": {
            "kind": "service",
            "service": "/contract/stop",
            "msg_type": "std_srvs/srv/Trigger",
        },
    },
    "volume": {
        "kind": "topic",
        "topic": "/contract/volume",
        "msg_type": "std_msgs/msg/Float32",
        "range": [0.0, 1.0],
    },
}

# 第二种机型：48kHz 单声道 32bit，采集字段映射到嵌套路径，flush 走话题消息
SECOND_PROFILE: dict[str, Any] = {
    "profile": "contract_48k_mono32",
    "display_name": "契约测试：48kHz 单声道 32bit",
    "ros_distro": ["humble", "jazzy"],
    "robot_models": ["contract_48k"],
    "adapter": "base_ros_audio",
    "capabilities": {
        "capture": True,
        "playback": True,
        "echo_reference": False,
        "hardware_aec": False,
        "volume_control": False,
        "playback_flush": "topic_msg",
    },
    "capture": {
        "topic": "/contract/mic48",
        "msg_type": "vendor_audio_msgs/msg/RawAudio",
        "fields": {"data": "audio.data"},
        "sample_rate": 48000,
        "channels": 1,
        "bit_depth": 32,
        "frame_ms": 20,
        "qos": {"reliability": "reliable", "depth": 40},
        "timestamp": {"source": "arrival", "unit": "s", "monotonic": True},
    },
    "playback": {
        "topic": "/contract/speaker48",
        "msg_type": "vendor_audio_msgs/msg/RawAudio",
        "fields": {"data": "audio.data"},
        "sample_rate": 48000,
        "channels": 1,
        "bit_depth": 32,
        "latency_ms": 40,
        "flush": {"kind": "topic_msg", "topic": "/contract/stop", "msg_type": ""},
    },
}


def make_profile(
    raw: dict[str, Any] | None = None, **updates: Any
) -> DeviceProfileSchema:
    """由原始 dict 构造并校验 Profile；`updates` 支持顶层覆盖。"""
    data = copy.deepcopy(raw if raw is not None else BASE_PROFILE)
    data.update(copy.deepcopy(updates))
    return DeviceProfileSchema.model_validate(data)


def flush_profile(kind: str) -> DeviceProfileSchema:
    """构造指定 flush 策略的 Profile（能力声明与实现保持一致）。"""
    data = copy.deepcopy(BASE_PROFILE)
    data["profile"] = f"contract_flush_{kind}"
    data["capabilities"]["playback_flush"] = kind
    if kind == "service":
        data["playback"]["flush"] = {
            "kind": kind,
            "service": "/contract/stop",
            "msg_type": "std_srvs/srv/Trigger",
        }
    elif kind == "topic_msg":
        data["playback"]["flush"] = {
            "kind": kind,
            "topic": "/contract/stop_cmd",
            "msg_type": "std_msgs/msg/Empty",
        }
    elif kind == "volume_to_zero":
        data["playback"]["flush"] = {"kind": kind}
    else:
        data["playback"]["flush"] = {"kind": "stream_stop"}
    return make_profile(data)


def sine_payload(
    rate: int,
    channels: int,
    bit_depth: int,
    duration_ms: float,
    *,
    freq: float = 1000.0,
    amplitude: float = 0.5,
    start_index: int = 0,
) -> bytes:
    """生成正弦波的 PCM 负载（多声道各声道同相）。"""
    count = int(rate * duration_ms / 1000)
    t = (np.arange(count, dtype=np.float64) + start_index) / rate
    mono = amplitude * np.sin(2.0 * np.pi * freq * t)
    samples = np.repeat(mono, channels) if channels > 1 else mono
    if bit_depth == 16:
        return np.round(np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    if bit_depth == 32:
        return np.round(np.clip(samples, -1.0, 1.0) * 2147483647.0).astype("<i4").tobytes()
    raise ValueError(f"契约测试不支持位深 {bit_depth}")


def thd_plus_n(signal: np.ndarray, rate: int, freq: float = 1000.0) -> float:
    """THD+N：基波（±3 bin）与直流之外能量占全频带能量的比例（越低越好）。"""
    if signal.size < 1024:
        return 1.0
    windowed = np.asarray(signal, dtype=np.float64) * np.blackman(signal.size)
    spectrum = np.abs(np.fft.rfft(windowed)) ** 2
    freqs = np.fft.rfftfreq(signal.size, 1.0 / rate)
    fundamental = int(np.argmin(np.abs(freqs - freq)))
    mask = np.ones(spectrum.size, dtype=bool)
    mask[:3] = False  # 直流
    mask[max(0, fundamental - 3) : fundamental + 4] = False
    noise = float(spectrum[mask].sum())
    total = float(spectrum.sum())
    if total <= 0.0:
        return 1.0
    return float(np.sqrt(noise / total))


class StepClock:
    """每次调用返回递增时间，用于模拟 flush 耗时。"""

    def __init__(self, step_s: float) -> None:
        self._value = 0.0
        self._step = step_s

    def __call__(self) -> float:
        current = self._value
        self._value += self._step
        return current


ClockFactory = Callable[[], StepClock]
