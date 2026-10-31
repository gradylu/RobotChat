"""声明式状态机（T-3.4、C-20 ~ C-23、INV-2、INV-4、ADR-07）。

- 转移表显式可枚举，杜绝散落的 if/else 状态判断；
- Guard 决定是否放行，Action 钩子在转移完成后执行（异常隔离）；
- 启动时做完整性校验：每个状态必须有出边、必须有超时或显式声明常驻无超时；
- 每次转移写入审计日志（from / to / event / turn_id / latency_ms / reason）。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from robotchat.core.events import Event, EventType
from robotchat.observability.logging import get_logger

logger = get_logger("robotchat.core.state_machine")


class State(str, Enum):
    BOOTING = "BOOTING"
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    INTERRUPTING = "INTERRUPTING"
    EXECUTING = "EXECUTING"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"
    ACTIVE_IDLE = "ACTIVE_IDLE"
    WAKING = "WAKING"


@dataclass
class MachineContext:
    """Guard 判定所需的运行时上下文（由编排层维护）。"""

    wakeword_enabled: bool = True
    barge_in_accepted: bool = False
    tools_authorized: bool = True
    text_valid: bool = True
    health_ok: bool = True


Guard = Callable[[Event, MachineContext], bool]
Action = Callable[[Event, MachineContext], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Transition:
    src: State | None  # None 表示「任意状态」
    event: EventType
    dst: State
    guard: Guard | None = None
    action: str | None = None
    reason: str = ""
    actor: str = ""


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    from_state: State | None
    to_state: State
    event: EventType
    turn_id: int | None
    latency_ms: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "from": self.from_state.value if self.from_state else None,
            "to": self.to_state.value,
            "event": self.event.value,
            "turn_id": self.turn_id,
            "latency_ms": self.latency_ms,
            "reason": self.reason,
        }


# ---------------------------------------------------------------- 默认转移表


def _wakeword_on(_event: Event, ctx: MachineContext) -> bool:
    return ctx.wakeword_enabled


def _wakeword_off(_event: Event, ctx: MachineContext) -> bool:
    return not ctx.wakeword_enabled


def _text_valid(_event: Event, ctx: MachineContext) -> bool:
    return ctx.text_valid


def _barge_in_accepted(_event: Event, ctx: MachineContext) -> bool:
    return ctx.barge_in_accepted


def _tools_authorized(_event: Event, ctx: MachineContext) -> bool:
    return ctx.tools_authorized


def default_transitions() -> list[Transition]:
    """design §5.3 转移表。"""
    return [
        # 启动
        Transition(State.BOOTING, EventType.EV_READY, State.IDLE, _wakeword_on, reason="启动完成"),
        Transition(
            State.BOOTING,
            EventType.EV_READY,
            State.ACTIVE_IDLE,
            _wakeword_off,
            reason="唤醒词关闭，常开聆听",
        ),
        Transition(State.BOOTING, EventType.EV_BOOT_FAIL, State.ERROR, reason="启动失败"),
        Transition(State.BOOTING, EventType.EV_TIMEOUT, State.ERROR, reason="启动超时"),
        # 待机
        Transition(State.IDLE, EventType.EV_WAKE_DETECTED, State.WAKING, reason="唤醒命中"),
        Transition(State.IDLE, EventType.EV_TIMEOUT, State.DEGRADED, reason="KWS 无心跳"),
        Transition(
            State.IDLE,
            EventType.EV_SESSION_START,
            State.ACTIVE_IDLE,
            reason="手动开始会话",
        ),
        Transition(
            State.IDLE,
            EventType.EV_TEXT_INPUT,
            State.THINKING,
            _text_valid,
            reason="待机态文本输入，视作显式唤醒",
        ),
        # 唤醒确认
        Transition(State.WAKING, EventType.EV_WAKE_ACK_DONE, State.ACTIVE_IDLE, reason="反馈完成"),
        Transition(
            State.WAKING,
            EventType.EV_SPEECH_START,
            State.LISTENING,
            reason="确认阶段直接说话",
        ),
        Transition(State.WAKING, EventType.EV_TIMEOUT, State.IDLE, reason="无有效语音"),
        # 活跃待命
        Transition(
            State.ACTIVE_IDLE,
            EventType.EV_SPEECH_START,
            State.LISTENING,
            reason="起音确认",
        ),
        Transition(
            State.ACTIVE_IDLE,
            EventType.EV_TEXT_INPUT,
            State.THINKING,
            _text_valid,
            reason="活跃期文本输入",
        ),
        Transition(
            State.ACTIVE_IDLE,
            EventType.EV_SESSION_ACTIVE_TIMEOUT,
            State.IDLE,
            reason="活跃窗口超时",
        ),
        Transition(
            State.ACTIVE_IDLE,
            EventType.EV_SESSION_END,
            State.IDLE,
            reason="用户主动结束",
        ),
        # 聆听
        Transition(State.LISTENING, EventType.EV_ASR_PARTIAL, State.LISTENING, reason="partial"),
        Transition(
            State.LISTENING,
            EventType.EV_SPEECH_START,
            State.LISTENING,
            reason="重置尾点计时",
        ),
        Transition(
            State.LISTENING,
            EventType.EV_ASR_FINAL,
            State.THINKING,
            _text_valid,
            reason="收到有效 final",
        ),
        Transition(
            State.LISTENING,
            EventType.EV_TIMEOUT,
            State.ACTIVE_IDLE,
            reason="无 final 超时，丢弃轮次",
        ),
        # 思考
        Transition(
            State.THINKING,
            EventType.EV_LLM_FIRST_TOKEN,
            State.SPEAKING,
            reason="首 token 到达",
        ),
        Transition(
            State.THINKING,
            EventType.EV_LLM_TOOL_CALL,
            State.EXECUTING,
            _tools_authorized,
            reason="工具已授权",
        ),
        Transition(
            State.THINKING,
            EventType.EV_BARGE_IN,
            State.INTERRUPTING,
            _barge_in_accepted,
            reason="打断判定成立",
        ),
        Transition(State.THINKING, EventType.EV_TIMEOUT, State.DEGRADED, reason="首 token 超时"),
        # 播报
        Transition(
            State.SPEAKING,
            EventType.EV_TTS_FIRST_CHUNK,
            State.SPEAKING,
            reason="首音频块",
        ),
        Transition(
            State.SPEAKING,
            EventType.EV_BARGE_IN,
            State.INTERRUPTING,
            _barge_in_accepted,
            reason="播报中被打断",
        ),
        Transition(State.SPEAKING, EventType.EV_TTS_DONE, State.SPEAKING, reason="等待播放完成"),
        Transition(
            State.SPEAKING,
            EventType.EV_PLAYBACK_DONE,
            State.ACTIVE_IDLE,
            reason="播放完成",
        ),
        Transition(State.SPEAKING, EventType.EV_TIMEOUT, State.DEGRADED, reason="播报超时"),
        # 打断清理
        Transition(
            State.INTERRUPTING,
            EventType.EV_INTERRUPT_DONE,
            State.LISTENING,
            reason="清理完成",
        ),
        Transition(
            State.INTERRUPTING,
            EventType.EV_TIMEOUT,
            State.LISTENING,
            reason="强制复位，防卡死",
        ),
        # 执行
        Transition(State.EXECUTING, EventType.EV_TOOL_DONE, State.THINKING, reason="工具完成"),
        Transition(State.EXECUTING, EventType.EV_TOOL_TIMEOUT, State.THINKING, reason="工具超时"),
        Transition(State.EXECUTING, EventType.EV_TOOL_ERROR, State.THINKING, reason="工具失败"),
        Transition(
            State.EXECUTING,
            EventType.EV_BARGE_IN,
            State.INTERRUPTING,
            _barge_in_accepted,
            reason="打断播报，工具任务默认继续",
        ),
        # 降级 / 错误
        Transition(
            State.DEGRADED,
            EventType.EV_HEALTH_OK,
            State.ACTIVE_IDLE,
            reason="主链路恢复",
        ),
        Transition(
            State.ERROR,
            EventType.EV_RECOVERED,
            State.IDLE,
            _wakeword_on,
            reason="恢复成功",
        ),
        Transition(
            State.ERROR,
            EventType.EV_RECOVERED,
            State.ACTIVE_IDLE,
            _wakeword_off,
            reason="恢复成功（常开聆听）",
        ),
        # 全局
        Transition(None, EventType.EV_CANCEL, State.INTERRUPTING, reason="全链路取消"),
        Transition(None, EventType.EV_RESET, State.BOOTING, reason="运维强制复位"),
        Transition(None, EventType.EV_HEALTH_FAIL, State.ERROR, reason="看门狗自恢复"),
    ]


DEFAULT_TIMEOUTS_MS: dict[str, int] = {
    "BOOTING": 15000,
    "WAKING": 5000,
    "LISTENING": 15000,
    "THINKING": 30000,
    "SPEAKING": 60000,
    "INTERRUPTING": 1000,
    "EXECUTING": 120000,
    "DEGRADED": 30000,
    "ERROR": 60000,
}

DEFAULT_RESIDENT_STATES = ("IDLE", "ACTIVE_IDLE")


class StateMachine:
    """单写者驱动的声明式状态机（调用方须串行化 `handle`）。"""

    def __init__(
        self,
        transitions: list[Transition] | None = None,
        *,
        timeouts_ms: Mapping[str, int] | None = None,
        resident_states: Mapping[str, Any] | list[str] | tuple[str, ...] | None = None,
        context: MachineContext | None = None,
        audit_size: int = 200,
        validate: bool = True,
    ) -> None:
        self.transitions = list(transitions or default_transitions())
        self.context = context or MachineContext()
        self._state = State.BOOTING
        self._actions: dict[str, Action] = {}
        self.audit: deque[TransitionRecord] = deque(maxlen=audit_size)
        self.transition_count = 0
        self.ignored_events = 0

        raw_timeouts = DEFAULT_TIMEOUTS_MS if timeouts_ms is None else timeouts_ms
        resident = DEFAULT_RESIDENT_STATES if resident_states is None else resident_states
        self._timeouts = _normalize_timeouts(raw_timeouts)
        self._resident = _normalize_states(resident)
        if validate:
            self.validate()

    # ------------------------------------------------------------ 状态与配置

    @property
    def state(self) -> State:
        return self._state

    def is_resident(self, state: State | str) -> bool:
        state = State(state) if isinstance(state, str) else state
        return state in self._resident

    def timeout_ms(self, state: State | str) -> int | None:
        state = State(state) if isinstance(state, str) else state
        if state in self._resident:
            return None
        return self._timeouts.get(state)

    def register_action(self, name: str, action: Action) -> None:
        self._actions[name] = action

    # ------------------------------------------------------------ 完整性校验

    def validate(self) -> None:
        """启动校验（INV-2）：缺超时且未声明常驻的状态直接拒绝启动。"""
        problems: list[str] = []
        for state in State:
            if not any(item.src == state for item in self.transitions):
                problems.append(f"{state.value} 没有声明任何出边")
            timeout = self._timeouts.get(state)
            if state in self._resident:
                if timeout is not None:
                    problems.append(f"{state.value} 已声明常驻无超时，不得再配置超时")
            elif timeout is None:
                problems.append(f"{state.value} 缺少超时兜底且未声明常驻无超时")
            elif timeout <= 0:
                problems.append(f"{state.value} 超时必须为正数：{timeout}")
        for item in self.transitions:
            if item.src is not None and not isinstance(item.src, State):
                problems.append(f"非法源状态：{item.src!r}")
            if not isinstance(item.dst, State):
                problems.append(f"非法目标状态：{item.dst!r}")
            if not isinstance(item.event, EventType):
                problems.append(f"非法事件类型：{item.event!r}")
        if problems:
            raise ValueError("状态机完整性校验失败：\n- " + "\n- ".join(problems))

    # ------------------------------------------------------------ 转移

    async def handle(self, event: Event) -> TransitionRecord | None:
        transition = self._select(event)
        if transition is None:
            self.ignored_events += 1
            logger.warning(
                "illegal_event_ignored",
                state=self._state.value,
                event_type=event.type.value,
                turn_id=event.turn_id,
            )
            return None

        source = self._state
        started = time.monotonic()
        self._state = transition.dst
        record = TransitionRecord(
            from_state=source,
            to_state=transition.dst,
            event=event.type,
            turn_id=event.turn_id,
            latency_ms=round((time.monotonic() - started) * 1000.0, 2),
            reason=transition.reason or str(event.payload.get("reason", "")),
        )
        self.transition_count += 1
        self.audit.append(record)
        logger.info("state_transition", transition=record.as_dict())

        if transition.action is not None:
            action = self._actions.get(transition.action)
            if action is None:
                logger.warning("state_action_missing", action=transition.action)
            else:
                try:
                    await action(event, self.context)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "state_action_failed",
                        action=transition.action,
                        to=transition.dst.value,
                    )
        return record

    def _select(self, event: Event) -> Transition | None:
        for item in self.transitions:
            if item.src != self._state or item.event != event.type:
                continue
            if self._guard(item, event):
                return item
        for item in self.transitions:
            if item.src is not None or item.event != event.type:
                continue
            if self._guard(item, event):
                return item
        return None

    def _guard(self, transition: Transition, event: Event) -> bool:
        if transition.guard is None:
            return True
        try:
            return bool(transition.guard(event, self.context))
        except Exception:
            logger.exception("state_guard_failed", event_type=event.type.value)
            return False

    # ------------------------------------------------------------ 观测

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "transitions": self.transition_count,
            "ignored_events": self.ignored_events,
            "timeouts_ms": {state.value: value for state, value in self._timeouts.items()},
            "resident_states": sorted(state.value for state in self._resident),
            "audit": [record.as_dict() for record in list(self.audit)[-10:]],
        }


def _normalize_timeouts(values: Mapping[str, int]) -> dict[State, int]:
    result: dict[State, int] = {}
    for key, value in values.items():
        try:
            state = State(key)
        except ValueError as exc:
            raise ValueError(f"未知状态：{key!r}") from exc
        result[state] = int(value)
    return result


def _normalize_states(values: Any) -> set[State]:
    result: set[State] = set()
    for key in values:
        result.add(State(key))
    return result


__all__ = [
    "State",
    "MachineContext",
    "Transition",
    "TransitionRecord",
    "StateMachine",
    "default_transitions",
    "DEFAULT_TIMEOUTS_MS",
    "DEFAULT_RESIDENT_STATES",
]
