"""机型适配器单测：X2 的 token / 音频焦点、A2·A3 的 protobuf 与 HTTP 音量、G1 的组播 + SDK 播放."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from robotchat.audio.adapters import registered_adapters
from robotchat.audio.adapters.agibot_aimdk_msgs import AgibotAimdkMsgsAdapter
from robotchat.audio.adapters.agibot_ros2_plugin import AgibotRos2PluginAdapter
from robotchat.audio.adapters.base_ros_audio import PublishSpec, ServiceSpec
from robotchat.audio.adapters.unitree_g1_native import UnitreeG1Adapter
from robotchat.audio.frame import AudioChunk
from robotchat.config.schema import DeviceProfileSchema

X2_PROFILE: dict[str, Any] = {
    "profile": "agibot_x2_ultra",
    "adapter": "agibot_aimdk_msgs",
    "capabilities": {
        "capture": True,
        "playback": True,
        "echo_reference": True,
        "hardware_aec": False,
        "volume_control": True,
        "playback_flush": "topic_msg",
    },
    "capture": {
        "topic": "/agent/process_audio_output",
        "msg_type": "aimdk_msgs/msg/ProcessedAudioOutput",
        "fields": {"data": "audio_data", "stream": "stream_id", "vad": "audio_vad_state"},
        "sample_rate": 16000,
        "channels": 1,
        "bit_depth": 16,
        "frame_ms": 40,
    },
    "playback": {
        "topic": "/aima/hal/audio/playback",
        "msg_type": "aimdk_msgs/msg/AudioPlayback",
        "fields": {"data": "audio_data", "token": "token_id"},
        "sample_rate": 16000,
        "channels": 1,
        "bit_depth": 16,
        "focus": {
            "request_service": "/aimdk_5Fmsgs/srv/RequestAudioFocus",
            "abandon_service": "/aimdk_5Fmsgs/srv/AbandonAudioFocus",
            "request_msg_type": "aimdk_msgs/srv/RequestAudioFocus",
            "abandon_msg_type": "aimdk_msgs/srv/AbandonAudioFocus",
            "priority": 6,
        },
        "flush": {
            "kind": "topic_msg",
            "topic": "/aima/hal/audio/playback",
            "msg_type": "aimdk_msgs/msg/AudioPlayback",
        },
    },
    "volume": {
        "kind": "ros2_service",
        "service": "/aimdk_5Fmsgs/srv/SetVolume",
        "msg_type": "aimdk_msgs/srv/SetVolume",
        "field": "audio_volume",
        "range": [0.0, 100.0],
    },
}

A2_PROFILE: dict[str, Any] = {
    "profile": "agibot_a2",
    "adapter": "agibot_ros2_plugin",
    "capabilities": {
        "capture": True,
        "playback": True,
        "echo_reference": False,
        "hardware_aec": False,
        "volume_control": True,
        "playback_flush": "none",
    },
    "capture": {
        "topic": "/agent/process_audio_output/pb_3Aaimdk_2Eprotocol_2EProcessedAudioOutput",
        "msg_type": "ros2_plugin_proto/msg/RosMsgWrapper",
        "payload": "protobuf",
        "payload_type": "aimdk.protocol.ProcessedAudioOutput",
        "fields": {"data": "msg"},
        "sample_rate": 16000,
        "channels": 1,
        "bit_depth": 16,
    },
    "playback": {
        "topic": "/audiohal/audio/playback",
        "msg_type": "audio_msgs/msg/AudioPlayback",
        "sample_rate": 16000,
        "channels": 1,
        "bit_depth": 16,
        "flush": {"kind": "none"},
    },
    "volume": {
        "kind": "http_rpc",
        "endpoint": "http://robot:56666/rpc/aimdk.protocol.HalAudioService/SetAudioVolume",
        "field": "audio_volume",
        "mute_field": "is_mute",
        "range": [0.0, 70.0],
    },
}

G1_PROFILE: dict[str, Any] = {
    "profile": "unitree_g1",
    "adapter": "unitree_g1_native",
    "capabilities": {
        "capture": True,
        "playback": True,
        "echo_reference": False,
        "hardware_aec": False,
        "volume_control": False,
        "playback_flush": "stream_stop",
    },
    "capture": {
        "kind": "udp_multicast",
        "multicast_address": "239.168.123.161",
        "multicast_port": 5555,
        "payload": "raw_pcm",
        "sample_rate": 16000,
        "channels": 1,
        "bit_depth": 16,
    },
    "playback": {
        "kind": "dds_rpc",
        "rpc_api": "unitree::robot::g1::AudioClient::PlayStream",
        "rpc_stop_api": "unitree::robot::g1::AudioClient::PlayStop",
        "payload": "raw_pcm",
        "sample_rate": 16000,
        "channels": 1,
        "bit_depth": 16,
        "flush": {"kind": "stream_stop"},
    },
}


class _RecordingBackend:
    """记录发布与服务调用的模拟 DDS 后端。"""

    def __init__(self) -> None:
        self.published: list[tuple[PublishSpec, bytes]] = []
        self.service_calls: list[ServiceSpec] = []

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def subscribe(self, spec: Any) -> None:
        return None

    async def publish(self, spec: PublishSpec, payload: bytes) -> None:
        self.published.append((spec, payload))

    async def publish_float(self, spec: PublishSpec, value: float) -> None:
        return None

    async def call_service(self, spec: ServiceSpec) -> None:
        self.service_calls.append(spec)


class _FakeAudioClient:
    def __init__(self) -> None:
        self.streams: list[tuple[str, int, bytes]] = []
        self.stops: list[str] = []

    def PlayStream(self, app_name: str, stream_id: int, pcm: bytes) -> None:
        self.streams.append((app_name, stream_id, pcm))

    def PlayStop(self, app_name: str) -> None:
        self.stops.append(app_name)


class _FakeSocket:
    """组播 socket 替身：recv 超时返回，避免测试忙等。"""

    def __init__(self) -> None:
        self.bound: tuple[str, int] | None = None
        self.options: list[tuple[int, int, bytes]] = []
        self.closed = False

    def setsockopt(self, level: int, opt: int, value: Any) -> None:
        self.options.append((level, opt, value))

    def bind(self, addr: tuple[str, int]) -> None:
        self.bound = addr

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def recv(self, size: int) -> bytes:
        time.sleep(0.02)
        raise TimeoutError

    def close(self) -> None:
        self.closed = True


def _profile(raw: dict[str, Any]) -> DeviceProfileSchema:
    return DeviceProfileSchema.model_validate(raw)


def _chunk(samples: int = 320) -> AudioChunk:
    data = np.zeros(samples, dtype="<i2").tobytes()
    return AudioChunk(data=data, sample_rate=16000, channels=1, bit_depth=16)


def test_machine_adapters_registered() -> None:
    names = registered_adapters()
    expected = {"base_ros_audio", "agibot_aimdk_msgs", "agibot_ros2_plugin", "unitree_g1_native"}
    assert expected <= set(names)


def test_x2_play_carries_token_pkg_and_priority_and_flush_bumps_token() -> None:
    backend = _RecordingBackend()
    adapter = AgibotAimdkMsgsAdapter(_profile(X2_PROFILE), backend=backend, pkg_name="robotchat")

    async def scenario() -> None:
        await adapter.open()
        await adapter.play(_chunk())
        await adapter.flush()
        await adapter.close()

    asyncio.run(scenario())

    focus = backend.service_calls[0]
    assert focus.service == "/aimdk_5Fmsgs/srv/RequestAudioFocus"
    assert focus.fields["pkg_name"] == "robotchat"
    assert focus.fields["priority"] == 6
    assert backend.service_calls[-1].service == "/aimdk_5Fmsgs/srv/AbandonAudioFocus"

    first_spec, first_payload = backend.published[0]
    assert first_spec.extra_fields["token_id"] == 1
    assert first_spec.extra_fields["pkg_name"] == "robotchat"
    assert len(first_payload) == 640

    flush_spec, flush_payload = backend.published[-1]
    assert flush_spec.extra_fields["token_id"] == 2  # 换 token 清空本体播放缓存
    assert flush_payload == b""
    assert adapter.flush_count == 1


def test_x2_volume_goes_through_ros2_service() -> None:
    backend = _RecordingBackend()
    adapter = AgibotAimdkMsgsAdapter(_profile(X2_PROFILE), backend=backend)

    asyncio.run(adapter.set_volume(60.0))

    call = backend.service_calls[-1]
    assert call.service == "/aimdk_5Fmsgs/srv/SetVolume"
    assert call.fields == {"audio_volume": 60}


def test_a2_decodes_protobuf_payload_to_frames() -> None:
    pcm = np.zeros(320, dtype="<i2").tobytes()
    adapter = AgibotRos2PluginAdapter(
        _profile(A2_PROFILE),
        backend=_RecordingBackend(),
        decoder=lambda payload, _type: payload,
    )

    frames = adapter._convert(pcm, None)

    assert len(frames) == 1
    assert frames[0].sample_rate == 16000
    assert frames[0].data == pcm


def test_a2_decode_failure_is_counted_and_skipped() -> None:
    def broken(_payload: bytes, _type: str) -> bytes:
        raise ValueError("pb 缺定义")

    adapter = AgibotRos2PluginAdapter(
        _profile(A2_PROFILE), backend=_RecordingBackend(), decoder=broken
    )

    frames = adapter._convert(b"\x01\x02", None)

    assert frames == []
    assert adapter.snapshot()["decode_failures"] == 1


def test_a2_flush_without_stop_capability_is_noop() -> None:
    backend = _RecordingBackend()
    adapter = AgibotRos2PluginAdapter(_profile(A2_PROFILE), backend=backend)

    asyncio.run(adapter.flush())

    assert backend.published == []
    assert adapter.flush_count == 0


def test_a2_volume_posts_http_rpc() -> None:
    recorded: list[tuple[str, dict[str, Any]]] = []

    def fake_post(url: str, body: dict[str, Any]) -> None:
        recorded.append((url, body))

    adapter = AgibotRos2PluginAdapter(
        _profile(A2_PROFILE), backend=_RecordingBackend(), http_post=fake_post
    )

    asyncio.run(adapter.set_volume(60.0))

    assert recorded == [
        (
            "http://robot:56666/rpc/aimdk.protocol.HalAudioService/SetAudioVolume",
            {"audio_volume": 60, "is_mute": False},
        )
    ]


def test_g1_capture_joins_multicast_and_play_uses_sdk() -> None:
    fake_socket = _FakeSocket()
    client = _FakeAudioClient()
    adapter = UnitreeG1Adapter(
        _profile(G1_PROFILE),
        audio_client=client,
        socket_factory=lambda: fake_socket,
        app_name="robotchat",
        stream_id=2,
    )

    async def scenario() -> None:
        await adapter.open()
        assert fake_socket.bound == ("", 5555)
        await adapter.play(_chunk())
        await adapter.flush()
        await adapter.close()

    asyncio.run(scenario())

    assert fake_socket.closed is True
    assert client.streams[0][0] == "robotchat"
    assert client.streams[0][1] == 2
    assert len(client.streams[0][2]) == 640
    assert client.stops == ["robotchat"]
    assert adapter.flush_count == 1


def test_g1_rejects_non_multicast_profile() -> None:
    raw = dict(G1_PROFILE)
    raw["capture"] = {
        "topic": "/mic",
        "msg_type": "std_msgs/msg/UInt8MultiArray",
        "sample_rate": 16000,
        "channels": 1,
        "bit_depth": 16,
    }
    with pytest.raises(ValueError, match="udp_multicast"):
        UnitreeG1Adapter(_profile(raw))


def test_profile_rejects_incomplete_capabilities() -> None:
    udp_missing_port = dict(G1_PROFILE)
    udp_missing_port["capture"] = {
        "kind": "udp_multicast",
        "multicast_address": "239.168.123.161",
        "payload": "raw_pcm",
        "sample_rate": 16000,
    }
    with pytest.raises(ValidationError, match="multicast_address"):
        DeviceProfileSchema.model_validate(udp_missing_port)

    dds_missing_api = dict(G1_PROFILE)
    dds_missing_api["playback"] = {
        "kind": "dds_rpc",
        "payload": "raw_pcm",
        "sample_rate": 16000,
        "flush": {"kind": "stream_stop"},
    }
    with pytest.raises(ValidationError, match="rpc_api"):
        DeviceProfileSchema.model_validate(dds_missing_api)

    http_missing_endpoint = dict(A2_PROFILE)
    http_missing_endpoint["volume"] = {"kind": "http_rpc", "range": [0.0, 100.0]}
    with pytest.raises(ValidationError, match="endpoint"):
        DeviceProfileSchema.model_validate(http_missing_endpoint)
