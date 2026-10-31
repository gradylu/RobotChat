"""播报工具适配器（T-5.8）：只产出文本，由编排层用 TTS 播报。"""

from __future__ import annotations

from typing import Any

from robotchat.core.cancel import CancellationToken
from robotchat.health import HealthStatus
from robotchat.tools.base import ToolCall, ToolResult
from robotchat.tools.registry import register_adapter


@register_adapter("builtin")
class BuiltinToolAdapter:
    """进程内轻量工具：当前仅播报，后续可在配置里扩展更多内置动作。"""

    name = "builtin"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._tools = dict((config or {}).get("tools", {}))

    async def execute(self, call: ToolCall, cancel: CancellationToken) -> ToolResult:
        if call.name == "speak":
            text = str(call.args.get("text", "")).strip()
            if not text:
                return ToolResult(ok=False, error="播报内容为空", error_kind="invalid")
            return ToolResult(ok=True, output=text)
        return ToolResult(
            ok=False,
            error=f"内置适配器不支持工具 {call.name}",
            error_kind="adapter",
        )

    async def health(self) -> HealthStatus:
        return HealthStatus.ok(f"内置工具 {len(self._tools)} 个")


__all__ = ["BuiltinToolAdapter"]
