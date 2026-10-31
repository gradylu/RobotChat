"""设备抽象与能力一致性契约（T-1A.5 / C-167）。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from pydantic import ValidationError

from robotchat.audio.adapters import (
    UnknownDeviceAdapterError,
    create_device_adapter,
    registered_adapters,
)
from robotchat.audio.adapters.base_ros_audio import BaseRosAudioAdapter
from robotchat.audio.buffer import float_to_pcm16
from robotchat.audio.device import AdapterAudioTransport
from robotchat.audio.frame import AudioChunk, AudioFrame

from .conftest import BASE_PROFILE, FakeBackend, flush_profile, make_profile


def test_registry_exposes_generic_adapter() -> None:
    assert "base_ros_audio" in registered_adapters()


def test_unknown_adapter_is_rejected() -> None:
    profile = make_profile(BASE_PROFILE, profile="contract_unknown", adapter="missing_adapter")
    with pytest.raises(UnknownDeviceAdapterError):
        create_device_adapter(profile)


def test_capability_flush_mismatch_rejected() -> None:
    raw = dict(BASE_PROFILE)
    raw = {**raw, "capabilities": {**raw["capabilities"], "playback_flush": "stream_stop"}}
    with pytest.raises(ValidationError):
        make_profile(raw)


def test_hardware_aec_requires_echo_reference() -> None:
    raw = dict(BASE_PROFILE)
    raw = {
        **raw,
        "capabilities": {**raw["capabilities"], "hardware_aec": True, "echo_reference": False},
    }
    with pytest.raises(ValidationError):
        make_profile(raw)


def test_volume_control_requires_volume_block() -> None:
    raw = dict(BASE_PROFILE)
    raw = {**raw, "capabilities": {**raw["capabilities"], "volume_control": True}, "volume": None}
    with pytest.raises(ValidationError):
        make_profile(raw)


async def test_adapter_transport_delegates_contracts() -> None:
    """业务层只看到 AudioTransport 的五个方法，机型差异留在适配器内。"""
    profile = flush_profile("service")
    backend = FakeBackend()
    adapter = BaseRosAudioAdapter(profile, backend=backend)
    transport = AdapterAudioTransport(adapter)

    assert transport.name == f"device:{profile.profile}"
    await transport.open()
    assert backend.opened is True

    # 24kHz 双声道 20ms = 1920 字节，转换后恰好产出 1 帧 16kHz 单声道
    backend.emit(bytes(1920), stamp=0)
    await asyncio.sleep(0)
    frames: list[AudioFrame] = []
    stream: AsyncIterator[AudioFrame] = transport.capture()
    async for frame in stream:
        frames.append(frame)
        break
    assert frames and frames[0].seq == 1

    await transport.play(AudioChunk(data=float_to_pcm16([0.0] * 320), sample_rate=16000))
    await transport.flush()
    assert backend.service_calls

    await transport.close()
    assert backend.closed is True
