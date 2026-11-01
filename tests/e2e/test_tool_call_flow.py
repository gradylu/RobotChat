"""工具调用与高风险二次确认（T-8.2 / C-84 / C-85）：执行态、结果回灌与确认/取消。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

from conftest import (
    FakeTTS,
    StubAdapter,
    ToolCallingLLM,
    make_orchestrator,
    make_settings,
    wait_until,
)

from robotchat.core.state_machine import State
from robotchat.dialog.session import SessionStore
from robotchat.providers.base import ASREvent
from robotchat.tools.base import ToolDefinition
from robotchat.tools.policy import ToolPolicy
from robotchat.tools.registry import ToolRegistry, build_tool_registry


def _payloads(store: SessionStore, event_type: str) -> list[dict[str, Any]]:
    return [item["payload"] for item in store.events("default") if item["type"] == event_type]


def _executing_states(store: SessionStore) -> list[dict[str, Any]]:
    return [
        payload
        for payload in _payloads(store, "state.changed")
        if payload.get("to") == State.EXECUTING.value
    ]


async def test_tool_call_executes_and_feeds_result_back(tmp_path: Path) -> None:
    """T-8.2 / C-84：低风险工具经 EXECUTING 状态执行，结果回灌后播报成功。"""
    settings = make_settings(tmp_path)
    adapter = StubAdapter()
    tools = ToolRegistry(ToolPolicy(), adapters={"stub": adapter})
    tools.register(
        ToolDefinition(
            name="diagnose",
            description="执行系统自检",
            risk="low",
            parameters={"type": "object", "properties": {}, "required": []},
            timeout_s=1.0,
            adapter="stub",
        )
    )
    llm = ToolCallingLLM(
        {"id": "call-1", "name": "diagnose", "arguments": {}},
        final=["状态", "正常。"],
    )
    tts = FakeTTS()
    orchestrator, store = make_orchestrator(
        settings, tmp_path, providers={"llm": llm, "tts": tts}, tools=tools
    )
    await orchestrator.start()

    result = await orchestrator.handle_text("自检一下")
    assert result is not None
    assert result["assistant_text"] == "状态正常。"

    assert len(llm.requests) == 2
    fed_back = llm.requests[1].messages[-1].content
    assert fed_back.startswith("[工具结果] diagnose")
    assert "执行成功" in fed_back
    assert adapter.calls[0].name == "diagnose"
    assert adapter.calls[0].args == {}

    assert _executing_states(store)
    results = _payloads(store, "tool.result")
    assert results[-1]["ok"] is True
    assert orchestrator.tools_snapshot()["calls"] == 1
    assert [request.text for request in tts.requests] == ["状态正常。"]
    turn = store.turn("default", 1)
    assert turn["status"] == "completed"

    await orchestrator.shutdown()


async def test_high_risk_tool_runs_after_voice_confirmation(tmp_path: Path) -> None:
    """T-8.2 / C-84：高风险动作经语音「确认」后执行，参数与结果回灌正确。"""
    settings = make_settings(tmp_path)
    adapter = StubAdapter(name="ros2", output="已到达充电桩")
    tools = build_tool_registry(settings.tools, adapters={"ros2": adapter})
    definition = tools.get("navigate_to")
    assert definition is not None
    # 缩短执行时长，避免超过异步阈值转入后台任务
    tools.register(replace(definition, timeout_s=1.0))
    llm = ToolCallingLLM(
        {"id": "nav-1", "name": "navigate_to", "arguments": {"location": "充电桩"}},
        final=["已到达充电桩。"],
    )
    tts = FakeTTS()
    orchestrator, store = make_orchestrator(
        settings, tmp_path, providers={"llm": llm, "tts": tts}, tools=tools
    )
    await orchestrator.start()

    task = asyncio.create_task(orchestrator.handle_text("导航到充电桩"))
    await wait_until(lambda: orchestrator._confirming)
    await orchestrator._on_asr_during_confirm(
        ASREvent(type="final", text="确认执行", lang="zh")
    )
    await asyncio.wait_for(task, timeout=5.0)

    assert adapter.calls[0].args == {"location": "充电桩"}
    resolved = _payloads(store, "tool.confirm.resolved")
    assert resolved[-1]["confirmed"] is True
    assert resolved[-1]["reason"] == "user"
    results = _payloads(store, "tool.result")
    assert results[-1]["ok"] is True
    assert _executing_states(store)
    fed_back = llm.requests[1].messages[-1].content
    assert fed_back.startswith("[工具结果] navigate_to")
    assert "已到达充电桩" in fed_back
    assert orchestrator.tools_snapshot()["calls"] == 1
    assert any(request.text == "已到达充电桩。" for request in tts.requests)
    assert store.turn("default", 1)["assistant_text"] == "已到达充电桩。"

    await orchestrator.shutdown()


async def test_high_risk_tool_cancelled_without_execution(tmp_path: Path) -> None:
    """T-8.2 / C-84 / C-85：说「取消」不执行，结果以 cancelled 回灌且不进入执行态。"""
    settings = make_settings(tmp_path)
    adapter = StubAdapter(name="ros2", output="已到达充电桩")
    tools = build_tool_registry(settings.tools, adapters={"ros2": adapter})
    definition = tools.get("navigate_to")
    assert definition is not None
    tools.register(replace(definition, timeout_s=1.0))
    llm = ToolCallingLLM(
        {"id": "nav-1", "name": "navigate_to", "arguments": {"location": "充电桩"}},
        final=["已取消导航。"],
    )
    tts = FakeTTS()
    orchestrator, store = make_orchestrator(
        settings, tmp_path, providers={"llm": llm, "tts": tts}, tools=tools
    )
    await orchestrator.start()

    task = asyncio.create_task(orchestrator.handle_text("导航到充电桩"))
    await wait_until(lambda: orchestrator._confirming)
    await orchestrator._on_asr_during_confirm(
        ASREvent(type="final", text="取消", lang="zh")
    )
    await asyncio.wait_for(task, timeout=5.0)

    assert adapter.calls == []
    resolved = _payloads(store, "tool.confirm.resolved")
    assert resolved[-1]["confirmed"] is False
    assert resolved[-1]["reason"] == "user"
    results = _payloads(store, "tool.result")
    assert results[-1]["ok"] is False
    assert results[-1]["error_kind"] == "cancelled"
    assert not _executing_states(store)
    fed_back = llm.requests[1].messages[-1].content
    assert fed_back.startswith("[工具结果] navigate_to")
    assert "未确认" in fed_back
    assert orchestrator.tools_snapshot()["calls"] == 0
    assert store.turn("default", 1)["assistant_text"] == "已取消导航。"

    await orchestrator.shutdown()
