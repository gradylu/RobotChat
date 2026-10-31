"""多轮对话记忆（T-5.1 / T-5.3、design §8）。

- 存储复用 `SessionStore` 的 SQLite 连接，零外部依赖（ADR-09）；
- 写入走单消费者队列串行落库，避免并发写锁竞争；
- 提供 `MemoryStore` 契约的读写接口与滚动摘要压缩；
- 历史从库中恢复，容器重启后多轮上下文不丢（C-70 / C-126）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Literal

from robotchat.dialog.session import SessionStore
from robotchat.health import HealthStatus
from robotchat.observability.logging import get_logger
from robotchat.providers.base import ChatMessage

logger = get_logger("robotchat.dialog.memory")

MemoryRole = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """一条持久化消息：绑定会话 / 轮次，供 UI 查看与清除（C-74）。"""

    role: MemoryRole
    content: str
    turn_id: int = 0
    ts: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "turn_id": self.turn_id,
            "ts": self.ts,
        }


def summarize_messages(messages: list[ChatMessage], *, max_chars: int = 800) -> str:
    """把被滑窗裁掉的历史压缩为抽取式摘要（保留早期要点，T-5.3）。

    不依赖 LLM（避免摘要本身成为首响瓶颈）：按时间顺序摘取每轮用户输入与
    助手回复的前若干字符，超出 `max_chars` 即截断。
    """
    if max_chars <= 0:
        return ""
    pieces: list[str] = []
    used = 0
    # 被裁掉的消息数量可能很多，摘要只需保留早期要点
    for message in messages:
        text = " ".join(message.content.split())
        if not text:
            continue
        prefix = "用户" if message.role == "user" else "助手"
        piece = f"{prefix}: {text[:120]}"
        if used + len(piece) + 1 > max_chars:
            break
        pieces.append(piece)
        used += len(piece) + 1
    if not pieces:
        return ""
    return "早期对话摘要（已压缩）：\n" + "\n".join(pieces)


class SqliteMemoryStore:
    """SQLite 记忆驱动：异步串行写入 + 同步读接口。"""

    name = "sqlite"

    def __init__(
        self,
        store: SessionStore,
        *,
        max_queue: int = 1024,
        summary_max_chars: int = 800,
    ) -> None:
        if max_queue < 1:
            raise ValueError(f"max_queue 必须 >= 1：{max_queue}")
        self._store = store
        self.summary_max_chars = summary_max_chars
        self._queue: asyncio.Queue[tuple[str, str, str, int] | None] = asyncio.Queue(
            maxsize=max_queue
        )
        self._writer: asyncio.Task[None] | None = None
        self._closed = False
        self.written = 0
        self.dropped = 0
        self.write_errors = 0

    # ------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self._writer is not None:
            return
        self._closed = False
        self._writer = asyncio.create_task(self._run_writer(), name="memory-writer")

    async def _run_writer(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                session_id, role, content, turn_id = item
                # 阻塞 IO 交给线程池，避免 SQLite 写入卡住事件循环
                await asyncio.to_thread(
                    self._store.append_message, session_id, role, content, turn_id
                )
                self.written += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                self.write_errors += 1
                logger.exception("memory_write_failed")
            finally:
                self._queue.task_done()

    async def close(self) -> None:
        """停止接收并等待队列内消息全部落库。"""
        if self._closed:
            return
        self._closed = True
        writer = self._writer
        if writer is None:
            return
        await self._queue.put(None)
        await asyncio.gather(writer, return_exceptions=True)
        self._writer = None

    # ------------------------------------------------------------ 写

    def _enqueue(self, session_id: str, role: str, content: str, turn_id: int) -> bool:
        if not content.strip() or self._closed:
            return False
        if self._writer is None:
            # 允许未显式 start 时惰性启动，保证消息不丢
            self._writer = asyncio.create_task(self._run_writer(), name="memory-writer")
        try:
            self._queue.put_nowait((session_id, role, content, turn_id))
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                logger.warning("memory_queue_full", dropped=self.dropped)
            return False
        return True

    async def append_message(
        self, session_id: str, message: ChatMessage, turn_id: int = 0
    ) -> None:
        self._enqueue(session_id, message.role, message.content, turn_id)

    async def append_turn(
        self,
        session_id: str,
        turn_id: int,
        *,
        user_text: str,
        assistant_text: str,
        status: str = "completed",
    ) -> None:
        """一轮结束后写入用户与助手消息；被打断时只保留已播出的部分。"""
        if user_text.strip():
            self._enqueue(session_id, "user", user_text, turn_id)
        if assistant_text.strip():
            self._enqueue(session_id, "assistant", assistant_text, turn_id)
        logger.info(
            "memory_turn_appended",
            session_id=session_id,
            turn_id=turn_id,
            status=status,
            chars=len(user_text) + len(assistant_text),
        )

    async def flush(self) -> None:
        """等待已入队消息写完（单测与优雅关停用）。"""
        await self._queue.join()

    # ------------------------------------------------------------ 读

    async def history(self, session_id: str, limit: int = 100) -> list[ChatMessage]:
        rows = await asyncio.to_thread(self._store.messages, session_id, limit)
        return [
            ChatMessage(role=row["role"], content=row["content"])
            for row in rows
            if row["role"] in ("system", "user", "assistant", "tool")
        ]

    def entries(self, session_id: str, limit: int = 100) -> list[MemoryEntry]:
        return [
            MemoryEntry(
                role=row["role"],
                content=row["content"],
                turn_id=int(row["turn_id"]),
                ts=float(row["ts"]),
            )
            for row in self._store.messages(session_id, limit)
        ]

    async def clear(self, session_id: str, turn_id: int | None = None) -> int:
        removed = await asyncio.to_thread(self._store.clear_messages, session_id, turn_id)
        if turn_id is None:
            await asyncio.to_thread(self._store.set_summary, session_id, "", 0)
        logger.info("memory_cleared", session_id=session_id, turn_id=turn_id, removed=removed)
        return removed

    # ------------------------------------------------------------ 摘要

    async def summary(self, session_id: str) -> str:
        row = await asyncio.to_thread(self._store.summary, session_id)
        return "" if row is None else str(row.get("text", ""))

    async def update_summary(
        self, session_id: str, dropped: list[ChatMessage], upto_turn: int = 0
    ) -> str:
        """把超窗历史合并进既有摘要；摘要长度超过上限时保留最新的尾部。"""
        if not dropped:
            return await self.summary(session_id)
        fresh = summarize_messages(dropped, max_chars=self.summary_max_chars)
        if not fresh:
            return await self.summary(session_id)
        previous = await self.summary(session_id)
        merged = fresh if not previous else f"{previous}\n{fresh}"
        if len(merged) > self.summary_max_chars:
            merged = merged[-self.summary_max_chars :]
        await asyncio.to_thread(self._store.set_summary, session_id, merged, upto_turn)
        logger.info("memory_summary_updated", session_id=session_id, chars=len(merged))
        return merged

    # ------------------------------------------------------------ 观测

    async def health(self) -> HealthStatus:
        try:
            await asyncio.to_thread(self._store.messages, "health-check", 1)
        except Exception as exc:
            return HealthStatus.unavailable(repr(exc))
        if self.write_errors:
            return HealthStatus.degraded(f"写入失败 {self.write_errors} 次")
        return HealthStatus.ok(f"已写入 {self.written} 条")

    def snapshot(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "written": self.written,
            "dropped": self.dropped,
            "write_errors": self.write_errors,
            "pending": self._queue.qsize(),
            "summary_max_chars": self.summary_max_chars,
            "updated_at": round(time.time(), 3),
        }


__all__ = ["MemoryEntry", "SqliteMemoryStore", "summarize_messages"]
