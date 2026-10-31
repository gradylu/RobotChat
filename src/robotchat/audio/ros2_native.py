"""ROS2 原生音频适配（T-1.2 / T-1.3，方案 A：容器内 rclpy 直连 DDS）。

- 采集：订阅麦克风 topic → AudioFrame 流（数据为原始 PCM，不做重复编解码，design §3.3）
- 播放：音频块 → 播放 topic；`flush()` 以流停止语义立即静音（Profile 化策略见 T-1A.4）
- 断线自动重连：采集超时视为链路停滞，重建订阅并计数
- 话题 / 消息类型最终由机型 Profile 提供（Phase 1A），此处支持配置覆盖以便先行自测
"""

from __future__ import annotations

import asyncio
import importlib
import threading
import time
from array import array
from collections.abc import AsyncIterator
from typing import Any

from robotchat.audio.frame import AudioChunk, AudioFrame
from robotchat.observability.logging import get_logger
from robotchat.providers.registry import register_provider

logger = get_logger("robotchat.audio.ros2")

_CAPTURE_STALL_S = 2.0


def _resolve_msg_type(spec: str) -> type:
    """按 `pkg/msg/Type` 形式动态导入 ROS2 消息类型。"""
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


@register_provider("transport", "ros2_native")
class Ros2NativeTransport:
    name = "ros2_native"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        capture = cfg.get("capture") or {}
        self.capture_topic = str(cfg.get("capture_topic") or "/mic/audio_raw")
        self.playback_topic = str(cfg.get("playback_topic") or "/spk/audio_out")
        self.capture_msg_type = str(
            cfg.get("capture_msg_type") or "audio_common_msgs/msg/AudioData"
        )
        self.playback_msg_type = str(
            cfg.get("playback_msg_type") or "audio_common_msgs/msg/AudioData"
        )
        self.sample_rate = int(cfg.get("sample_rate") or capture.get("target_sample_rate", 16000))
        self.channels = int(cfg.get("channels") or capture.get("target_channels", 1))
        self.frame_ms = int(cfg.get("frame_ms", 20))
        self.queue_max = int(cfg.get("queue_frames") or 50)
        self.reconnect_interval_s = float(cfg.get("reconnect_interval_s", 0.5))

        self.reconnects = 0
        self.dropped_frames = 0
        self.played_chunks = 0
        self._queue: asyncio.Queue[bytes] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._node: Any = None
        self._rclpy: Any = None
        self._subscription: Any = None
        self._publisher: Any = None
        self._capture_type: type | None = None
        self._playback_type: type | None = None
        self._qos: Any = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._seq = 0

    # ------------------------------------------------------------ 生命周期

    async def open(self) -> None:
        try:
            import rclpy
            from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
        except ImportError as exc:
            raise RuntimeError(
                "rclpy 不可用：当前环境不是 ROS2 容器。方案 A 需以 ros:<distro>-ros-base "
                "为基础镜像；开发环境可改用 audio.transport=file（ADR-02）"
            ) from exc

        self._rclpy = rclpy
        if not rclpy.ok():
            rclpy.init(args=None)
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=self.queue_max)
        self._qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
        )
        self._node = rclpy.create_node("robotchat_audio_transport")

        self._capture_type = self._resolve_type_with_fallback(self.capture_msg_type)
        self._playback_type = self._resolve_type_with_fallback(self.playback_msg_type)

        self._running = True
        self._ensure_subscription()
        self._publisher = self._node.create_publisher(
            self._playback_type, self.playback_topic, 10
        )
        self._thread = threading.Thread(
            target=self._spin, name="robotchat-ros2-spin", daemon=True
        )
        self._thread.start()
        logger.info(
            "ros2_transport_open",
            capture_topic=self.capture_topic,
            playback_topic=self.playback_topic,
        )

    def _resolve_type_with_fallback(self, spec: str) -> type:
        try:
            return _resolve_msg_type(spec)
        except ValueError as exc:
            logger.warning("ros2_msg_type_fallback", requested=spec, reason=str(exc))
            return _resolve_msg_type("std_msgs/msg/UInt8MultiArray")

    def _spin(self) -> None:
        node = self._node
        while self._running and node is not None:
            try:
                self._rclpy.spin_once(node, timeout_sec=0.05)
            except Exception:  # 单次 spin 失败不终止链路，下一轮重建订阅
                logger.exception("ros2_spin_failed")
                time.sleep(self.reconnect_interval_s)

    def _ensure_subscription(self) -> None:
        """创建 / 重建订阅；创建失败只告警，由采集侧超时重试。"""
        if self._node is None or self._loop is None:
            return
        try:
            if self._subscription is not None:
                self._node.destroy_subscription(self._subscription)
            self._subscription = self._node.create_subscription(
                self._capture_type, self.capture_topic, self._on_audio, self._qos
            )
        except Exception:
            self._subscription = None
            logger.exception("ros2_subscription_failed", topic=self.capture_topic)

    def _on_audio(self, msg: Any) -> None:
        """订阅回调（spin 线程）→ 事件循环。"""
        loop = self._loop
        queue = self._queue
        if loop is None or queue is None or loop.is_closed():
            return
        data = bytes(msg.data)
        loop.call_soon_threadsafe(self._push_frame, data)

    def _push_frame(self, data: bytes) -> None:
        queue = self._queue
        if queue is None:
            return
        if queue.full():
            # 采集不能被播放阻塞；溢出丢最旧帧并计数（INV-7）
            try:
                queue.get_nowait()
                self.dropped_frames += 1
                logger.warning("audio_capture_overflow", dropped=self.dropped_frames)
            except asyncio.QueueEmpty:  # pragma: no cover - 竞态兜底
                pass
        queue.put_nowait(data)

    # ------------------------------------------------------------ 采集

    def capture(self) -> AsyncIterator[AudioFrame]:
        async def generator() -> AsyncIterator[AudioFrame]:
            while self._running:
                queue = self._queue
                if queue is None:
                    return
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=_CAPTURE_STALL_S)
                except asyncio.TimeoutError:
                    logger.warning("audio_capture_stalled", topic=self.capture_topic)
                    self.reconnects += 1
                    self._ensure_subscription()
                    continue
                self._seq += 1
                yield AudioFrame(
                    data=data,
                    sample_rate=self.sample_rate,
                    channels=self.channels,
                    seq=self._seq,
                    ts_device=time.monotonic(),
                )

        return generator()

    # ------------------------------------------------------------ 播放

    async def play(self, chunk: AudioChunk) -> None:
        if self._publisher is None or self._playback_type is None:
            raise RuntimeError("ROS2 播放未初始化：请先调用 open()")
        msg = self._playback_type()
        msg.data = array("B", chunk.data)
        self._publisher.publish(msg)
        self.played_chunks += 1

    async def flush(self) -> None:
        """流停止语义：发布空音频块让设备侧停播（Profile 化策略见 T-1A.4）。"""
        if self._publisher is None or self._playback_type is None:
            return
        msg = self._playback_type()
        msg.data = array("B", b"")
        self._publisher.publish(msg)

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
                node.destroy_node()
            except Exception:  # pragma: no cover - 关停异常不应阻塞退出
                logger.exception("ros2_close_failed")
        self._node = None
        self._subscription = None
        self._publisher = None
        if self._rclpy is not None and self._rclpy.ok():
            self._rclpy.shutdown()
        self._rclpy = None
