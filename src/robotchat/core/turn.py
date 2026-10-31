"""Turn 管理（T-3.2、INV-1）：单调递增 turn_id 与陈旧事件判定。

任意时刻只有一个有效轮次；非当前 `turn_id` 的事件静默丢弃并计数，
避免旧轮次的迟到事件污染当前对话。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from robotchat.observability.logging import get_logger

logger = get_logger("robotchat.core.turn")


@dataclass
class Turn:
    turn_id: int
    seq: int
    started_at: float = field(default_factory=time.monotonic)
    status: str = "active"


class TurnManager:
    """签发与校验当前有效轮次。"""

    def __init__(self, session_id: str = "") -> None:
        self.session_id = session_id
        self._current: Turn | None = None
        self._last_id = 0
        self.created = 0
        self.stale_dropped = 0

    @property
    def current_turn_id(self) -> int | None:
        return None if self._current is None else self._current.turn_id

    @property
    def current(self) -> Turn | None:
        return self._current

    def new_turn(self) -> int:
        """签发新轮次；旧轮次若未结束则标记 interrupted。"""
        if self._current is not None and self._current.status == "active":
            self._current.status = "interrupted"
        self._last_id += 1
        self.created += 1
        self._current = Turn(turn_id=self._last_id, seq=self.created)
        return self._last_id

    def end_turn(self, status: str = "completed") -> None:
        if self._current is not None and self._current.status == "active":
            self._current.status = status
        self._current = None

    def accept(self, turn_id: int | None) -> bool:
        """判断事件是否属于当前轮次；`None` 表示全局事件，始终接受。"""
        if turn_id is None:
            return True
        current = self.current_turn_id
        if current is not None and turn_id == current:
            return True
        self.stale_dropped += 1
        logger.warning(
            "stale_turn_event_dropped",
            turn_id=turn_id,
            current_turn_id=current,
            dropped_total=self.stale_dropped,
        )
        return False

    def snapshot(self) -> dict[str, int | str | None]:
        return {
            "session_id": self.session_id,
            "current_turn_id": self.current_turn_id,
            "created": self.created,
            "stale_dropped": self.stale_dropped,
        }


__all__ = ["Turn", "TurnManager"]
