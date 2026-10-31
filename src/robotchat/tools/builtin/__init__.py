"""内置工具集：播报 / 导航 / IO 控制（T-5.8、design §9.5）。

定义来自 `config/tools.yaml`；此处提供默认定义与「播报」适配器，
保证未配置时 `/api/tools` 仍有可用的最小工具集。
"""

from __future__ import annotations

from typing import Any

from robotchat.tools.base import ToolDefinition

# 默认定义仅作兜底，实际以 config/tools.yaml 为准（可覆盖 / 扩展）
BUILTIN_DEFINITIONS: dict[str, dict[str, Any]] = {
    "speak": {
        "description": "让机器人播报指定内容",
        "risk": "low",
        "timeout_s": 10,
        "adapter": "builtin",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": 1, "maxLength": 200}
            },
            "required": ["text"],
        },
    },
    "navigate_to": {
        "description": "导航到指定地点",
        "risk": "high",
        "timeout_s": 120,
        "adapter": "ros2",
        "target": "/robot/navigate_to",
        "kind": "action",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "minLength": 1},
                "speed": {"type": "number", "minimum": 0.1, "maximum": 1.0},
            },
            "required": ["location"],
        },
    },
    "set_io": {
        "description": "设置机器人 IO 输出通道",
        "risk": "medium",
        "timeout_s": 10,
        "adapter": "ros2",
        "target": "/robot/set_io",
        "kind": "service",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "integer", "minimum": 0, "maximum": 7},
                "value": {"type": "boolean"},
            },
            "required": ["channel", "value"],
        },
    },
}

_RESERVED_KEYS = {
    "description",
    "risk",
    "timeout_s",
    "adapter",
    "target",
    "parameters",
}


def load_definitions(config: dict[str, Any]) -> list[ToolDefinition]:
    """合并默认定义与配置，产出不可变的 `ToolDefinition` 列表。"""
    merged: dict[str, Any] = {name: dict(item) for name, item in BUILTIN_DEFINITIONS.items()}
    for name, item in (config or {}).items():
        merged[name] = dict(item or {})
    definitions: list[ToolDefinition] = []
    for name, item in merged.items():
        parameters = dict(item.get("parameters") or {})
        options = {key: value for key, value in item.items() if key not in _RESERVED_KEYS}
        definitions.append(
            ToolDefinition(
                name=name,
                description=str(item.get("description", name)),
                risk=str(item.get("risk", "low")),  # type: ignore[arg-type]
                parameters=parameters,
                timeout_s=float(item.get("timeout_s", 30) or 30),
                adapter=str(item.get("adapter", "builtin")),
                target=str(item.get("target", "")),
                options=options,
            )
        )
    return definitions


__all__ = ["BUILTIN_DEFINITIONS", "load_definitions"]
