"""工具注册表与执行适配单测（T-5.4 / T-5.6 / T-5.11、C-80 / C-81 / C-86）。

覆盖：定义注册与黑白名单过滤、成功 / 参数非法 / 黑名单 / 未知工具 / 适配器缺失 /
超时 / 异常 / 取消、审计与快照、按配置装配内置工具集（含风险等级）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from robotchat.config.loader import load_settings
from robotchat.core.cancel import CancellationToken
from robotchat.health import HealthStatus
from robotchat.tools.base import ToolCall, ToolDefinition, ToolResult
from robotchat.tools.policy import ToolPolicy
from robotchat.tools.registry import ToolRegistry, build_tool_registry

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


class StubAdapter:
    """可注入延迟 / 失败 / 异常的桩适配器，用于验证注册表的超时与审计。"""

    name = "stub"

    def __init__(
        self,
        *,
        output: str = "执行成功",
        delay: float = 0.0,
        fail: str = "",
        raise_exc: bool = False,
    ) -> None:
        self.output = output
        self.delay = delay
        self.fail = fail
        self.raise_exc = raise_exc
        self.calls: list[ToolCall] = []

    async def execute(self, call: ToolCall, cancel: CancellationToken) -> ToolResult:
        del cancel
        self.calls.append(call)
        if self.raise_exc:
            raise RuntimeError("适配器异常")
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            return ToolResult(ok=False, error=self.fail, error_kind="adapter")
        return ToolResult(ok=True, output=self.output, duration_ms=1.0)

    async def health(self) -> HealthStatus:
        return HealthStatus.ok("桩适配器可用")


def make_definition(name: str = "echo", **overrides: Any) -> ToolDefinition:
    data: dict[str, Any] = {
        "name": name,
        "description": "回显文本",
        "risk": "low",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string", "minLength": 1}},
            "required": ["text"],
        },
        "timeout_s": 10.0,
        "adapter": "stub",
    }
    data.update(overrides)
    return ToolDefinition(**data)


def make_registry(
    adapter: StubAdapter | None = None, policy: ToolPolicy | None = None
) -> ToolRegistry:
    adapter = adapter or StubAdapter()
    return ToolRegistry(policy or ToolPolicy(), adapters={adapter.name: adapter})


def test_definitions_sorted_and_enabled_filtered_by_policy() -> None:
    registry = ToolRegistry(ToolPolicy(whitelist=["alpha"]))
    registry.register(make_definition("beta"))
    registry.register(make_definition("alpha"))

    assert [item.name for item in registry.definitions()] == ["alpha", "beta"]
    assert [item.name for item in registry.enabled_definitions()] == ["alpha"]
    assert registry.get("missing") is None

    blacklist = ToolRegistry(ToolPolicy(blacklist=["beta"]))
    blacklist.register(make_definition("alpha"))
    blacklist.register(make_definition("beta"))
    assert [item.name for item in blacklist.enabled_definitions()] == ["alpha"]


async def test_invoke_success_records_result_and_audit() -> None:
    registry = make_registry(StubAdapter(output="你好"))
    registry.register(make_definition("echo"))

    result = await registry.invoke(
        ToolCall(id="call-1", name="echo", args={"text": "hi"}), turn_id=7
    )

    assert result.ok is True
    assert result.output == "你好"
    assert result.error is None
    assert registry.calls == 1
    assert registry.failures == 0
    recent = registry.snapshot()["recent"][-1]
    assert recent["turn_id"] == 7
    assert recent["name"] == "echo"
    assert recent["ok"] is True


async def test_invoke_invalid_parameters_denied_before_adapter() -> None:
    adapter = StubAdapter()
    registry = make_registry(adapter)
    registry.register(make_definition("echo"))

    result = await registry.invoke(ToolCall(id="call-1", name="echo", args={}), turn_id=1)

    assert result.ok is False
    assert result.error_kind == "denied"
    assert "缺少必填参数 text" in (result.error or "")
    assert adapter.calls == []
    assert registry.denied == 1
    assert registry.calls == 0


async def test_invoke_blacklisted_tool_is_denied() -> None:
    adapter = StubAdapter()
    registry = make_registry(adapter, ToolPolicy(blacklist=["echo"]))
    registry.register(make_definition("echo"))

    result = await registry.invoke(
        ToolCall(id="call-1", name="echo", args={"text": "hi"}), turn_id=1
    )

    assert result.error_kind == "denied"
    assert "黑名单" in (result.error or "")
    assert adapter.calls == []
    assert registry.denied == 1


async def test_invoke_unknown_tool_records_denied() -> None:
    registry = make_registry()

    result = await registry.invoke(ToolCall(id="call-1", name="nope", args={}), turn_id=1)

    assert result.ok is False
    assert result.error_kind == "unknown"
    assert registry.denied == 1
    assert registry.failures == 1


async def test_invoke_missing_adapter_fails_cleanly() -> None:
    registry = make_registry()
    registry.register(make_definition("echo", adapter="missing"))

    result = await registry.invoke(
        ToolCall(id="call-1", name="echo", args={"text": "hi"}), turn_id=1
    )

    assert result.error_kind == "adapter"
    assert "未注册" in (result.error or "")
    assert registry.failures == 1


async def test_invoke_adapter_failure_and_exception() -> None:
    registry = make_registry(StubAdapter(fail="动作失败"))
    registry.register(make_definition("echo"))
    failed = await registry.invoke(
        ToolCall(id="call-1", name="echo", args={"text": "hi"}), turn_id=1
    )
    assert failed.error_kind == "adapter"
    assert failed.error == "动作失败"
    assert registry.failures == 1

    raising = make_registry(StubAdapter(raise_exc=True))
    raising.register(make_definition("echo"))
    raised = await raising.invoke(
        ToolCall(id="call-2", name="echo", args={"text": "hi"}), turn_id=2
    )
    assert raised.error_kind == "adapter"
    assert "适配器异常" in (raised.error or "")
    assert raising.failures == 1


async def test_invoke_timeout_records_timeout() -> None:
    registry = make_registry(StubAdapter(delay=0.05))
    registry.register(make_definition("echo", timeout_s=0.01))

    result = await registry.invoke(
        ToolCall(id="call-1", name="echo", args={"text": "hi"}), turn_id=1
    )

    assert result.ok is False
    assert result.error_kind == "timeout"
    assert registry.timeouts == 1
    assert registry.audit[-1].error_kind == "timeout"


async def test_invoke_cancellation_records_and_reraises() -> None:
    registry = make_registry(StubAdapter(delay=10.0))
    registry.register(make_definition("echo", timeout_s=30.0))

    task = asyncio.create_task(
        registry.invoke(ToolCall(id="call-1", name="echo", args={"text": "hi"}), turn_id=1)
    )
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert registry.audit[-1].error_kind == "cancelled"


async def test_health_and_snapshot() -> None:
    registry = make_registry(StubAdapter())
    registry.register(make_definition("echo"))

    status = await registry.health()
    assert status.state == "ok"

    snapshot = registry.snapshot()
    assert snapshot["adapters"] == ["stub"]
    assert snapshot["enabled"] == ["echo"]
    assert snapshot["policy"]["rate_limit_per_minute"] == 30
    assert snapshot["definitions"][0]["risk"] == "low"


def test_build_tool_registry_from_config_exposes_risk(tmp_path: Path) -> None:
    settings = load_settings(
        config_dir=REPO_CONFIG,
        data_dir=tmp_path,
        environ={"ROBOTCHAT__RESILIENCE__OFFLINE_PROBE_ON_START": "false"},
    )
    registry = build_tool_registry(settings.tools)
    definitions = {item.name: item for item in registry.definitions()}

    assert set(definitions) == {"navigate_to", "set_io", "speak"}
    assert definitions["speak"].adapter == "builtin"
    assert definitions["navigate_to"].risk == "high"
    assert definitions["navigate_to"].options.get("kind") == "action"
    assert definitions["set_io"].risk == "medium"

    snapshot = registry.snapshot()
    assert "builtin" in snapshot["adapters"]
    assert "ros2" in snapshot["adapters"]
    assert {item["name"] for item in snapshot["definitions"]} == set(definitions)
    assert all("risk" in item for item in snapshot["definitions"])
