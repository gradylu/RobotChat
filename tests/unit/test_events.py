"""T-3.1 事件总线单测：有界丢弃、串行分发、订阅者异常隔离。"""

from __future__ import annotations

from robotchat.core.events import BoundedEventBus, Event, EventType


async def test_overflow_drops_newest_and_counts_by_type() -> None:
    bus = BoundedEventBus(maxsize=2)

    assert bus.publish(Event(EventType.EV_READY)) is True
    assert bus.publish(Event(EventType.EV_READY)) is True
    assert bus.publish(Event(EventType.EV_TIMEOUT)) is False

    assert bus.dropped == 1
    assert bus.dropped_by_type["EV_TIMEOUT"] == 1
    assert bus.pending == 2


async def test_process_one_dispatches_in_order() -> None:
    bus = BoundedEventBus()
    seen: list[EventType] = []
    bus.subscribe(lambda event: seen.append(event.type))

    bus.publish(Event(EventType.EV_READY))
    bus.publish(Event(EventType.EV_SPEECH_START))

    assert await bus.process_one() is True
    assert await bus.process_one() is True
    assert await bus.process_one() is False
    assert seen == [EventType.EV_READY, EventType.EV_SPEECH_START]


async def test_handler_failure_is_isolated() -> None:
    bus = BoundedEventBus()
    seen: list[EventType] = []

    def broken(event: Event) -> None:
        raise RuntimeError("handler boom")

    async def good(event: Event) -> None:
        seen.append(event.type)

    bus.subscribe(broken)
    bus.subscribe(good)
    bus.publish(Event(EventType.EV_READY))
    await bus.process_one()

    assert seen == [EventType.EV_READY]
    assert bus.errors == 1
    assert bus.processed == 1


async def test_drain_stops_at_limit() -> None:
    bus = BoundedEventBus()
    for _ in range(3):
        bus.publish(Event(EventType.EV_READY))

    assert await bus.drain(limit=2) == 2
    assert bus.pending == 1
    assert await bus.drain() == 1
    assert bus.processed == 3


async def test_snapshot_reports_bounded_state() -> None:
    bus = BoundedEventBus(maxsize=8)
    bus.publish(Event(EventType.EV_READY))

    snapshot = bus.snapshot()
    assert snapshot["maxsize"] == 8
    assert snapshot["pending"] == 1
    assert snapshot["dropped"] == 0
