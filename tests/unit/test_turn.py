"""T-3.2 Turn 管理单测：turn_id 单调递增、陈旧事件丢弃并计数（INV-1）。"""

from __future__ import annotations

from robotchat.core.turn import TurnManager


def test_turn_ids_are_monotonic() -> None:
    manager = TurnManager("s1")

    assert manager.new_turn() == 1
    assert manager.new_turn() == 2
    assert manager.current_turn_id == 2
    assert manager.created == 2


def test_new_turn_marks_previous_active_turn_interrupted() -> None:
    manager = TurnManager()
    manager.new_turn()
    first = manager.current
    manager.new_turn()

    assert first is not None
    assert first.status == "interrupted"


def test_accept_rejects_stale_turn() -> None:
    manager = TurnManager()
    manager.new_turn()
    manager.new_turn()

    assert manager.accept(2) is True
    assert manager.accept(None) is True
    assert manager.accept(1) is False
    assert manager.stale_dropped == 1


def test_end_turn_clears_current_and_rejects_late_events() -> None:
    manager = TurnManager()
    manager.new_turn()
    manager.end_turn("completed")

    assert manager.current_turn_id is None
    assert manager.accept(1) is False
    assert manager.stale_dropped == 1


def test_snapshot_exposes_counts() -> None:
    manager = TurnManager("s1")
    manager.new_turn()

    snapshot = manager.snapshot()
    assert snapshot["session_id"] == "s1"
    assert snapshot["current_turn_id"] == 1
    assert snapshot["stale_dropped"] == 0
