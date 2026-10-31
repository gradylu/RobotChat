"""WebSocket 事件网关（T-6.1 / design §10.2、C-90）。

- 端点 `/ws/session/{session_id}`，协议版本化（`v=1`）；
- 服务端推送状态机、ASR、LLM、播报、打断等事件；
- 客户端指令白名单校验后转交编排层，不回显未授权指令；
- 连接空闲超过 30 分钟自动断开（NFR-5 / C-98）。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from robotchat.api.auth import SESSION_IDLE_SECONDS
from robotchat.api.schemas import (
    CLIENT_COMMANDS,
    PROTOCOL_VERSION,
    ClientMessage,
    envelope,
)
from robotchat.observability.logging import get_logger

logger = get_logger("robotchat.api.ws")

router = APIRouter()

# 编排层内部事件名 → 协议事件名（design §10.2 中的既有名称保持原样）
_EVENT_ALIASES = {
    "barge_in.accepted": "barge_in",
    "barge_in.updated": "barge_in.config",
}

_METRICS_INTERVAL_S = 1.0


def _set_nested(target: dict[str, Any], path: str, value: Any) -> None:
    keys = [part for part in path.split(".") if part]
    node = target
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[keys[-1]] = value


async def _dispatch(
    runtime: Any, session_id: str, message: ClientMessage
) -> dict[str, Any]:
    """执行客户端指令；返回 ack 负载（异常以 error 事件返回）。"""
    dialogue = runtime.dialogue
    if dialogue is None:
        return {"accepted": False, "reason": "编排层未初始化"}

    kind = message.type
    payload = message.payload
    if kind in {"session.start", "wake.trigger"}:
        result = await dialogue.manual_wake(
            phrase=str(payload.get("phrase", "manual"))
        )
        return {"accepted": True, "command": kind, **result}
    if kind in {"session.stop", "session.sleep"}:
        result = await dialogue.sleep()
        return {"accepted": True, "command": kind, **result}
    if kind == "language.set":
        snapshot = await dialogue.set_language(
            payload.get("mode"), payload.get("lang")
        )
        return {"accepted": True, "command": kind, "language": snapshot}
    if kind == "interrupt":
        result = await dialogue.interrupt(str(payload.get("reason", "manual")))
        return {"accepted": True, "command": kind, **result}
    if kind == "text.input":
        text = str(payload.get("text", "")).strip()
        if not text:
            return {"accepted": False, "reason": "text 不能为空"}
        lang = payload.get("lang")
        asyncio.create_task(
            dialogue.handle_text(text, str(lang) if lang else None), name="ws-text-input"
        )
        return {"accepted": True, "command": kind, "text": text}
    if kind in {"mic.mute", "mic.unmute"}:
        return {
            "accepted": True,
            "command": kind,
            **await dialogue.set_muted(kind == "mic.mute"),
        }
    if kind == "config.update":
        patch = payload.get("patch")
        if not isinstance(patch, dict):
            patch = {}
            path = str(payload.get("path", "") or "")
            if path:
                _set_nested(patch, path, payload.get("value"))
        if not patch:
            return {"accepted": False, "reason": "缺少配置路径或补丁"}
        result = await runtime.apply_config_patch(patch)
        return {
            "accepted": True,
            "command": kind,
            "applied": result["applied"],
            "restart_required": result["restart_required"],
        }
    return {"accepted": False, "reason": f"不支持的指令：{kind}"}


@router.websocket("/ws/session/{session_id}")
async def session_websocket(websocket: WebSocket, session_id: str) -> None:
    runtime = websocket.app.state.runtime
    ok, reason = runtime.auth.check(websocket.query_params.get("token", ""))
    if not ok:
        await websocket.close(code=4401, reason=reason)
        return
    await websocket.accept()

    lock = asyncio.Lock()

    async def send(payload: dict[str, Any]) -> None:
        async with lock:
            with contextlib.suppress(Exception):
                await websocket.send_json(payload)

    async def sink(kind: str, payload: dict[str, Any], turn_id: int | None) -> None:
        await send(envelope(_EVENT_ALIASES.get(kind, kind), payload, turn_id))

    dialogue = runtime.dialogue
    if dialogue is not None:
        dialogue.subscribe_ui(sink)
    snapshot = dialogue.snapshot() if dialogue is not None else {}
    await send(
        envelope(
            "session.snapshot",
            {
                "protocol_version": PROTOCOL_VERSION,
                "session_id": session_id,
                "active_session_id": dialogue.session_id if dialogue is not None else "",
                "running": bool(snapshot.get("running")),
                "state": snapshot.get("state", "UNKNOWN"),
                "wake": runtime.wake_snapshot(),
                "metrics": runtime.metrics.counters(),
                "snapshot": snapshot,
            },
        )
    )
    logger.info("ws_connected", session_id=session_id)

    async def metrics_loop() -> None:
        while True:
            await asyncio.sleep(_METRICS_INTERVAL_S)
            await send(
                envelope(
                    "metrics.snapshot",
                    {
                        "stages": runtime.metrics.snapshot(),
                        "counters": runtime.metrics.counters(),
                    },
                )
            )

    metrics_task = asyncio.create_task(metrics_loop(), name="ws-metrics")
    last_active = time.monotonic()
    # 独立接收任务 + 超时等待：避免 wait_for 取消底层 receive 导致断连消息丢失
    receive_task: asyncio.Task[Any] = asyncio.create_task(websocket.receive_json())
    try:
        while True:
            done, _ = await asyncio.wait({receive_task}, timeout=1.0)
            if not done:
                if time.monotonic() - last_active >= SESSION_IDLE_SECONDS:
                    await send(
                        envelope(
                            "error",
                            {
                                "code": "idle_timeout",
                                "message": "连接空闲超时",
                                "recoverable": True,
                            },
                        )
                    )
                    break
                continue
            raw = receive_task.result()
            receive_task = asyncio.create_task(websocket.receive_json())
            last_active = time.monotonic()
            try:
                message = ClientMessage.model_validate(raw)
            except ValidationError:
                await send(
                    envelope(
                        "error",
                        {
                            "code": "bad_message",
                            "message": "消息格式非法",
                            "recoverable": True,
                        },
                    )
                )
                continue
            if message.v != PROTOCOL_VERSION:
                await send(
                    envelope(
                        "error",
                        {
                            "code": "protocol_mismatch",
                            "message": (
                                f"协议版本不匹配：客户端 {message.v}，"
                                f"服务端 {PROTOCOL_VERSION}"
                            ),
                            "recoverable": False,
                        },
                    )
                )
                continue
            if message.type not in CLIENT_COMMANDS:
                await send(
                    envelope(
                        "error",
                        {
                            "code": "unknown_command",
                            "message": f"未知指令：{message.type}",
                            "recoverable": True,
                        },
                    )
                )
                continue
            try:
                result = await _dispatch(runtime, session_id, message)
            except Exception as exc:
                logger.exception("ws_command_failed", command=message.type)
                await send(
                    envelope(
                        "error",
                        {"code": "command_failed", "message": repr(exc), "recoverable": True},
                    )
                )
                continue
            await send(envelope("command.ack", {"command": message.type, **result}))
    except WebSocketDisconnect:
        pass
    finally:
        receive_task.cancel()
        metrics_task.cancel()
        # CancelledError 继承 BaseException，必须显式捕获
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await receive_task
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await metrics_task
        if dialogue is not None:
            with contextlib.suppress(Exception):
                dialogue.unsubscribe_ui(sink)
        logger.info("ws_disconnected", session_id=session_id)


__all__ = ["router"]
