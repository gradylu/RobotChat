"""REST 执行适配器扩展点骨架（T-5.7）。

A6 已推迟 IoT 需求：本期不实现、不验收。保留注册点是为了证明
「新增适配器只需一个文件 + 配置」的扩展性（C-87）。
"""

from __future__ import annotations

from typing import Any

from robotchat.core.cancel import CancellationToken
from robotchat.health import HealthStatus
from robotchat.tools.base import ToolCall, ToolResult
from robotchat.tools.registry import register_adapter


@register_adapter("rest")
class RestToolAdapter:
    """预留：通过 HTTP 调用外部控制接口（本期不实现）。"""

    name = "rest"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = dict(config or {})

    async def execute(self, call: ToolCall, cancel: CancellationToken) -> ToolResult:
        return ToolResult(
            ok=False,
            error="REST 工具适配器尚未实现（A6 推迟，本期不验收）",
            error_kind="adapter",
        )

    async def health(self) -> HealthStatus:
        return HealthStatus.degraded("骨架实现，未启用")


__all__ = ["RestToolAdapter"]
