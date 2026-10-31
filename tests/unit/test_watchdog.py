"""T-3.5 看门狗单测：状态超时、无进展自恢复、常驻态不误触发（C-22、C-27）。"""

from __future__ import annotations

from robotchat.core.events import Event, EventType
from robotchat.core.state_machine import DEFAULT_TIMEOUTS_MS, State, StateMachine
from robotchat.core.watchdog import Watchdog


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _to_listening(machine: StateMachine) -> None:
    await machine.handle(Event(EventType.EV_READY))
    await machine.handle(Event(EventType.EV_WAKE_DETECTED))
    await machine.handle(Event(EventType.EV_SPEECH_START))
    assert machine.state == State.LISTENING


def _machine(timeouts: dict[str, int] | None = None) -> StateMachine:
    return StateMachine(timeouts_ms=timeouts or dict(DEFAULT_TIMEOUTS_MS))


async def test_state_timeout_triggers_hook() -> None:
    clock = FakeClock()
    timeouts = dict(DEFAULT_TIMEOUTS_MS)
    timeouts["LISTENING"] = 100
    machine = _machine(timeouts)
    await _to_listening(machine)

    fired: list[State] = []

    async def on_timeout(state: State) -> None:
        fired.append(state)

    watchdog = Watchdog(machine, heartbeat_ms=5000, on_timeout=on_timeout, clock=clock)
    clock.advance(0.2)
    await watchdog.tick()

    assert fired == [State.LISTENING]
    assert watchdog.timeouts == 1


async def test_no_progress_triggers_stall_once() -> None:
    clock = FakeClock()
    timeouts = dict(DEFAULT_TIMEOUTS_MS)
    timeouts["LISTENING"] = 10000
    machine = _machine(timeouts)
    await _to_listening(machine)

    stalls: list[str] = []

    async def on_stall(reason: str) -> None:
        stalls.append(reason)

    watchdog = Watchdog(machine, heartbeat_ms=1000, on_stall=on_stall, clock=clock)
    clock.advance(1.2)
    await watchdog.tick()
    await watchdog.tick()

    assert len(stalls) == 1
    assert "无进展" in stalls[0]
    assert watchdog.stalls == 1


async def test_progress_suppresses_stall() -> None:
    clock = FakeClock()
    timeouts = dict(DEFAULT_TIMEOUTS_MS)
    timeouts["LISTENING"] = 10000
    machine = _machine(timeouts)
    await _to_listening(machine)

    stalls: list[str] = []

    async def on_stall(reason: str) -> None:
        stalls.append(reason)

    watchdog = Watchdog(machine, heartbeat_ms=1000, on_stall=on_stall, clock=clock)
    clock.advance(0.9)
    watchdog.mark_progress("audio_frame")
    clock.advance(0.5)
    await watchdog.tick()

    assert stalls == []


async def test_resident_states_never_timeout_or_stall() -> None:
    clock = FakeClock()
    machine = _machine()
    await machine.handle(Event(EventType.EV_READY))
    assert machine.state == State.IDLE

    fired: list[str] = []

    async def on_timeout(state: State) -> None:
        fired.append(f"timeout:{state.value}")

    async def on_stall(reason: str) -> None:
        fired.append(f"stall:{reason}")

    watchdog = Watchdog(
        machine, heartbeat_ms=1000, on_timeout=on_timeout, on_stall=on_stall, clock=clock
    )
    clock.advance(600.0)
    for _ in range(3):
        await watchdog.tick()

    assert fired == []
    assert watchdog.timeouts == 0 and watchdog.stalls == 0


async def test_state_change_resets_timers() -> None:
    clock = FakeClock()
    timeouts = dict(DEFAULT_TIMEOUTS_MS)
    timeouts["LISTENING"] = 100
    machine = _machine(timeouts)

    stalls: list[str] = []

    async def on_stall(reason: str) -> None:
        stalls.append(reason)

    # 看门狗先于状态变化创建，tick 才能观测到 BOOTING → LISTENING 并重置计时
    watchdog = Watchdog(machine, heartbeat_ms=1000, on_stall=on_stall, clock=clock)
    await _to_listening(machine)
    clock.advance(1.5)
    await watchdog.tick()
    assert stalls == []  # 首次 tick：状态刚变化，计时器重置

    clock.advance(1.2)
    await watchdog.tick()
    assert len(stalls) == 1
