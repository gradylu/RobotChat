"""宇树 G1 适配器（unitree_sdk2：组播 UDP 采集 + DDS RPC 播放，完全不经过 ROS2）。

机型差异（相对通用适配器）：
- 麦克风音频是组播裸 PCM（默认 239.168.123.161:5555），不是 ROS2 话题，需要自行
  建 socket 并加入组播组；
- 播放是 DDS RPC（`AudioClient::PlayStream(app_name, stream_id, pcm)`），
  停播是 `AudioClient::PlayStop(app_name)`；
- 因此本适配器不订阅任何 ROS2 话题，DDS 后端退化为空实现。
"""

from __future__ import annotations

import asyncio
import importlib
import os
import socket
import struct
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from robotchat.audio.adapters import register_device_adapter
from robotchat.audio.adapters.base_ros_audio import (
    BaseRosAudioAdapter,
    PublishSpec,
    RosAudioBackend,
    ServiceSpec,
    SubscribeSpec,
)
from robotchat.audio.frame import AudioChunk
from robotchat.config.schema import DeviceProfileSchema
from robotchat.observability.logging import get_logger

logger = get_logger("robotchat.audio.adapters.unitree_g1_native")

DEFAULT_APP_NAME = "robotchat"
DEFAULT_STREAM_ID = 2
SDK_NETWORK_IFACE_ENV = "UNITREE_NETWORK_IFACE"


class _NullBackend:
    """G1 不走 ROS2：占位后端，仅保证基类生命周期可运行。"""

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def subscribe(self, spec: SubscribeSpec) -> None:
        return None

    async def publish(self, spec: PublishSpec, payload: bytes) -> None:
        raise RuntimeError("unitree_g1_native 不使用 ROS2 发布：请用 AudioClient")

    async def publish_float(self, spec: PublishSpec, value: float) -> None:
        raise RuntimeError("unitree_g1_native 不使用 ROS2 发布：请用 AudioClient")

    async def call_service(self, spec: ServiceSpec) -> None:
        raise RuntimeError("unitree_g1_native 不使用 ROS2 服务：请用 AudioClient")


class AudioClientLike(Protocol):
    def PlayStream(self, app_name: str, stream_id: int, pcm: bytes) -> Any: ...

    def PlayStop(self, app_name: str) -> Any: ...


SocketFactory = Callable[[], socket.socket]


def _default_socket() -> socket.socket:
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)


@register_device_adapter("unitree_g1_native")
class UnitreeG1Adapter(BaseRosAudioAdapter):
    """宇树 G1 音频适配器。"""

    def __init__(
        self,
        profile: DeviceProfileSchema,
        *,
        backend: RosAudioBackend | None = None,
        audio_client: AudioClientLike | None = None,
        socket_factory: SocketFactory | None = None,
        app_name: str = DEFAULT_APP_NAME,
        stream_id: int = DEFAULT_STREAM_ID,
        target_sample_rate: int = 16000,
        target_channels: int = 1,
        target_bit_depth: int = 16,
        frame_ms: int = 20,
        capture_timeout_s: float = 2.0,
        queue_frames: int = 50,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if profile.capture.kind != "udp_multicast":
            raise ValueError(
                "unitree_g1_native 仅支持 capture.kind=udp_multicast，"
                f"Profile {profile.profile!r} 为 {profile.capture.kind!r}"
            )
        super().__init__(
            profile,
            backend=backend or _NullBackend(),
            target_sample_rate=target_sample_rate,
            target_channels=target_channels,
            target_bit_depth=target_bit_depth,
            frame_ms=frame_ms,
            capture_timeout_s=capture_timeout_s,
            queue_frames=queue_frames,
            clock=clock,
        )
        self.app_name = app_name
        self.stream_id = stream_id
        self._audio_client = audio_client
        self._socket_factory = socket_factory or _default_socket
        self._socket: socket.socket | None = None
        self._udp_thread: threading.Thread | None = None
        self._udp_running = False

    # ------------------------------------------------------------ 采集（组播 UDP）

    async def _subscribe(self) -> None:
        self._start_multicast()

    def _start_multicast(self) -> None:
        capture = self.profile.capture
        try:
            sock = self._socket_factory()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", capture.multicast_port))
            membership = struct.pack(
                "4s4s",
                socket.inet_aton(capture.multicast_address),
                socket.inet_aton("0.0.0.0"),
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            sock.settimeout(0.5)
        except OSError as exc:
            raise RuntimeError(
                f"加入组播失败 {capture.multicast_address}:{capture.multicast_port}：{exc}"
            ) from exc
        self._socket = sock
        self._udp_running = True
        self._udp_thread = threading.Thread(
            target=self._udp_loop, name="robotchat-g1-mic", daemon=True
        )
        self._udp_thread.start()
        logger.info(
            "g1_multicast_joined",
            profile=self.profile_name,
            address=capture.multicast_address,
            port=capture.multicast_port,
        )

    def _udp_loop(self) -> None:
        sock = self._socket
        while self._udp_running and sock is not None:
            try:
                data = sock.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                break
            if data:
                self._on_audio(bytes(data), None)

    def _stop_multicast(self) -> None:
        self._udp_running = False
        thread = self._udp_thread
        if thread is not None:
            thread.join(timeout=1.0)
            self._udp_thread = None
        sock = self._socket
        if sock is not None:
            try:
                sock.close()
            except OSError:
                logger.warning("g1_multicast_close_failed", profile=self.profile_name)
            self._socket = None

    async def close(self) -> None:
        self._stop_multicast()
        await super().close()

    # ------------------------------------------------------------ 播放（DDS RPC）

    async def play(self, chunk: AudioChunk) -> None:
        if not self._opened or self._closed:
            raise RuntimeError("设备适配器未打开：play() 前须先 open()")
        payload = self._prepare_playback_payload(chunk)
        client = self._client()
        await asyncio.to_thread(client.PlayStream, self.app_name, self.stream_id, payload)
        self.played_chunks += 1

    async def flush(self) -> None:
        started = self._clock()
        client = self._client()
        await asyncio.to_thread(client.PlayStop, self.app_name)
        self._mark_flush(started)

    def _client(self) -> AudioClientLike:
        if self._audio_client is not None:
            return self._audio_client
        try:
            channel_module = importlib.import_module("unitree_sdk2py.core.channel")
            audio_module = importlib.import_module("unitree_sdk2py.g1.audio.g1_audio_client")
        except ImportError as exc:
            raise RuntimeError(
                "unitree_sdk2py 未安装：宇树 G1 播放需在已安装 SDK 的本体环境运行"
            ) from exc
        iface = os.environ.get(SDK_NETWORK_IFACE_ENV, "eth0")
        channel_module.ChannelFactoryInitialize(0, iface)
        client: AudioClientLike = audio_module.AudioClient()
        set_timeout = getattr(client, "SetTimeout", None)
        if callable(set_timeout):
            set_timeout(10.0)
        init = getattr(client, "Init", None)
        if callable(init):
            init()
        self._audio_client = client
        logger.info("g1_audio_client_ready", profile=self.profile_name, iface=iface)
        return client

    # ------------------------------------------------------------ 诊断

    def snapshot(self) -> dict[str, Any]:
        data = super().snapshot()
        data["multicast_running"] = self._udp_running
        data["stream_id"] = self.stream_id
        return data


__all__ = [
    "AudioClientLike",
    "DEFAULT_APP_NAME",
    "DEFAULT_STREAM_ID",
    "SDK_NETWORK_IFACE_ENV",
    "UnitreeG1Adapter",
]
