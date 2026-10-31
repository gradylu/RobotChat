"""适配器注册包：导入即注册全部内置适配器。

本期实现 ROS2 与内置播报；REST / MQTT 仅保留扩展点骨架（T-5.7，A6 推迟）。
"""

from __future__ import annotations

from robotchat.tools.adapters import mqtt_adapter, rest_adapter, ros2_adapter
from robotchat.tools.adapters.ros2_adapter import Ros2ToolAdapter

# 显式引用避免 linter 判定未使用；注册发生在模块导入时
_REGISTERED = (ros2_adapter, rest_adapter, mqtt_adapter)

__all__ = ["Ros2ToolAdapter"]
