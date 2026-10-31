"""会话记忆与滚动摘要单测（T-5.1 / T-5.3、C-70 / C-126）。"""

from __future__ import annotations

from pathlib import Path

from robotchat.dialog.memory import SqliteMemoryStore, summarize_messages
from robotchat.dialog.session import SessionStore
from robotchat.providers.base import ChatMessage


def _store(tmp_path: Path) -> SessionStore:
    store = SessionStore(tmp_path / "memory.sqlite3")
    store.ensure_session("default", {})
    return store


def test_message_crud(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.append_message("default", "user", "你好", 1)
        store.append_message("default", "assistant", "你好呀", 1)
        rows = store.messages("default")
        assert [row["role"] for row in rows] == ["user", "assistant"]
        assert store.clear_messages("default", 1) == 2
        assert store.messages("default") == []
    finally:
        store.close()


def test_summary_upsert(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.set_summary("default", "早期摘要", 3)
        store.set_summary("default", "更新后的摘要", 5)
        row = store.summary("default")
        assert row is not None
        assert row["text"] == "更新后的摘要"
        assert row["upto_turn"] == 5
    finally:
        store.close()


def test_summarize_messages_is_extractive_and_bounded() -> None:
    messages = [
        ChatMessage(role="user", content="第一轮问题：" + "细节" * 80),
        ChatMessage(role="assistant", content="第一轮回答：" + "细节" * 80),
    ]
    assert summarize_messages(messages, max_chars=0) == ""
    summary = summarize_messages(messages, max_chars=150)
    assert summary.startswith("早期对话摘要")
    assert "第一轮问题" in summary
    assert len(summary) <= 200


async def test_async_write_and_restart_recovery(tmp_path: Path) -> None:
    """异步串行写入后重启驱动仍能恢复历史（C-70 / C-126）。"""
    store = _store(tmp_path)
    memory = SqliteMemoryStore(store)
    await memory.start()
    await memory.append_turn(
        "default", 1, user_text="今天天气怎么样", assistant_text="今天晴，适合出行"
    )
    await memory.flush()
    history = await memory.history("default", 10)
    assert [(item.role, item.content) for item in history] == [
        ("user", "今天天气怎么样"),
        ("assistant", "今天晴，适合出行"),
    ]
    await memory.close()

    # 模拟容器重启：同一 SQLite 文件重新装配驱动
    revived = SqliteMemoryStore(store)
    await revived.start()
    restored = await revived.history("default", 10)
    assert len(restored) == 2
    assert restored[0].content == "今天天气怎么样"
    await revived.close()
    store.close()


async def test_rolling_summary_keeps_early_context(tmp_path: Path) -> None:
    """超窗历史压缩为摘要，早期内容仍可被引用（C-73）。"""
    store = _store(tmp_path)
    memory = SqliteMemoryStore(store, summary_max_chars=400)
    await memory.start()
    dropped: list[ChatMessage] = []
    for index in range(5):
        dropped.append(ChatMessage(role="user", content=f"请记住编号 {index}"))
        dropped.append(ChatMessage(role="assistant", content=f"已记住编号 {index}"))
    summary = await memory.update_summary("default", dropped, upto_turn=10)
    assert "编号 0" in summary
    assert "编号 4" in summary
    assert await memory.summary("default") == summary

    merged = await memory.update_summary("default", dropped[-2:], upto_turn=12)
    assert "编号 4" in merged
    assert len(merged) <= 400
    assert await memory.summary("default") == merged

    await memory.clear("default")
    assert await memory.summary("default") == ""
    await memory.flush()
    await memory.close()
    store.close()


async def test_bounded_queue_reports_drops(tmp_path: Path) -> None:
    store = _store(tmp_path)
    memory = SqliteMemoryStore(store, max_queue=1)
    # 未启动 writer 且队列容量为 1：第二次入队应被计数丢弃，而非阻塞调用方
    await memory.append_turn("default", 1, user_text="一", assistant_text="二")
    await memory.append_turn("default", 2, user_text="三", assistant_text="四")
    await memory.flush()
    assert memory.dropped >= 1
    await memory.close()
    store.close()
