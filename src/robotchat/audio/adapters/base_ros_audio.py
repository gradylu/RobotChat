"""通用 ROS2 音频适配器（T-1A.3 / T-1A.4 / design §3.6.2 ~ §3.6.4）。

- 话题 / 服务 / 消息类型 / 字段映射 / QoS 全部来自机型 Profile，不在业务代码中出现；
- 采集与播放均在适配器内完成采样率、声道、位深转换（抗混叠重采样，FR-16.5）；
- `flush()` 支持 Profile 声明的四种停播方式：service / topic_msg / stream_stop / volume_to_zero；
- 底层 DDS 操作通过 `RosAudioBackend` 注入：生产用 `RclpyBackend`，契约测试用模拟后端。
"""

from __future__ import annotations

import asyncio
import importlib
import threading
import time
from array import array
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from robotchat.audio.adapters import register_device_adapter
from robotchat.audio.buffer import convert_channels, pcm16_to_float, resample_float
from robotchat.audio.frame import AudioChunk, AudioFrame
from robotchat.config.schema import DeviceProfileSchema, ProfileTimestamp
from robotchat.observability.logging import get_logger

logger = get_logger("robotchat.audio.adapters")

AudioCallback = Callable[[bytes, float | None], None]

_TS_SCALE = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0}
_TS_PER_SECOND = {"ns": 1e9, "us": 1e6, "ms": 1e3, "s": 1.0}


@dataclass(frozen=True, slots=True)
class SubscribeSpec:
    topic: str
    msg_type: str
    fields: dict[str, str]
    qos: dict[str, Any]
    timestamp: ProfileTimestamp
    callback: AudioCallback


@dataclass(frozen=True, slots=True)
class PublishSpec:
    topic: str
    msg_type: str
    field: str = "data"


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    service: str
    msg_type: str
    call_timeout_s: float = 1.0


class RosAudioBackend(Protocol):
    """DDS 操作抽象：适配器只声明「订阅哪个话题、发布什么」，不直接 import rclpy。"""

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def subscribe(self, spec: SubscribeSpec) -> None: ...

    async def publish(self, spec: PublishSpec, payload: bytes) -> None: ...

    async def publish_float(self, spec: PublishSpec, value: float) -> None: ...

    async def call_service(self, spec: ServiceSpec) -> None: ...


# ---------------------------------------------------------------- PCM 位深转换


def decode_pcm(data: bytes, bit_depth: int, channels: int) -> np.ndarray:
    if bit_depth == 16:
        return np.asarray(pcm16_to_float(data, channels), dtype=np.float32)
    if bit_depth == 8:
        samples = np.frombuffer(data, dtype="u1").astype(np.float32) / 128.0 - 1.0
    elif bit_depth == 32:
        samples = np.frombuffer(data, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"不支持的位深：{bit_depth}")
    if channels > 1:
        samples = samples.reshape(-1, channels)
    return np.asarray(samples, dtype=np.float32)


def encode_pcm(samples: np.ndarray, bit_depth: int) -> bytes:
    data = np.clip(np.asarray(samples, dtype=np.float64), -1.0, 1.0).ravel()
    if bit_depth == 16:
        return np.round(data * 32767.0).astype("<i2").tobytes()
    if bit_depth == 8:
        return np.round((data + 1.0) * 127.5).clip(0, 255).astype("u1").tobytes()
    if bit_depth == 32:
        return np.round(data * 2147483647.0).astype("<i4").tobytes()
    raise ValueError(f"不支持的位深：{bit_depth}")


# ---------------------------------------------------------------- 通用适配器


class _CaptureContract:
    def __init__(self, owner: BaseRosAudioAdapter) -> None:
        self._owner = owner

    async def open(self) -> None:
        return None

    def stream(self) -> AsyncIterator[AudioFrame]:
        return self._owner.capture_stream()

    def last_timestamp(self) -> float:
        return self._owner.last_timestamp()

    async def close(self) -> None:
        await self._owner.close()


