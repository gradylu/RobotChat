"""T-3.8 会话落库单测：轮次 timings、事件顺序与导出重放。"""

from __future__ import annotations

from pathlib import Path

from robotchat.dialog.session import SessionStore


def test_store_round_trip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.sqlite3")
    store.ensure_session("s1", {"env": "test"})
    db_id = store.start_turn("s1", 1, lang="zh", user_text="你好")

    store.add_event("s1", "asr.partial", 1, {"text": "你"})
    store.add_event("s1", "state.changed", 1, {"to": "THINKING"})
    store.complete_turn(
        db_id,
        assistant_text="你好呀",
        status="completed",
        timings={"llm_first": 120.0, "tts_first": 200.0},
        rag_hits=["doc-1"],
        asr_partials=["你", "你好"],
        user_text="你好",
        lang="zh",
    )
    store.end_session("s1")

    session = store.session("s1")
    assert session is not None
    assert session["state"] == "ended"
    assert session["config_snapshot"] == {"env": "test"}
    assert session["turn_count"] == 1

    turns = store.turns("s1")
    assert len(turns) == 1
    assert turns[0]["assistant_text"] == "你好呀"
    assert turns[0]["timings"]["llm_first"] == 120.0
    assert turns[0]["rag_hits"] == ["doc-1"]
    assert turns[0]["asr_partials"] == ["你", "你好"]

    events = store.events("s1")
    assert [item["type"] for item in events] == ["asr.partial", "state.changed"]
    store.close()


def test_export_is_replayable(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.sqlite3")
    db_id = store.start_turn("s1", 1, user_text="测试")
    store.complete_turn(db_id, assistant_text="收到", status="completed")
    store.add_event("s1", "playback.state", 1, {"playing": False})

    export = store.export("s1")
    assert export["session"]["id"] == "s1"
    assert export["turns"][0]["assistant_text"] == "收到"
    assert export["events"][0]["payload"] == {"playing": False}
    store.close()


def test_events_are_limited_and_ordered(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "session.sqlite3")
    store.ensure_session("s1")
    for index in range(5):
        store.add_event("s1", f"e{index}", None, {})

    events = store.events("s1", limit=3)
    assert [item["type"] for item in events] == ["e2", "e3", "e4"]
    store.close()
