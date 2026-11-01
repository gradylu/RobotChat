"""智元 A2 / A3 适配器（AimDK 高层接口：ros2_plugin_proto + 内嵌 protobuf + HTTP RPC 音量）。

机型差异（相对通用适配器）：
- 采集话题的消息是 `ros2_plugin_proto/msg/RosMsgWrapper`，音频 PCM 在序列化的
  `aimdk.protocol.ProcessedAudioOutput` 里，需要先反序列化再解 PCM；
- 音量走 HTTP RPC（`HalAudioService/SetAudioVolume`），不是 ROS2 服务；
- 流式 PCM 没有停播接口，Profile 声明 `flush=none`，`flush()` 由基类记录告警后返回。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import time
import urllib.request
from collections.abc import Callable
from typing import Any

from robotchat.audio.adapters import register_device_adapter
from robotchat.audio.adapters.base_ros_audio import (
    BaseRosAudioAdapter,
    RosAudioBackend,
)
from robotchat.config.schema import DeviceProfileSchema
from robotchat.observability.logging import get_logger

logger = get_logger("robotchat.audio.adapters.agibot_ros2_plugin")

# protobuf 里 PCM 字节的字段名（文档：ProcessedAudioOutput.audio_data）
AUDIO_DATA_FIELD = "audio_data"

PayloadDecoder = Callable[[bytes, str], bytes]
HttpPost = Callable[[str, dict[str, Any]], None]


class ProtobufUnavailableError(RuntimeError):
    """缺少 SDK 提供的 protobuf 定义：不做猜测性解析，直接失败（FR-16.9）。"""


def load_payload_class(payload_type: str) -> Any:
    """按 `aimdk.protocol.ProcessedAudioOutput` 定位生成的 protobuf 类。"""
    package, _, name = payload_type.rpartition(".")
    candidates = [f"{package}.{name}_pb2", f"{package}_pb2"]
    if package:
        candidates.append(package)
    for module_name in candidates:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        cls = getattr(module, name, None)
        if cls is not None:
            return cls
    raise ProtobufUnavailableError(
        f"缺少 protobuf 定义 {payload_type}：请安装智元 SDK 的 aimdk.protocol pb2 模块，"
        f"或注入自定义 decoder（已尝试 {', '.join(candidates)}）"
    )


def decode_ros2_plugin_payload(payload: bytes, payload_type: str) -> bytes:
    """RosMsgWrapper 内的序列化 pb → PCM 字节。"""
    cls = load_payload_class(payload_type)
    message = cls()
    message.ParseFromString(payload)
    data = getattr(message, AUDIO_DATA_FIELD, b"")
    return bytes(data)


def http_post_json(url: str, body: dict[str, Any], timeout_s: float = 2.0) -> None:
    """同步 POST JSON（在线程里调用）；A2 / A3 的 HTTP RPC 入口。"""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
        response.read()


@register_device_adapter("agibot_ros2_plugin")
class AgibotRos2PluginAdapter(BaseRosAudioAdapter):
    """智元 A2 / A3 音频适配器。"""

    def __init__(
        self,
        profile: DeviceProfileSchema,
        *,
        backend: RosAudioBackend | None = None,
        decoder: PayloadDecoder | None = None,
        http_post: HttpPost | None = None,
        target_sample_rate: int = 16000,
        target_channels: int = 1,
        target_bit_depth: int = 16,
        frame_ms: int = 20,
        capture_timeout_s: float = 2.0,
        queue_frames: int = 50,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(
            profile,
            backend=backend,
            target_sample_rate=target_sample_rate,
            target_channels=target_channels,
            target_bit_depth=target_bit_depth,
            frame_ms=frame_ms,
            capture_timeout_s=capture_timeout_s,
            queue_frames=queue_frames,
            clock=clock,
        )
        self._decoder = decoder or decode_ros2_plugin_payload
        self._http_post = http_post or http_post_json
        self._decode_failures = 0

    # ------------------------------------------------------------ 采集

    def _decode_capture_payload(self, payload: bytes) -> bytes:
        capture = self.profile.capture
        if not capture.payload_type:
            raise RuntimeError("capture.payload=protobuf 时须声明 capture.payload_type")
        try:
            return self._decoder(payload, capture.payload_type)
        except Exception:
            # 单帧解析失败不应打断整条采集链路，但必须可观测
            self._decode_failures += 1
            logger.exception(
                "device_payload_decode_failed",
                profile=self.profile_name,
                payload_type=capture.payload_type,
                failures=self._decode_failures,
            )
            return b""

    # ------------------------------------------------------------ 音量

    async def set_volume(self, level: float) -> None:
        volume = self.profile.volume
        if volume is None:
            await super().set_volume(level)
            return
        low, high = volume.range
        if not low <= level <= high:
            raise ValueError(f"音量 {level} 超出 Profile 范围 {volume.range}")
        self._volume = float(level)
        if volume.kind != "http_rpc":
            await super().set_volume(level)
            return
        body: dict[str, Any] = {volume.field: int(round(level))}
        if volume.mute_field:
            body[volume.mute_field] = False
        await asyncio.to_thread(self._http_post, volume.endpoint, body)
        logger.info(
            "device_volume_set",
            profile=self.profile_name,
            level=int(round(level)),
            endpoint=volume.endpoint,
        )

    # ------------------------------------------------------------ 诊断

    def snapshot(self) -> dict[str, Any]:
        data = super().snapshot()
        data["decode_failures"] = self._decode_failures
        return data


__all__ = [
    "AUDIO_DATA_FIELD",
    "AgibotRos2PluginAdapter",
    "ProtobufUnavailableError",
    "decode_ros2_plugin_payload",
    "http_post_json",
    "load_payload_class",
]
