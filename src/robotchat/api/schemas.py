"""接入层协议模型（T-6.1 / design §10.2）。

- WebSocket 消息统一为 `{v, type, ts, turn_id, payload}`，协议版本随 `api.protocol_version` 演进；
- 服务端事件与客户端指令共用同一信封，未知 `type` 由接入层显式拒绝而不是静默忽略。
"""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PROTOCOL_VERSION = 1

# 客户端指令白名单（design §10.2「客户端 → 服务端」）
CLIENT_COMMANDS: tuple[str, ...] = (
    "session.start",
    "session.stop",
    "wake.trigger",
    "session.sleep",
    "language.set",
    "interrupt",
    "text.input",
    "mic.mute",
    "mic.unmute",
    "config.update",
)


class ClientMessage(BaseModel):
    """客户端指令信封。"""

    model_config = ConfigDict(extra="ignore")

    v: int = PROTOCOL_VERSION
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ServerMessage(BaseModel):
    """服务端事件信封。"""

    model_config = ConfigDict(extra="ignore")

    v: int = PROTOCOL_VERSION
    type: str
    ts: int = Field(default_factory=lambda: int(time.time() * 1000))
    turn_id: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


def envelope(
    kind: str, payload: dict[str, Any] | None = None, turn_id: int | None = None
) -> dict[str, Any]:
    """构造服务端消息（dict 形式便于直接 `send_json`）。"""
    return ServerMessage(
        type=kind, payload=payload or {}, turn_id=turn_id
    ).model_dump()


CommandKind = Literal[
    "session.start",
    "session.stop",
    "wake.trigger",
    "session.sleep",
    "language.set",
    "interrupt",
    "text.input",
    "mic.mute",
    "mic.unmute",
    "config.update",
]


__all__ = [
    "PROTOCOL_VERSION",
    "CLIENT_COMMANDS",
    "ClientMessage",
    "ServerMessage",
    "envelope",
    "CommandKind",
]