class _PlaybackContract:
    def __init__(self, owner: BaseRosAudioAdapter) -> None:
        self._owner = owner

    async def open(self) -> None:
        return None

    async def play(self, chunk: AudioChunk) -> None:
        await self._owner.play(chunk)

    async def flush(self) -> None:
        await self._owner.flush()

    async def set_volume(self, level: float) -> None:
        await self._owner.set_volume(level)

    async def close(self) -> None:
        await self._owner.close()


@register_device_adapter("base_ros_audio")
class BaseRosAudioAdapter:
    """按 Profile 绑定接口的通用适配器；机型专属差异通过子类或 Profile 表达。"""

    def __init__(
        self,
        profile: DeviceProfileSchema,
        *,
        backend: RosAudioBackend | None = None,
        target_sample_rate: int = 16000,
        target_channels: int = 1,
        target_bit_depth: int = 16,
        frame_ms: int = 20,
        capture_timeout_s: float = 2.0,
        queue_frames: int = 50,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if target_channels != 1:
            raise ValueError("内部管线统一为单声道，target_channels 必须为 1")
        self.profile = profile
        self.profile_name = profile.profile
        self.target_sample_rate = target_sample_rate
        self.target_channels = target_channels
        self.target_bit_depth = target_bit_depth
        self.frame_ms = frame_ms
        self.capture_timeout_s = capture_timeout_s
        self.queue_frames = queue_frames
        self._clock = clock
        self._backend: RosAudioBackend = backend or RclpyBackend()

        self.capture: _CaptureContract = _CaptureContract(self)
        self.playback: _PlaybackContract = _PlaybackContract(self)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[tuple[bytes, float | None]] | None = None
        self._opened = False
        self._closed = False
        self._residual = np.zeros(0, dtype=np.float32)
        self._seq = 0
        self._ts_offset: float | None = None
        self._last_ts: float | None = None
        self._volume_restore: float | None = None
        self._volume = 1.0

        # 诊断计数（C-168）
        self.frames_captured = 0
        self.dropped_frames = 0
        self.reconnects = 0
        self.played_chunks = 0
        self.flush_count = 0
        self.last_flush_ms: float | None = None

    # ------------------------------------------------------------ 生命周期

    async def open(self) -> None:
        if self._opened:
            return
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self.queue_frames)
        self._closed = False
        await self._backend.open()
        await self._subscribe()
        self._opened = True
        logger.info(
            "device_adapter_open",
            profile=self.profile_name,
            capture_topic=self.profile.capture.topic,
            capture_rate=self.profile.capture.sample_rate,
            playback_topic=self.profile.playback.topic,
            playback_rate=self.profile.playback.sample_rate,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue = None
        await self._backend.close()
        self._opened = False

    async def _subscribe(self) -> None:
        spec = SubscribeSpec(
            topic=self.profile.capture.topic,
            msg_type=self.profile.capture.msg_type,
            fields=dict(self.profile.capture.fields),
            qos={
                "reliability": self.profile.capture.qos.reliability,
                "depth": self.profile.capture.qos.depth,
            },
            timestamp=self.profile.capture.timestamp,
            callback=self._on_audio,
        )
        await self._backend.subscribe(spec)

    def _on_audio(self, payload: bytes, stamp: float | None) -> None:
        """DDS 回调（可能在 spin 线程）→ 事件循环队列。"""
        loop = self._loop
        queue = self._queue
        if loop is None or queue is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._push, payload, stamp)

    def _push(self, payload: bytes, stamp: float | None) -> None:
        queue = self._queue
        if queue is None or self._closed:
            return
        if queue.full():
            try:
                queue.get_nowait()
                self.dropped_frames += 1
                logger.warning(
                    "device_capture_overflow",
                    profile=self.profile_name,
                    dropped=self.dropped_frames,
                )
            except asyncio.QueueEmpty:  # pragma: no cover - 竞态兜底
                pass
        queue.put_nowait((payload, stamp))

    # ------------------------------------------------------------ 采集

    async def capture_stream(self) -> AsyncIterator[AudioFrame]:
        while not self._closed:
            queue = self._queue
            if queue is None:
                return
            try:
                payload, stamp = await asyncio.wait_for(
                    queue.get(), timeout=self.capture_timeout_s
                )
            except asyncio.TimeoutError:
                if self._closed:
                    return
                # 采集超时视为链路停滞：重建订阅并计数，恢复后继续出帧
                self.reconnects += 1
                logger.warning(
                    "device_capture_stalled",
                    profile=self.profile_name,
                    topic=self.profile.capture.topic,
                    reconnects=self.reconnects,
                )
                await self._subscribe()
                continue
            for frame in self._convert(payload, stamp):
                yield frame

    def _convert(self, payload: bytes, stamp: float | None) -> list[AudioFrame]:
        capture = self.profile.capture
        samples = decode_pcm(payload, capture.bit_depth, capture.channels)
        if capture.channels != self.target_channels:
            samples = convert_channels(samples, capture.channels, self.target_channels)
        samples = np.asarray(samples, dtype=np.float32).ravel()
        if capture.sample_rate != self.target_sample_rate:
            samples = np.asarray(
                resample_float(samples, capture.sample_rate, self.target_sample_rate),
                dtype=np.float32,
            ).ravel()
        self._residual = np.concatenate([self._residual, samples])
        block = max(1, int(self.target_sample_rate * self.frame_ms / 1000))
        frames: list[AudioFrame] = []
        while self._residual.size >= block:
            chunk = self._residual[:block]
            self._residual = self._residual[block:]
            self._seq += 1
            frames.append(
                AudioFrame(
                    data=encode_pcm(chunk, self.target_bit_depth),
                    sample_rate=self.target_sample_rate,
                    channels=self.target_channels,
                    bit_depth=self.target_bit_depth,
                    ts_device=self._normalize_ts(stamp),
                    seq=self._seq,
                )
            )
        self.frames_captured += len(frames)
        return frames

    def _normalize_ts(self, stamp: float | None) -> float:
        """设备时间戳归一为单调递增秒；不可靠时退化为到达时间（design §3.6.4）。"""
        if stamp is None:
            candidate = self._clock()
        else:
            candidate = stamp * _TS_SCALE[self.profile.capture.timestamp.unit]
            if self._ts_offset is None:
                self._ts_offset = candidate
            candidate -= self._ts_offset
        if self._last_ts is not None and candidate < self._last_ts:
            candidate = self._last_ts
        self._last_ts = candidate
        return candidate

    def last_timestamp(self) -> float:
        return self._last_ts if self._last_ts is not None else 0.0

    # ------------------------------------------------------------ 播放

    async def play(self, chunk: AudioChunk) -> None:
        if not self._opened or self._closed:
            raise RuntimeError("设备适配器未打开：play() 前须先 open()")
        playback = self.profile.playback
        samples = decode_pcm(chunk.data, chunk.bit_depth, chunk.channels)
        if chunk.channels != playback.channels:
            samples = convert_channels(samples, chunk.channels, playback.channels)
        samples = np.asarray(samples, dtype=np.float32)
        if chunk.sample_rate != playback.sample_rate:
            samples = resample_float(samples, chunk.sample_rate, playback.sample_rate)
        payload = encode_pcm(samples, playback.bit_depth)

        if self._volume_restore is not None:
            # flush(volume_to_zero) 后的第一帧：恢复音量再出声
            await self.set_volume(self._volume_restore)
            self._volume_restore = None

        spec = PublishSpec(
            topic=playback.topic,
            msg_type=playback.msg_type,
            field=playback.fields.get("data", "data"),
        )
        await self._backend.publish(spec, payload)
        self.played_chunks += 1

    async def flush(self) -> None:
        """按 Profile 声明的策略立即停播（T-1A.4 / FR-16.6）。"""
        started = self._clock()
        flush = self.profile.playback.flush
        if flush.kind == "stream_stop":
            await self._backend.publish(
                PublishSpec(
                    topic=self.profile.playback.topic,
                    msg_type=self.profile.playback.msg_type,
                    field=self.profile.playback.fields.get("data", "data"),
                ),
                b"",
            )
        elif flush.kind == "service":
            await self._backend.call_service(
                ServiceSpec(
                    service=flush.service,
                    msg_type=flush.msg_type or "std_srvs/srv/Trigger",
                )
            )
        elif flush.kind == "topic_msg":
            await self._backend.publish(
                PublishSpec(
                    topic=flush.topic,
                    msg_type=flush.msg_type or "std_msgs/msg/Empty",
                    field="data",
                ),
                b"",
            )
        elif flush.kind == "volume_to_zero":
            if self.profile.volume is None:
                raise RuntimeError("flush(volume_to_zero) 需要 Profile 声明 volume 配置")
            self._volume_restore = self._volume
            await self.set_volume(self.profile.volume.range[0])
        else:  # pragma: no cover - Literal 已限制
            raise RuntimeError(f"未知 flush 策略：{flush.kind}")
        self.flush_count += 1
        self.last_flush_ms = round((self._clock() - started) * 1000.0, 2)
        logger.debug(
            "device_flush",
            profile=self.profile_name,
            strategy=flush.kind,
            elapsed_ms=self.last_flush_ms,
        )

    async def set_volume(self, level: float) -> None:
        volume = self.profile.volume
        if volume is None:
            if level not in (0.0, 1.0):
                raise RuntimeError("Profile 未声明 volume 配置，不支持调节音量")
            return
        low, high = volume.range
        if not low <= level <= high:
            raise ValueError(f"音量 {level} 超出 Profile 范围 {volume.range}")
        self._volume = float(level)
        await self._backend.publish_float(
            PublishSpec(topic=volume.topic, msg_type=volume.msg_type), float(level)
        )

    # ------------------------------------------------------------ 诊断

    def snapshot(self) -> dict[str, Any]:
        return {
            "profile": self.profile_name,
            "source_queue": self._queue.qsize() if self._queue is not None else 0,
            "frames_captured": self.frames_captured,
            "dropped_frames": self.dropped_frames,
            "reconnects": self.reconnects,
            "played_chunks": self.played_chunks,
            "flush_count": self.flush_count,
            "last_flush_ms": self.last_flush_ms,
            "last_timestamp": round(self.last_timestamp(), 4),
        }


