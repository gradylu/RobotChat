"""设备适配器注册表（T-1A.1 / design §3.6.3 第 3 步）。

新增机型只允许：新增 Profile + 新增 / 复用适配器 + 登记到 `device_adapters`，
不得改动状态机、编排器与 VAD / ASR / LLM / TTS / KWS 代码（FR-16.7）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from robotchat.audio.device import AudioDeviceAdapter
from robotchat.config.schema import DeviceProfileSchema

DEVICE_ADAPTERS: dict[str, Callable[..., AudioDeviceAdapter]] = {}


class UnknownDeviceAdapterError(ValueError):
    """Profile 声明的适配器未注册：直接报错，不做猜测性回退（FR-16.9）。"""


def register_device_adapter(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(factory: Callable[..., Any]) -> Callable[..., Any]:
        DEVICE_ADAPTERS[name] = factory
        return factory

    return decorator


def registered_adapters() -> list[str]:
    return sorted(DEVICE_ADAPTERS)


def create_device_adapter(
    profile: DeviceProfileSchema, **kwargs: Any
) -> AudioDeviceAdapter:
    factory = DEVICE_ADAPTERS.get(profile.adapter)
    if factory is None:
        raise UnknownDeviceAdapterError(
            f"机型 Profile {profile.profile!r} 声明的适配器 {profile.adapter!r} 未注册；"
            f"可用适配器：{registered_adapters() or '(无)'}"
        )
    return factory(profile, **kwargs)


# 导入即登记通用 ROS2 适配器（其他机型适配器在此追加 import）
from robotchat.audio.adapters import base_ros_audio  # noqa: E402, F401

__all__ = [
    "DEVICE_ADAPTERS",
    "UnknownDeviceAdapterError",
    "create_device_adapter",
    "register_device_adapter",
    "registered_adapters",
]
