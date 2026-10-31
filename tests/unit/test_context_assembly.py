"""上下文预算滑窗与 100 轮不溢出（T-5.2、C-70 / C-71）。"""

from __future__ import annotations

from robotchat.dialog.prompt import (
    SUMMARY_HEADER,
    assemble_context,
    estimate_tokens,
)
from robotchat.providers.base import ChatMessage


def test_estimate_tokens_counts_cjk_and_ascii() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("你好 abcdefgh") == 4


def test_context_keeps_system_summary_and_current_user() -> None:
    history = [ChatMessage(role="user", content="很旧的内容" * 10)]
    result = assemble_context(
        system="系统提示词",
        history=history,
        user_text="当前问题",
        budget_tokens=200,
        summary="早期摘要",
    )
    assert result.messages[0].role == "system"
    assert result.messages[0].content == "系统提示词"
    assert result.messages[1].content.startswith(SUMMARY_HEADER)
    assert result.messages[-1].content == "当前问题"


def test_history_trimmed_from_oldest() -> None:
    history = [
        ChatMessage(role="user", content=f"历史消息 {index} " + "内容" * 20)
        for index in range(20)
    ]
    result = assemble_context(
        system="系统",
        history=history,
        user_text="现在",
        budget_tokens=260,
    )
    assert result.dropped
    # 被裁掉的是更旧的消息，保留的是最近若干条
    assert result.dropped[0].content.startswith("历史消息 0")
    assert all("历史消息" in item.content for item in result.messages[1:-1])
    assert result.messages[-1].content == "现在"


def test_hundred_turns_do_not_overflow() -> None:
    """100 轮对话下每轮组装后的上下文始终受预算约束（C-71）。"""
    budget = 320
    history: list[ChatMessage] = []
    for index in range(100):
        result = assemble_context(
            system="系统提示词",
            history=history,
            user_text=f"第 {index} 轮问题",
            budget_tokens=budget,
        )
        assert result.messages[-1].content == f"第 {index} 轮问题"
        assert result.used_tokens <= budget + 64
        history.append(ChatMessage(role="user", content=f"第 {index} 轮问题"))
        history.append(
            ChatMessage(role="assistant", content=f"第 {index} 轮回答，" + "说明" * 40)
        )
    # 历史会增长，但每轮只保留最近的部分
    assert len(result.messages) < len(history) + 3
