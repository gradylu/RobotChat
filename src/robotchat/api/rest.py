"""REST 接入层（T-6.2 / design §10.3）。

覆盖健康、配置、驱动、会话、工具、指标、日志与诊断；写操作统一走口令鉴权
（T-6.4 / C-98），配置变更走「校验 + 预检 + 原子生效 + 回滚」（T-6.3 / C-57）。
"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from robotchat.api.auth import require_write
from robotchat.config.loader import ConfigError, masked_settings
from robotchat.observability.metrics import (
    INTENT_CLASSIFY,
    INTENT_FALLBACK,
    INTENT_LATE_ROUTE,
    INTENT_LOW_CONFIDENCE,
    INTENT_RULE,
    INTENT_TOTAL,
)
from robotchat.tools.base import ToolCall

if TYPE_CHECKING:
    from robotchat.runtime import AppRuntime

router = APIRouter(prefix="/api", tags=["system"])

_SESSION_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class LanguageUpdate(BaseModel):
    """语言开关请求：切换 fixed / auto 或直接指定语言（T-2A.7）。"""

    mode: Literal["fixed", "auto"] | None = None
    lang: str | None = None


class BargeInUpdate(BaseModel):
    """打断参数热更新请求（T-4.6）：只提交需要变更的字段。"""

    mode: Literal["enabled", "disabled"] | None = None
    guard_ms: int | None = None
    threshold_boost_db: float | None = None
    confirm_frames: int | None = None
    semantic_check: bool | None = None
    echo_similarity_threshold: float | None = None


class ToolInvokeRequest(BaseModel):
    """手动触发工具（调试用，T-5.4）。"""

    args: dict[str, Any] = Field(default_factory=dict)


class IntentTestRequest(BaseModel):
    """意图试跑请求（FR-17）：只收文本，不触发任何工具。"""

    text: str
    lang: str = ""


class SessionCreateRequest(BaseModel):
    session_id: str | None = None


class ProviderSwitchRequest(BaseModel):
    driver: str


def _runtime(request: Request) -> AppRuntime:
    return request.app.state.runtime


def _serialize(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    runtime: AppRuntime = _runtime(request)
    drivers = {kind: status.as_dict() for kind, status in runtime.drivers.items()}
    unhealthy = [kind for kind, status in runtime.drivers.items() if status.state != "ok"]
    overall = "degraded" if unhealthy else "ok"

    return {
        "status": overall,
        "ready": runtime.ready,
        "uptime_s": runtime.uptime_s(),
        "app": {"name": runtime.settings.app.name, "env": runtime.settings.app.env},
        "subsystems": {
            "config": {
                "state": "ok",
                "config_dir": str(runtime.config_dir),
                "data_dir": str(runtime.data_dir),
            },
            "audio": {
                "transport": runtime.settings.audio.transport,
                "device_profile": runtime.settings.audio.device_profile,
                "resolved_profile": {
                    "name": runtime.device_profile,
                    "source": runtime.device_profile_source,
                    "warnings": runtime.device_profile_warnings,
                },
                "aec": {
                    "requested": runtime.settings.audio.aec.mode,
                    "effective": runtime.aec_mode,
                    "detail": runtime.aec_detail,
                    "fallback_notice": runtime.settings.audio.aec.fallback_notice,
                },
            },
            "drivers": drivers,
            "providers": runtime.registry.snapshot(),
            "offline": {
                "state": "offline" if runtime.offline else "online",
                "detail": runtime.offline_detail,
            },
            "rag": runtime.rag.stats.as_dict() if runtime.rag is not None else {},
        },
    }


@router.get("/providers")
async def providers(request: Request) -> dict[str, Any]:
    """能力协商可见性（C-58）：capabilities、熔断状态与生效驱动。"""
    runtime: AppRuntime = _runtime(request)
    return {
        "providers": runtime.provider_report(),
        "drivers": {kind: status.as_dict() for kind, status in runtime.drivers.items()},
        "offline": {
            "state": "offline" if runtime.offline else "online",
            "detail": runtime.offline_detail,
        },
    }


@router.post("/providers/{kind}/switch", dependencies=[Depends(require_write)])
async def switch_provider(
    request: Request, kind: str, body: ProviderSwitchRequest
) -> JSONResponse:
    """热切换驱动（T-6.8 / C-53）：当前会话不中断。"""
    runtime: AppRuntime = _runtime(request)
    try:
        result = await runtime.switch_provider(kind, body.driver)
    except ConfigError as exc:
        return JSONResponse({"switched": False, "detail": str(exc)}, status_code=400)
    return JSONResponse(result)


@router.post("/providers/{kind}/{driver}/test", dependencies=[Depends(require_write)])
async def test_provider(request: Request, kind: str, driver: str) -> JSONResponse:
    """连通性测试（T-6.8 / C-95）：返回真实探活结果与耗时。"""
    runtime: AppRuntime = _runtime(request)
    try:
        result = await runtime.test_provider(kind, driver)
    except ConfigError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=400)
    return JSONResponse(result)


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """就绪探测（T-7.6 / C-124）：就绪前 503，就绪后 200 且含子系统明细。"""
    runtime: AppRuntime = _runtime(request)
    subsystems = {
        kind: {
            "state": status.state,
            "active": status.active,
            "effective": status.effective,
        }
        for kind, status in runtime.drivers.items()
    }
    payload: dict[str, Any] = {
        "ready": runtime.ready,
        "status": "ready" if runtime.ready else "starting",
        "uptime_s": runtime.uptime_s(),
        "subsystems": subsystems,
    }
    if not runtime.ready:
        payload["blocking"] = [
            kind
            for kind, status in runtime.drivers.items()
            if status.state == "unavailable"
        ]
        return JSONResponse(payload, status_code=503)
    unhealthy = [kind for kind, status in runtime.drivers.items() if status.state != "ok"]
    payload["degraded"] = unhealthy
    return JSONResponse(payload)


@router.get("/config")
async def get_config(request: Request) -> dict[str, Any]:
    return masked_settings(_runtime(request).settings)


@router.put("/config", dependencies=[Depends(require_write)])
async def update_config(request: Request, body: dict[str, Any]) -> JSONResponse:
    """更新配置（校验 + 预检 + 热生效），非法配置被拒并给出字段级错误（C-57）。"""
    runtime: AppRuntime = _runtime(request)
    if not body:
        return JSONResponse({"updated": False, "detail": "补丁为空"}, status_code=400)
    try:
        result = await runtime.apply_config_patch(body)
    except ConfigError as exc:
        payload: dict[str, Any] = {"updated": False, "detail": str(exc)}
        if exc.details:
            payload["errors"] = exc.details
        return JSONResponse(payload, status_code=400)
    return JSONResponse(result)


@router.post("/config/rollback", dependencies=[Depends(require_write)])
async def rollback_config(request: Request) -> JSONResponse:
    """回滚到上一版本配置（C-57 / C-94）。"""
    runtime: AppRuntime = _runtime(request)
    try:
        result = await runtime.rollback_config()
    except ConfigError as exc:
        payload: dict[str, Any] = {"rolled_back": False, "detail": str(exc)}
        if exc.details:
            payload["errors"] = exc.details
        return JSONResponse(payload, status_code=400)
    return JSONResponse(result)


@router.get("/auth/status")
async def auth_status(request: Request) -> dict[str, Any]:
    """鉴权开关与会话安全参数（供前端判断是否需要口令）。"""
    return _runtime(request).auth.status()


@router.get("/tools")
async def get_tools(request: Request) -> dict[str, Any]:
    """工具清单（含风险等级）、策略与调用统计（C-80 / T-5.4）。"""
    return _runtime(request).tools_snapshot()


@router.post("/tools/{name}/invoke", dependencies=[Depends(require_write)])
async def invoke_tool(
    request: Request, name: str, body: ToolInvokeRequest | None = None
) -> JSONResponse:
    """手动触发工具（调试用）：绕过 LLM，仍受安全策略与超时约束。"""
    runtime: AppRuntime = _runtime(request)
    if runtime.tools is None:
        return JSONResponse(
            {"invoked": False, "detail": "工具注册表未初始化"}, status_code=503
        )
    call = ToolCall(
        id=f"api-{name}",
        name=name,
        args=dict(body.args) if body is not None else {},
    )
    result = await runtime.tools.invoke(call)
    return JSONResponse({"tool": name, "invoked": True, **result.as_dict()})


@router.get("/metrics")
async def get_metrics(request: Request) -> dict[str, Any]:
    runtime: AppRuntime = _runtime(request)
    barge = runtime.barge_in_snapshot()
    return {
        "stages": runtime.metrics.snapshot(),
        "counters": runtime.metrics.counters(),
        "rag": runtime.rag.stats.as_dict() if runtime.rag is not None else {},
        "languages": runtime.language_stats(),
        "barge_in": {
            "counters": barge.get("metrics", {}),
            "last": barge.get("last"),
        },
        "errors": runtime.dialogue.error_count if runtime.dialogue is not None else 0,
    }


@router.get("/wake")
async def get_wake(request: Request) -> dict[str, Any]:
    """唤醒开关、活跃窗口倒计时与 KWS 状态（T-3A.8 / C-148）。"""
    return _runtime(request).wake_snapshot()


@router.post("/wake/false-positive", dependencies=[Depends(require_write)])
async def report_false_wake(request: Request) -> dict[str, Any]:
    """运维标记误唤醒：只累计计数，不上报任何音频片段（CON-6）。"""
    runtime: AppRuntime = _runtime(request)
    count = runtime.mark_false_wake("rest")
    return {"recorded": True, "wake_false_positive": count}


@router.get("/languages")
async def get_languages(request: Request) -> dict[str, Any]:
    """语言模式、生效语言与四项语言指标（T-2A.7 / C-158）。"""
    return _runtime(request).language_snapshot()


@router.post("/languages", dependencies=[Depends(require_write)])
async def update_languages(request: Request, body: LanguageUpdate) -> JSONResponse:
    """运维切换语言开关；非法语言返回 400。"""
    return await _apply_language(request, body)


@router.post("/languages/active", dependencies=[Depends(require_write)])
async def update_active_language(request: Request, body: LanguageUpdate) -> JSONResponse:
    """design §10.3 的等价入口：切换当前语言或语言模式。"""
    return await _apply_language(request, body)


async def _apply_language(request: Request, body: LanguageUpdate) -> JSONResponse:
    runtime: AppRuntime = _runtime(request)
    if runtime.dialogue is None:
        return JSONResponse(
            {"updated": False, "detail": "会话编排未初始化"}, status_code=503
        )
    if body.lang:
        supported = set(runtime.language_snapshot()["supported"])
        if body.lang not in supported:
            return JSONResponse(
                {
                    "updated": False,
                    "detail": f"语言 {body.lang!r} 不在 supported 中",
                    "supported": sorted(supported),
                },
                status_code=400,
            )
    snapshot = await runtime.set_language(body.mode, body.lang)
    return JSONResponse({"updated": True, **snapshot})


@router.get("/barge-in")
async def get_barge_in(request: Request) -> dict[str, Any]:
    """打断参数、保护窗口、计数器与最近一次阶段耗时（T-4.6 / C-10）。"""
    return _runtime(request).barge_in_snapshot()


@router.post("/barge-in", dependencies=[Depends(require_write)])
async def update_barge_in(request: Request, body: BargeInUpdate) -> JSONResponse:
    """热更新打断参数；非法值返回 400（T-4.6 / C-57）。"""
    runtime: AppRuntime = _runtime(request)
    if runtime.dialogue is None:
        return JSONResponse(
            {"updated": False, "detail": "会话编排未初始化"}, status_code=503
        )
    params = body.model_dump(exclude_unset=True)
    try:
        snapshot = await runtime.set_barge_in(params)
    except (ValidationError, ValueError) as exc:
        return JSONResponse(
            {"updated": False, "detail": f"非法打断参数：{exc}"}, status_code=400
        )
    return JSONResponse({"updated": True, **snapshot})


# ---------------------------------------------------------------- 意图（FR-17 / R14）


@router.get("/intents")
async def get_intents(request: Request) -> dict[str, Any]:
    """意图配置、驱动槽位与运行指标聚合（分布 / 低置信度 / 兜底 / 晚到 / 时延）。"""
    runtime: AppRuntime = _runtime(request)
    metrics = runtime.metrics
    counters = metrics.counters()
    distribution = {
        name[len("intent_distribution:"):]: count
        for name, count in counters.items()
        if name.startswith("intent_distribution:")
    }
    total = counters.get(INTENT_TOTAL, 0)
    low_confidence = counters.get(INTENT_LOW_CONFIDENCE, 0)
    slot = runtime.registry.slot("intent")
    dialogue = runtime.dialogue
    return {
        "config": runtime.settings.intent.model_dump(),
        "router": (
            dialogue.intent_router.snapshot()
            if dialogue is not None and dialogue.intent_router is not None
            else {"enabled": False}
        ),
        "slot": slot.snapshot() if slot is not None else None,
        "metrics": {
            "total": total,
            "distribution": distribution,
            "low_confidence": low_confidence,
            "low_confidence_ratio": round(low_confidence / total, 4) if total else 0.0,
            "fallback": counters.get(INTENT_FALLBACK, 0),
            "late_route": counters.get(INTENT_LATE_ROUTE, 0),
            "latency": {
                stage: metrics.summary(stage).as_dict()
                for stage in (INTENT_RULE, INTENT_CLASSIFY)
            },
        },
    }


@router.post("/intents/test", dependencies=[Depends(require_write)])
async def test_intent(request: Request, body: IntentTestRequest) -> JSONResponse:
    """意图试跑（FR-17.9）：先规则快速路径再语义分类，不写运行指标、不触发工具。"""
    runtime: AppRuntime = _runtime(request)
    dialogue = runtime.dialogue
    router = dialogue.intent_router if dialogue is not None else None
    if router is None or not router.enabled:
        return JSONResponse({"tested": False, "detail": "意图识别未启用"}, status_code=503)
    text = body.text.strip()
    if not text:
        return JSONResponse({"tested": False, "detail": "text 不能为空"}, status_code=400)
    result = router.fast_path(text, body.lang)
    if result is None:
        result = await router.classify(text, body.lang, record_metrics=False)
    return JSONResponse({"tested": True, "routed": router.route(result, False), **result.as_dict()})


# ---------------------------------------------------------------- 会话（design §10.3）


@router.get("/sessions")
async def list_sessions(request: Request, limit: int = 200) -> dict[str, Any]:
    runtime: AppRuntime = _runtime(request)
    if runtime.sessions is None:
        return {"count": 0, "sessions": []}
    sessions = runtime.sessions.list_sessions(limit=limit)
    return {"count": len(sessions), "sessions": sessions}


@router.post("/sessions", dependencies=[Depends(require_write)])
async def create_session(
    request: Request, body: SessionCreateRequest | None = None
) -> JSONResponse:
    runtime: AppRuntime = _runtime(request)
    if runtime.sessions is None:
        return JSONResponse({"created": False, "detail": "落库未初始化"}, status_code=503)
    session_id = (body.session_id if body is not None else None) or uuid4().hex[:16]
    if not _SESSION_ID.match(session_id):
        return JSONResponse(
            {"created": False, "detail": "session_id 仅允许字母 / 数字 / ._-，长度 1~64"},
            status_code=400,
        )
    runtime.sessions.ensure_session(session_id, {"source": "api"})
    return JSONResponse({"created": True, "session_id": session_id})


@router.get("/sessions/{session_id}")
async def get_session(request: Request, session_id: str) -> JSONResponse:
    runtime: AppRuntime = _runtime(request)
    if runtime.sessions is None:
        return JSONResponse({"detail": "落库未初始化"}, status_code=503)
    session = runtime.sessions.session(session_id)
    if session is None:
        return JSONResponse({"detail": "会话不存在"}, status_code=404)
    turns = runtime.sessions.turns(session_id)
    return JSONResponse({"session": session, "turn_count": len(turns)})


@router.delete("/sessions/{session_id}", dependencies=[Depends(require_write)])
async def delete_session(request: Request, session_id: str) -> JSONResponse:
    runtime: AppRuntime = _runtime(request)
    if runtime.sessions is None:
        return JSONResponse({"deleted": False, "detail": "落库未初始化"}, status_code=503)
    active = runtime.dialogue.session_id if runtime.dialogue is not None else ""
    if session_id == active and runtime.dialogue is not None:
        await runtime.dialogue.reset(session_id)
    counts = runtime.sessions.delete_session(session_id)
    return JSONResponse({"deleted": True, "session_id": session_id, "removed": counts})


@router.get("/sessions/{session_id}/turns")
async def get_turns(request: Request, session_id: str) -> dict[str, Any]:
    runtime: AppRuntime = _runtime(request)
    if runtime.sessions is None:
        return {"session_id": session_id, "count": 0, "turns": []}
    turns = runtime.sessions.turns(session_id)
    return {"session_id": session_id, "count": len(turns), "turns": turns}


@router.delete(
    "/sessions/{session_id}/turns/{seq}", dependencies=[Depends(require_write)]
)
async def delete_turn(request: Request, session_id: str, seq: int) -> JSONResponse:
    runtime: AppRuntime = _runtime(request)
    if runtime.sessions is None:
        return JSONResponse({"deleted": False, "detail": "落库未初始化"}, status_code=503)
    deleted = runtime.sessions.delete_turn(session_id, seq)
    return JSONResponse(
        {"deleted": deleted, "session_id": session_id, "seq": seq},
        status_code=200 if deleted else 404,
    )


@router.get("/sessions/{session_id}/messages")
async def get_messages(
    request: Request, session_id: str, limit: int = 100
) -> dict[str, Any]:
    runtime: AppRuntime = _runtime(request)
    if runtime.sessions is None:
        return {"session_id": session_id, "count": 0, "messages": []}
    messages = runtime.sessions.messages(session_id, limit=limit)
    return {"session_id": session_id, "count": len(messages), "messages": messages}


@router.delete(
    "/sessions/{session_id}/messages", dependencies=[Depends(require_write)]
)
async def clear_messages(
    request: Request, session_id: str, turn_id: int | None = None
) -> JSONResponse:
    """清除整会话或单轮记忆（T-6.9 / FR-8.4）。"""
    runtime: AppRuntime = _runtime(request)
    if runtime.memory is not None:
        removed = await runtime.memory.clear(session_id, turn_id)
    elif runtime.sessions is not None:
        removed = runtime.sessions.clear_messages(session_id, turn_id)
    else:
        return JSONResponse(
            {"cleared": False, "detail": "记忆未初始化"}, status_code=503
        )
    return JSONResponse(
        {"cleared": True, "session_id": session_id, "turn_id": turn_id, "removed": removed}
    )


# ---------------------------------------------------------------- 日志与诊断


@router.get("/logs/events")
async def get_events(
    request: Request,
    session_id: str | None = None,
    limit: int = 500,
    replay: bool = False,
) -> dict[str, Any]:
    """会话事件导出（C-104）：默认输出当前会话，按时间顺序可直接重放。"""
    runtime: AppRuntime = _runtime(request)
    subject = session_id or (
        runtime.dialogue.session_id if runtime.dialogue is not None else "default"
    )
    if runtime.sessions is None:
        return {"session_id": subject, "count": 0, "events": []}
    if replay:
        return runtime.sessions.export(subject, limit=limit)
    events = runtime.sessions.events(subject, limit=limit)
    return {"session_id": subject, "count": len(events), "events": events}


@router.get("/logs/download", dependencies=[Depends(require_write)])
async def download_logs(
    request: Request, session_id: str | None = None
) -> Response:
    """日志导出（C-123 / C-132）：按 JSONL 输出事件流，可直接回放与归档。"""
    runtime: AppRuntime = _runtime(request)
    subject = session_id or (
        runtime.dialogue.session_id if runtime.dialogue is not None else "default"
    )
    events = runtime.sessions.events(subject, limit=5000) if runtime.sessions else []
    lines = "\n".join(_serialize(event) for event in events)
    filename = f"robotchat-{subject}-{int(time.time())}.jsonl"
    return Response(
        content=lines + ("\n" if lines else ""),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/diagnostics/selfcheck", dependencies=[Depends(require_write)])
async def diagnostics_selfcheck(request: Request) -> dict[str, Any]:
    """一键自检（T-7.5 / C-123）：18 项 PASS / WARN / FAIL 与可操作建议。"""
    return await _runtime(request).selfcheck()


@router.post("/diagnostics/audio-loopback", dependencies=[Depends(require_write)])
async def diagnostics_loopback(
    request: Request, duration_s: float = 1.5
) -> JSONResponse:
    """音频回环测试（T-6.11 / C-99）。"""
    runtime: AppRuntime = _runtime(request)
    try:
        result = await runtime.run_audio_loopback(duration_s=duration_s)
    except RuntimeError as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=409)
    except Exception as exc:
        return JSONResponse({"ok": False, "detail": repr(exc)}, status_code=503)
    return JSONResponse(result)


@router.post("/session/{session_id}/reset", dependencies=[Depends(require_write)])
async def reset_session(request: Request, session_id: str) -> JSONResponse:
    """强制复位（C-30）：取消当前轮次、清空历史并回到待机 / 常开聆听。"""
    runtime: AppRuntime = _runtime(request)
    if runtime.dialogue is None:
        return JSONResponse(
            {"reset": False, "detail": "会话编排未初始化"}, status_code=503
        )
    result = await runtime.reset_session(session_id)
    return JSONResponse(result)
