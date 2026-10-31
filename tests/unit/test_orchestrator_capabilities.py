"""编排层能力协商单测：无 tool call 时禁用任务指令并告警（T-2.9）。"""

from __future__ import annotations

from typing import Any

from robotchat.core.orchestrator import build_provider_report, negotiate_llm_capabilities
from robotchat.providers.base import LLMCapabilities
from robotchat.providers.registry import ProviderRegistry, register_provider


@register_provider("llm", "caps_no_tools")
class _NoToolsLLM:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        del config
        self.name = "caps_no_tools"
        self.capabilities = LLMCapabilities(streaming=True, tool_call=False, context_window=4096)


@register_provider("llm", "caps_with_tools")
class _ToolsLLM:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        del config
        self.name = "caps_with_tools"
        self.capabilities = LLMCapabilities(streaming=True, tool_call=True)


@register_provider("llm", "caps_non_stream")
class _NonStreamLLM:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        del config
        self.name = "caps_non_stream"
        self.capabilities = LLMCapabilities(streaming=False, tool_call=False)


def test_tools_disabled_when_driver_lacks_tool_call() -> None:
    driver = _NoToolsLLM()
    effective = negotiate_llm_capabilities(driver, tools_requested=True)

    assert effective.tools_enabled is False
    assert effective.streaming is True
    assert any("tool call" in note for note in effective.notes)


def test_tools_enabled_when_capability_present() -> None:
    effective = negotiate_llm_capabilities(_ToolsLLM(), tools_requested=True)
    assert effective.tools_enabled is True
    assert effective.notes == ()


def test_non_stream_driver_is_reported() -> None:
    effective = negotiate_llm_capabilities(_NonStreamLLM(), tools_requested=False)
    assert effective.streaming is False
    assert effective.tools_enabled is False
    assert any("流式" in note for note in effective.notes)


def test_missing_provider_reports_unavailable() -> None:
    effective = negotiate_llm_capabilities(None)
    assert effective.streaming is False
    assert effective.tools_enabled is False


def test_provider_report_exposes_capabilities_and_features() -> None:
    registry = ProviderRegistry()
    registry.register_slot("llm", "caps_no_tools", {}, "caps_with_tools", {})

    report = build_provider_report(registry, tools_requested=True)
    item = report["llm"]

    assert item["kind"] == "llm"
    assert item["capabilities"]["tool_call"] is False
    assert item["capabilities"]["context_window"] == 4096
    # 主驱动不支持 tool call → 能力协商后任务指令关闭并给出说明
    assert item["features"]["tools_enabled"] is False
    assert item["features"]["notes"]
    assert item["primary"]["name"] == "caps_no_tools"
