"""多语言系统提示词单测（T-2A.4 / T-2A.5 / T-2A.8）。"""

from __future__ import annotations

from robotchat.dialog.prompt import MIXED_RULE, build_system_prompt, language_rule


def test_english_rule() -> None:
    rule = language_rule("en")
    assert "English" in rule


def test_dialect_rule_without_fallback() -> None:
    rule = language_rule("yue")
    assert "粤语" in rule
    assert "普通话" in rule


def test_dialect_rule_forces_mandarin_when_voice_fallback() -> None:
    rule = language_rule("dongbei", tts_fallback=True)
    assert "普通话书面语" in rule
    assert "禁止" in rule


def test_build_system_prompt_includes_mixed_rule_and_context() -> None:
    prompt = build_system_prompt("zh", rag_context="知识片段：充电桩位于 A 点")
    assert MIXED_RULE in prompt
    assert "知识片段：充电桩位于 A 点" in prompt


def test_build_system_prompt_marks_fallback() -> None:
    prompt = build_system_prompt("wuhan", tts_fallback=True)
    assert "普通话书面语" in prompt
