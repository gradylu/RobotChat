"""全双工插话打断（T-8.2 / C-102 / C-08）：真实能量插话触发打断与连续 20 次不错乱。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from conftest import (
    EchoLLM,
    FakeASRProvider,
    FakeLLM,
    FakeTransport,
    FakeTTS,
    make_orchestrator,
    make_settings,
    wait_turn,
    wait_until,
)

from robotchat.core.state_machine import State
from robotchat.observability.metrics import BARGE_IN, BARGE_IN_ACCEPTED
from robotchat.providers.base import ASREvent


async def test_energy_interjection_interrupts_playback(tmp_path: Path) -> None:
    """T-8.2 / C-102：播报期真实插话 → 停声、归档已播片段、turn_id 递增并回到聆听。"""
    settings = make_settings(
        tmp_path,
        {
            "ROBOTCHAT__BARGE_IN__GUARD_MS": "0",
            "ROBOTCHAT__BARGE_IN__CONFIRM_FRAMES": "2",
        },
    )
    transport = FakeTransport(frames=360, frame_delay=0.005, loud_after=60)
    asr = FakeASRProvider(
        ["你好吗？"], partial="等一下", repeat_partials=True, partial_interval=0.02
    )
    llm = FakeLLM(["第一句。", "第二句。", "第三句很长没有结束"], gate=True)
    tts = FakeTTS(chunks=2, interval_s=0.02)
    orchestrator, store = make_orchestrator(
        settings,
        tmp_path,
        transport=transport,
        providers={"asr": asr, "llm": llm, "tts": tts},
    )
    await orchestrator.start()

    handle_task = asyncio.create_task(orchestrator.handle_text("你好"))
    # 不手工注入打断事件：完全依赖采集帧能量 + ASR partial 语义确认
    await wait_until(lambda: orchestrator._last_barge is not None)
    last = orchestrator._last_barge
    assert last is not None
    assert last["result"] == "accepted"
    assert last["trigger"] == "semantic"
    assert last["turn_id"] == 1
    assert last["next_turn_id"] == 2
    assert set(last["stage_latency"]) == {
        "state",
        "flush",
        "cancel",
        "archive",
        "turn",
        "resume",
    }
    assert last["total_ms"] > 0

    assert orchestrator.machine.state in (State.LISTENING, State.ACTIVE_IDLE)
    assert transport.flush_count >= 1

    old_turn = store.turn("default", 1)
    assert old_turn["status"] == "interrupted"
    assert old_turn["barge_in"] == 1
    assert old_turn["user_text"] == "你好"
    # 仅已送入播放队列的两句进入记忆，未播的第三句只落在事件日志
    assert old_turn["assistant_text"] == "第一句。第二句。"
    assert 0 < old_turn["spoken_ratio"] < 1
    assert [message.content for message in orchestrator._history] == [
        "你好",
        "第一句。第二句。",
    ]
    events = store.events("default")
    dropped = [item for item in events if item["type"] == "barge_in.dropped_text"]
    assert dropped and dropped[-1]["payload"]["text"] == "第三句很长没有结束"

    snapshot = orchestrator.barge_in_snapshot()
    assert snapshot["counters"]["accepted"] == 1
    assert orchestrator.metrics.counter(BARGE_IN_ACCEPTED) == 1
    assert orchestrator.metrics.summary(BARGE_IN).count == 1

    llm.release()
    await asyncio.wait_for(handle_task, timeout=5.0)
    await orchestrator.shutdown()


async def test_twenty_consecutive_barge_ins_keep_turns_consistent(tmp_path: Path) -> None:
    """T-8.2 / C-08：连续 20 次打断，turn_id 单调递增且每轮问答不串轮。"""
    settings = make_settings(tmp_path)
    transport = FakeTransport(frames=120, frame_delay=0.01)
    llm = EchoLLM()
    tts = FakeTTS(chunks=2, interval_s=0.015)
    orchestrator, store = make_orchestrator(
        settings, tmp_path, transport=transport, providers={"llm": llm, "tts": tts}
    )
    await orchestrator.start()

    for index in range(20):
        await orchestrator._on_speech_start()
        turn = await wait_until(lambda: orchestrator._current_turn)
        question = f"第{index + 1}次提问"
        await orchestrator._on_asr_event(
            ASREvent(type="final", text=question, lang="zh"), None
        )
        await wait_until(
            lambda current=turn: orchestrator.machine.state == State.SPEAKING
            and bool(current.spoken_fragments)
        )
        result = await orchestrator.interrupt("manual")
        assert result["accepted"] is True
        await wait_turn(store, index + 1, status="interrupted")

    turns = store.turns("default")
    assert [item["seq"] for item in turns] == list(range(1, 22))
    interrupted = [item for item in turns if item["status"] == "interrupted"]
    assert len(interrupted) == 20
    for index, item in enumerate(interrupted, start=1):
        assert item["user_text"] == f"第{index}次提问"
        assert item["assistant_text"] == f"答复：第{index}次提问。"
    for index, request in enumerate(llm.requests):
        assert request.messages[-1].content == f"第{index + 1}次提问"
    assert orchestrator.turns.current_turn_id == 21
    assert orchestrator.turns.stale_dropped == 0

    await orchestrator.shutdown()
