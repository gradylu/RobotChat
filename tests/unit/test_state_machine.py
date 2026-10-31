"""T-3.4 状态机单测：转移表、Guard、完整性校验、非法事件忽略与审计（C-20~C-23）。"""

from __future__ import annotations

import pytest

from robotchat.core.events import Event, EventType
from robotchat.core.state_machine import (
    DEFAULT_TIMEOUTS_MS,
    MachineContext,
    State,
    StateMachine,
    Transition,
    default_transitions,
)


async def _send(machine: StateMachine, event: EventType) -> State:
    await machine.handle(Event(event))
    return machine.state


async def test_default_transition_table_passes_validation() -> None:
    machine = StateMachine()
    assert machine.state == State.BOOTING
    assert machine.is_resident(State.IDLE)
    assert machine.timeout_ms(State.IDLE) is None
    assert machine.timeout_ms(State.LISTENING) == 15000


async def test_boot_ready_branches_by_wakeword() -> None:
    with_wakeword = StateMachine()
    without_wakeword = StateMachine(context=MachineContext(wakeword_enabled=False))

    assert await _send(with_wakeword, EventType.EV_READY) == State.IDLE
    assert await _send(without_wakeword, EventType.EV_READY) == State.ACTIVE_IDLE
    assert without_wakeword.is_resident(State.ACTIVE_IDLE)


async def test_dialog_flow_and_tool_guard() -> None:
    machine = StateMachine(context=MachineContext(wakeword_enabled=False))
    assert await _send(machine, EventType.EV_READY) == State.ACTIVE_IDLE
    assert await _send(machine, EventType.EV_SPEECH_START) == State.LISTENING
    assert await _send(machine, EventType.EV_ASR_FINAL) == State.THINKING
    assert await _send(machine, EventType.EV_LLM_FIRST_TOKEN) == State.SPEAKING
    assert await _send(machine, EventType.EV_PLAYBACK_DONE) == State.ACTIVE_IDLE

    machine.context.tools_authorized = False
    await machine.handle(Event(EventType.EV_TEXT_INPUT))
    assert machine.state == State.THINKING
    assert await machine.handle(Event(EventType.EV_LLM_TOOL_CALL)) is None
    assert machine.state == State.THINKING
    assert machine.ignored_events == 1


async def test_illegal_event_is_ignored_and_counted() -> None:
    machine = StateMachine()
    await _send(machine, EventType.EV_READY)

    assert await _send(machine, EventType.EV_ASR_FINAL) == State.IDLE
    assert machine.ignored_events == 1


async def test_reset_is_global_and_audited() -> None:
    machine = StateMachine()
    await _send(machine, EventType.EV_READY)
    await _send(machine, EventType.EV_TEXT_INPUT)

    record = await machine.handle(Event(EventType.EV_RESET))
    assert record is not None
    assert record.from_state == State.THINKING
    assert record.to_state == State.BOOTING
    assert record.reason == "运维强制复位"
    assert machine.audit[-1].event == EventType.EV_RESET


async def test_health_fail_forces_error_and_recovery_returns_idle() -> None:
    machine = StateMachine()
    await _send(machine, EventType.EV_READY)
    assert await _send(machine, EventType.EV_HEALTH_FAIL) == State.ERROR
    assert await _send(machine, EventType.EV_RECOVERED) == State.IDLE


async def test_timeout_edges_prevent_stuck_states() -> None:
    machine = StateMachine()
    await _send(machine, EventType.EV_READY)
    await _send(machine, EventType.EV_WAKE_DETECTED)
    await _send(machine, EventType.EV_SPEECH_START)

    assert await _send(machine, EventType.EV_TIMEOUT) == State.ACTIVE_IDLE


def test_validation_rejects_state_without_timeout_or_resident() -> None:
    with pytest.raises(ValueError, match="缺少超时"):
        StateMachine(
            default_transitions(),
            timeouts_ms={},
            resident_states=("IDLE", "ACTIVE_IDLE"),
        )


def test_validation_rejects_state_without_outgoing_edge() -> None:
    transitions = [item for item in default_transitions() if item.src != State.WAKING]
    with pytest.raises(ValueError, match="没有声明任何出边"):
        StateMachine(transitions)


def test_validation_rejects_resident_state_with_timeout() -> None:
    timeouts = dict(DEFAULT_TIMEOUTS_MS)
    timeouts["IDLE"] = 1000
    with pytest.raises(ValueError, match="不得再配置超时"):
        StateMachine(timeouts_ms=timeouts, resident_states=("IDLE", "ACTIVE_IDLE"))


async def test_action_hook_failure_is_isolated() -> None:
    async def broken_action(event: Event, context: MachineContext) -> None:
        raise RuntimeError("action boom")

    machine = StateMachine(
        [Transition(State.BOOTING, EventType.EV_READY, State.IDLE, action="ready")],
        validate=False,
    )
    machine.register_action("ready", broken_action)

    assert await machine.handle(Event(EventType.EV_READY)) is not None
    assert machine.state == State.IDLE
