"""会话与轮次落库（T-3.8、C-104、design §11）。

- `Session` / `Turn` / `Event` 三张表，SQLite 零外部依赖（ADR-09）；
- 轮次记录 timings、RAG 命中、打断标记与状态；
- `/api/logs/events` 基于事件表导出，可按时间顺序重放。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from robotchat.observability.logging import get_logger

logger = get_logger("robotchat.dialog.session")


class SessionStore:
    """SQLite 落库：写入用锁串行化，避免并发写导致的锁竞争。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    ended_at REAL,
                    state TEXT NOT NULL DEFAULT 'active',
                    config_snapshot TEXT NOT NULL DEFAULT '{}',
                    turn_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    lang TEXT NOT NULL DEFAULT '',
                    tts_lang TEXT NOT NULL DEFAULT '',
                    tts_fallback INTEGER NOT NULL DEFAULT 0,
                    user_text TEXT NOT NULL DEFAULT '',
                    asr_partials TEXT NOT NULL DEFAULT '[]',
                    assistant_text TEXT NOT NULL DEFAULT '',
                    rag_hits TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'active',
                    timings TEXT NOT NULL DEFAULT '{}',
                    barge_in INTEGER NOT NULL DEFAULT 0,
                    spoken_ratio REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    ended_at REAL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_id INTEGER,
                    type TEXT NOT NULL,
                    ts REAL NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    turn_id INTEGER NOT NULL DEFAULT 0,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    ts REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS summaries (
                    session_id TEXT PRIMARY KEY,
                    text TEXT NOT NULL DEFAULT '',
                    upto_turn INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id, seq);
                CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, id);
                CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
                """
            )
            self._migrate_turns()

    def _migrate_turns(self) -> None:
        """旧库兼容：补齐 Phase 2A 新增的语言字段（T-2A.8）。"""
        existing = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(turns)").fetchall()
        }
        additions = {
            "tts_lang": "TEXT NOT NULL DEFAULT ''",
            "tts_fallback": "INTEGER NOT NULL DEFAULT 0",
            # 打断轮次的已播报比例（FR-6.8 / C-72）
            "spoken_ratio": "REAL NOT NULL DEFAULT 0",
            # 意图识别落库（FR-17 / R14）
            "intent": "TEXT NOT NULL DEFAULT ''",
            "intent_confidence": "REAL NOT NULL DEFAULT 0",
            "intent_source": "TEXT NOT NULL DEFAULT ''",
            "intent_slots": "TEXT NOT NULL DEFAULT '[]'",
            "intent_latency_ms": "REAL NOT NULL DEFAULT 0",
        }
        for name, ddl in additions.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE turns ADD COLUMN {name} {ddl}")
                logger.info("session_schema_migrated", column=name)

    # ------------------------------------------------------------ 会话

    def ensure_session(
        self, session_id: str, config_snapshot: dict[str, Any] | None = None
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions (id, created_at, state, config_snapshot) "
                "VALUES (?, ?, 'active', ?)",
                (session_id, time.time(), _dumps(config_snapshot or {})),
            )

    def end_session(self, session_id: str, state: str = "ended") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE sessions SET ended_at = ?, state = ? WHERE id = ?",
                (time.time(), state, session_id),
            )

    def session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return None if row is None else _row_to_dict(row, ("config_snapshot",))

    def list_sessions(self, limit: int = 200) -> list[dict[str, Any]]:
        """会话列表（T-6.2 / design §10.3）：按创建时间倒序。"""
        limit = max(1, int(limit))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_dict(row, ("config_snapshot",)) for row in rows]

    def delete_session(self, session_id: str) -> dict[str, int]:
        """删除会话及其轮次 / 事件 / 消息 / 摘要（MemoryView 清理入口）。"""
        counts: dict[str, int] = {}
        with self._lock, self._conn:
            for table in ("events", "messages", "turns", "summaries", "sessions"):
                column = "id" if table == "sessions" else "session_id"
                cursor = self._conn.execute(
                    f"DELETE FROM {table} WHERE {column} = ?", (session_id,)
                )
                counts[table] = cursor.rowcount
        return counts

    def turn(self, session_id: str, seq: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM turns WHERE session_id = ? AND seq = ?", (session_id, seq)
            ).fetchone()
        return None if row is None else _row_to_dict(
            row, ("asr_partials", "rag_hits", "timings", "intent_slots")
        )

    def delete_turn(self, session_id: str, seq: int) -> bool:
        """删除单轮：轮次记录与对应消息（FR-8.4 记忆管理）。"""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM turns WHERE session_id = ? AND seq = ?", (session_id, seq)
            )
            self._conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND turn_id = ?", (session_id, seq)
            )
            self._conn.execute(
                "UPDATE sessions SET turn_count = MAX(turn_count - 1, 0) WHERE id = ?",
                (session_id,),
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------ 轮次

    def start_turn(
        self, session_id: str, seq: int, lang: str = "", user_text: str = ""
    ) -> int:
        self.ensure_session(session_id)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO turns "
                "(session_id, seq, lang, user_text, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, seq, lang, user_text, time.time()),
            )
            self._conn.execute(
                "UPDATE sessions SET turn_count = turn_count + 1 WHERE id = ?",
                (session_id,),
            )
            last_id = cursor.lastrowid
        if last_id is None:
            raise RuntimeError("轮次写入失败：数据库未返回 rowid")
        return last_id

    def complete_turn(
        self,
        turn_id: int,
        *,
        assistant_text: str = "",
        status: str = "completed",
        timings: dict[str, float] | None = None,
        barge_in: bool = False,
        rag_hits: list[Any] | None = None,
        asr_partials: list[str] | None = None,
        user_text: str | None = None,
        lang: str | None = None,
        tts_lang: str | None = None,
        tts_fallback: bool | None = None,
        spoken_ratio: float | None = None,
        intent: str | None = None,
        intent_confidence: float | None = None,
        intent_source: str | None = None,
        intent_slots: dict[str, Any] | None = None,
        intent_latency_ms: float | None = None,
    ) -> None:
        fields: dict[str, Any] = {
            "assistant_text": assistant_text,
            "status": status,
            "timings": _dumps(timings or {}),
            "barge_in": int(barge_in),
            "rag_hits": _dumps(rag_hits or []),
            "asr_partials": _dumps(asr_partials or []),
            "ended_at": time.time(),
        }
        if spoken_ratio is not None:
            fields["spoken_ratio"] = float(spoken_ratio)
        if user_text is not None:
            fields["user_text"] = user_text
        if lang is not None:
            fields["lang"] = lang
        if tts_lang is not None:
            fields["tts_lang"] = tts_lang
        if tts_fallback is not None:
            fields["tts_fallback"] = int(tts_fallback)
        if intent is not None:
            fields["intent"] = intent
        if intent_confidence is not None:
            fields["intent_confidence"] = float(intent_confidence)
        if intent_source is not None:
            fields["intent_source"] = intent_source
        if intent_slots is not None:
            fields["intent_slots"] = _dumps(intent_slots)
        if intent_latency_ms is not None:
            fields["intent_latency_ms"] = float(intent_latency_ms)
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE turns SET {assignments} WHERE id = ?",  # noqa: S608 - 字段名来自白名单
                (*fields.values(), turn_id),
            )

    def turns(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM turns WHERE session_id = ? ORDER BY seq ASC", (session_id,)
            ).fetchall()
        return [
            _row_to_dict(row, ("asr_partials", "rag_hits", "timings", "intent_slots"))
            for row in rows
        ]

    # ------------------------------------------------------------ 事件

    def add_event(
        self,
        session_id: str,
        event_type: str,
        turn_id: int | None = None,
        payload: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events (session_id, turn_id, type, ts, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, turn_id, event_type, ts or time.time(), _dumps(payload or {})),
            )

    def events(self, session_id: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        limit = max(1, int(limit))
        with self._lock:
            if session_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
        return [_row_to_dict(row, ("payload",)) for row in reversed(rows)]

    # ------------------------------------------------------------ 消息与摘要（T-5.1 / T-5.3）

    def append_message(
        self, session_id: str, role: str, content: str, turn_id: int = 0
    ) -> None:
        """追加一条对话消息（用户 / 助手 / 工具 / 摘要系统消息）。"""
        if not content.strip():
            return
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO messages (session_id, turn_id, role, content, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, turn_id, role, content, time.time()),
            )

    def messages(self, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """按时间正序返回最近 `limit` 条消息（超出部分从最旧开始丢弃）。"""
        limit = max(1, int(limit))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def clear_messages(self, session_id: str, turn_id: int | None = None) -> int:
        """清除整会话消息；指定 `turn_id` 时只清除该轮（FR-8.4）。"""
        with self._lock, self._conn:
            if turn_id is None:
                cursor = self._conn.execute(
                    "DELETE FROM messages WHERE session_id = ?", (session_id,)
                )
            else:
                cursor = self._conn.execute(
                    "DELETE FROM messages WHERE session_id = ? AND turn_id = ?",
                    (session_id, turn_id),
                )
            return cursor.rowcount

    def set_summary(self, session_id: str, text: str, upto_turn: int = 0) -> None:
        """写入滚动摘要（超窗历史压缩后的单条系统消息）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO summaries (session_id, text, upto_turn, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "text = excluded.text, upto_turn = excluded.upto_turn, "
                "updated_at = excluded.updated_at",
                (session_id, text, upto_turn, time.time()),
            )

    def summary(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM summaries WHERE session_id = ?", (session_id,)
            ).fetchone()
        return None if row is None else dict(row)

    # ------------------------------------------------------------ 导出 / 回放

    def export(self, session_id: str, *, limit: int = 2000) -> dict[str, Any]:
        return {
            "session": self.session(session_id),
            "turns": self.turns(session_id),
            "events": self.events(session_id, limit=limit),
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def _row_to_dict(row: sqlite3.Row, json_fields: tuple[str, ...]) -> dict[str, Any]:
    data: dict[str, Any] = dict(row)
    for name in json_fields:
        data[name] = _loads(data[name])
    return data


__all__ = ["SessionStore"]
