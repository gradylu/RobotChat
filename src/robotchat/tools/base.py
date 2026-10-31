"""工具定义、执行适配与结果模型（FR-9、T-5.4）。

`ToolDefinition` 是 LLM 可见的唯一契约：名称 / 描述 / JSON Schema 参数 /
风险等级 / 超时 / 适配器与目标，业务层不出现具体 ROS2 调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from robotchat.core.cancel import CancellationToken
from robotchat.health import HealthStatus

RiskLevel = Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    risk: RiskLevel = "low"
    parameters: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = 30.0
    adapter: str = "ros2"
    target: str = ""
    # 适配器执行所需的其他字段（消息类型 / action vs service 等）
    options: dict[str, Any] = field(default_factory=dict)

    def as_client_schema(self) -> dict[str, Any]:
        """转换为 LLM Function Calling 的 function 字段。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters or {"type": "object", "properties": {}},
        }

    def as_dict(self) -> dict[str, Any]:
        """REST `/api/tools` 输出（含风险等级，C-80）。"""
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk,
            "parameters": self.parameters,
            "timeout_s": self.timeout_s,
            "adapter": self.adapter,
            "target": self.target,
            "options": self.options,
        }


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    turn_id: int = 0


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    output: str = ""
    error: str | None = None
    duration_ms: float = 0.0
    error_kind: str = ""  # denied | invalid | timeout | cancelled | adapter

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "error_kind": self.error_kind,
        }


@runtime_checkable
class ToolAdapter(Protocol):
    """执行适配器（ROS2 / REST / MQTT），业务层只依赖本契约。"""

    name: str

    async def execute(self, call: ToolCall, cancel: CancellationToken) -> ToolResult: ...

    async def health(self) -> HealthStatus: ...


__all__ = ["RiskLevel", "ToolAdapter", "ToolCall", "ToolDefinition", "ToolResult"]
