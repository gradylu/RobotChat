"""MQTT 执行适配器扩展点骨架（T-5.7）。

A6 已推迟 IoT 需求：本期不实现、不验收，仅保留注册点。
"""

from __future__ import annotations

from typing import Any

from robotchat.core.cancel import CancellationToken
from robotchat.health import HealthStatus
from robotchat.tools.base import ToolCall, ToolResult
from robotchat.tools.registry import register_adapter


@register_adapter("mqtt")
class MqttToolAdapter:
    """预留：通过 MQTT 下发 IoT 设备指令（本期不实现）。"""

    name = "mqtt"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = dict(config or {})

    async def execute(self, call: ToolCall, cancel: CancellationToken) -> ToolResult:
        return ToolResult(
            ok=False,
            error="MQTT 工具适配器尚未实现（A6 推迟，本期不验收）",
            error_kind="adapter",
        )

    async def health(self) -> HealthStatus:
        return HealthStatus.degraded("骨架实现，未启用")


__all__ = ["MqttToolAdapter"]