# ---------------------------------------------------------------- rclpy 后端


@dataclass
class _PublisherSlot:
    publisher: Any
    msg_type: Any


class RclpyBackend:
    """生产环境 DDS 后端：动态解析消息类型，按 Profile 绑定话题 / 服务。"""

    def __init__(self) -> None:
        self._rclpy: Any = None
        self._node: Any = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._subscription: Any = None
        self._publishers: dict[tuple[str, str], _PublisherSlot] = {}
        self._clients: dict[str, Any] = {}

    async def open(self) -> None:
        try:
            import rclpy
        except ImportError as exc:
            raise RuntimeError(
                "rclpy 不可用：机型适配需在 ROS2 镜像运行；开发环境请用 audio.transport=file"
            ) from exc
        self._rclpy = rclpy
        if not rclpy.ok():
            rclpy.init(args=None)
        self._node = rclpy.create_node("robotchat_device_adapter")
        self._running = True
        self._thread = threading.Thread(
            target=self._spin, name="robotchat-device-spin", daemon=True
        )
        self._thread.start()

    def _spin(self) -> None:
        node = self._node
        while self._running and node is not None:
            try:
                self._rclpy.spin_once(node, timeout_sec=0.05)
            except Exception:  # 单次 spin 失败不终止链路
                logger.exception("device_spin_failed")
                time.sleep(0.1)

    async def subscribe(self, spec: SubscribeSpec) -> None:
        msg_type = _resolve_msg_type(spec.msg_type)
        qos = self._qos_profile(spec.qos)
        fields = dict(spec.fields)
        timestamp = spec.timestamp
        callback = spec.callback

        def handler(msg: Any, *, _fields=fields, _ts=timestamp, _cb=callback) -> None:
            try:
                data = _extract_bytes(msg, _fields.get("data", "data"))
                stamp = _extract_stamp(msg, _ts) if _ts.source == "header_stamp" else None
            except Exception:
                logger.exception("device_capture_field_error", topic=spec.topic)
                return
            _cb(data, stamp)

        if self._subscription is not None and self._node is not None:
            self._node.destroy_subscription(self._subscription)
        self._subscription = self._node.create_subscription(
            msg_type, spec.topic, handler, qos
        )

    def _qos_profile(self, qos: dict[str, Any]) -> Any:
        from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy

        reliability = (
            ReliabilityPolicy.RELIABLE
            if qos.get("reliability") == "reliable"
            else ReliabilityPolicy.BEST_EFFORT
        )
        return QoSProfile(
            reliability=reliability,
            history=HistoryPolicy.KEEP_LAST,
            depth=int(qos.get("depth", 10)),
        )

    def _publisher(self, spec: PublishSpec) -> Any:
        key = (spec.topic, spec.msg_type)
        slot = self._publishers.get(key)
        if slot is None:
            msg_type = _resolve_msg_type(spec.msg_type)
            slot = _PublisherSlot(self._node.create_publisher(msg_type, spec.topic, 10), msg_type)
            self._publishers[key] = slot
        return slot

    async def publish(self, spec: PublishSpec, payload: bytes) -> None:
        slot = self._publisher(spec)
        msg = slot.msg_type()
        _assign(msg, spec.field, array("B", payload))
        slot.publisher.publish(msg)

    async def publish_float(self, spec: PublishSpec, value: float) -> None:
        slot = self._publisher(spec)
        msg = slot.msg_type()
        _assign(msg, spec.field, value)
        slot.publisher.publish(msg)

    async def call_service(self, spec: ServiceSpec) -> None:
        msg_type = _resolve_msg_type(spec.msg_type)
        client = self._clients.get(spec.service)
        if client is None:
            client = self._node.create_client(msg_type, spec.service)
            self._clients[spec.service] = client
        ready = await asyncio.to_thread(client.wait_for_service, timeout_sec=spec.call_timeout_s)
        if not ready:
            raise RuntimeError(f"停播服务不可用：{spec.service}")
        request = msg_type.Request()
        await asyncio.to_thread(client.call, request)

    async def close(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        node = self._node
        if node is not None:
            try:
                if self._subscription is not None:
                    node.destroy_subscription(self._subscription)
                for slot in self._publishers.values():
                    node.destroy_publisher(slot.publisher)
                for client in self._clients.values():
                    node.destroy_client(client)
                node.destroy_node()
            except Exception:  # pragma: no cover - 关停异常不阻塞退出
                logger.exception("device_backend_close_failed")
        self._node = None
        self._subscription = None
        self._publishers.clear()
        self._clients.clear()
        if self._rclpy is not None and self._rclpy.ok():
            self._rclpy.shutdown()
        self._rclpy = None


# ---------------------------------------------------------------- 消息辅助


def _resolve_msg_type(spec: str) -> Any:
    package, _, class_name = spec.partition("/msg/")
    if not class_name:
        raise ValueError(f"消息类型格式错误：{spec!r}（期望 pkg/msg/Type）")
    try:
        module = importlib.import_module(f"{package}.msg")
    except ImportError as exc:
        raise ValueError(f"消息类型不可用：{spec}（ROS2 消息包未安装）") from exc
    try:
        return getattr(module, class_name)
    except AttributeError as exc:
        raise ValueError(f"消息类型不存在：{spec}") from exc


def _extract_value(msg: Any, path: str) -> Any:
    value = msg
    for part in path.split("."):
        value = getattr(value, part)
    return value


def _extract_bytes(msg: Any, path: str) -> bytes:
    value = _extract_value(msg, path)
    if isinstance(value, bytes):
        return value
    if hasattr(value, "tobytes"):
        return bytes(value.tobytes())
    return bytes(value)


def _extract_stamp(msg: Any, timestamp: ProfileTimestamp) -> float | None:
    try:
        stamp = _extract_value(msg, "header.stamp")
    except AttributeError:
        return None
    seconds = float(getattr(stamp, "sec", 0)) + float(getattr(stamp, "nanosec", 0)) * 1e-9
    return seconds * _TS_PER_SECOND[timestamp.unit]


def _assign(msg: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    target = msg
    for part in parts[:-1]:
        target = getattr(target, part)
    setattr(target, parts[-1], value)


__all__ = [
    "BaseRosAudioAdapter",
    "PublishSpec",
    "RclpyBackend",
    "RosAudioBackend",
    "ServiceSpec",
    "SubscribeSpec",
    "decode_pcm",
    "encode_pcm",
]
