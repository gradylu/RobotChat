"""Barge-in 打断检测器（T-4.1 / T-4.2 / T-4.8）。

判定口径（design §6.1，需全部满足）：
- C1 当前处于播报期（由编排层通过 `mark_playback_start()` / `mark_playback_stop()` 告知）；
- C2 帧能量 ≥ 自适应噪声底 + 动态门限 Δ（播报期整体抬升 `threshold_boost_db`）；
- C3 连续语音帧数 ≥ `confirm_frames`（默认 3 帧 / ~60ms）；
- C4 已过播放起始保护窗口（`guard_ms` 留空时随 AEC 模式自适应：100 / 200 / 300 ms）；
- C5 语义确认（可选）：ASR partial 长度 ≥ 2 字且与最近播报文本相似度 < 阈值。

防自打断（T-4.2）：播报期间的能量共识仅产生「待语义确认」状态，必须由 ASR partial 与
最近播报文本做相似度比对；高相似直接判为回声丢弃并计数，不触发打断。若播报期间
ASR 不可用，待确认状态在 `SEMANTIC_TIMEOUT_S` 后按能量判定放行，避免打断能力整体失效。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

from robotchat.audio.vad import AdaptiveNoiseFloor
from robotchat.config.schema import BargeInSettings

# 保护窗口随 AEC 模式自适应（design §6.2 / FR-6.5 / C-169）
DEFAULT_GUARD_MS: dict[str, int] = {"hardware": 100, "software": 200, "none": 300}

# 语义确认的最小长度与超时（C5）：过短不足以判定，超时则退化为纯能量判定
SEMANTIC_MIN_CHARS = 2
SEMANTIC_TIMEOUT_S = 0.6

Trigger = Literal["vad", "energy", "semantic", "manual"]


def resolve_guard_ms(settings: BargeInSettings, aec_mode: str) -> int:
    """保护窗口：显式配置优先，否则按 AEC 实际生效模式取默认值。"""
    if settings.guard_ms is not None:
        return max(0, int(settings.guard_ms))
    return DEFAULT_GUARD_MS.get(aec_mode, DEFAULT_GUARD_MS["software"])


@dataclass(frozen=True, slots=True)
class BargeInDecision:
    """一次检测结论；`pending` 表示已过能量确认、等待语义确认。"""

    accepted: bool
    pending: bool = False
    echo_suppressed: bool = False
    trigger: Trigger = "vad"
    energy_db: float = 0.0
    noise_floor_db: float = 0.0
    reason: str = ""
    text: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "pending": self.pending,
            "echo_suppressed": self.echo_suppressed,
            "trigger": self.trigger,
            "energy_db": round(self.energy_db, 2),
            "noise_floor_db": round(self.noise_floor_db, 2),
            "reason": self.reason,
            "text": self.text,
        }


class BargeInDetector:
    """帧驱动 + 语义确认的打断检测器（与状态机、ASR、播放队列解耦，可独立单测）。"""

    def __init__(
        self,
        settings: BargeInSettings,
        aec_mode: str = "software",
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._floor = AdaptiveNoiseFloor(window=50, percentile=10.0)
        self._spoken: deque[str] = deque(maxlen=8)
        self._settings = settings
        self._aec_mode = aec_mode
        self._guard_ms = resolve_guard_ms(settings, aec_mode)

        self._playing = False
        self._playback_started_at: float | None = None
        self._confirm = 0
        self._pending_since: float | None = None
        self._decided = False  # 同一次播报内只放行一次
        self.accepted = 0
        self.echo_suppressed = 0
        self.pending = 0
        self.rejected = 0

    # ------------------------------------------------------------ 配置

    def apply_settings(self, settings: BargeInSettings, aec_mode: str | None = None) -> None:
        """运行时热更新参数（T-4.6）；下一次帧判定立即生效。"""
        self._settings = settings
        if aec_mode is not None:
            self._aec_mode = aec_mode
        self._guard_ms = resolve_guard_ms(settings, self._aec_mode)

    @property
    def enabled(self) -> bool:
        return self._settings.mode == "enabled"

    @property
    def guard_ms(self) -> int:
        return self._guard_ms

    @property
    def settings(self) -> BargeInSettings:
        return self._settings

    # ------------------------------------------------------------ 播报窗口

    def mark_playback_start(self, ts: float | None = None) -> None:
        """播报开始：重置确认计数并开启保护窗口。"""
        now = self._clock() if ts is None else ts
        if self._playing:
            return
        self._playing = True
        self._playback_started_at = now
        self._confirm = 0
        self._pending_since = None
        self._decided = False

    def mark_playback_stop(self) -> None:
        self._playing = False
        self._playback_started_at = None
        self._confirm = 0
        self._pending_since = None
        self._decided = False
        # 播报结束后清空回声比对基准，避免上一轮文本影响下一轮判定
        self._spoken.clear()

    def note_spoken_text(self, text: str) -> None:
        """记录已送入播放队列的助手文本片段（回声比对基准，FR-1.5 / T-4.2）。"""
        normalized = _normalize(text)
        if normalized:
            self._spoken.append(normalized)

    def reset(self) -> None:
        self.mark_playback_stop()
        self._floor.reset()
        self._spoken.clear()

    # ------------------------------------------------------------ 帧判定

    def observe_frame(self, energy_db: float, ts: float | None = None) -> BargeInDecision | None:
        """输入一帧能量；返回非空表示需要编排层处理的结论（含 pending / 回声）。"""
        if not self.enabled:
            return None
        now = self._clock() if ts is None else ts

        if not self._playing:
            # 非播报期持续学习噪声底，播报期冻结，避免回声抬高门限
            self._floor.update(energy_db)
            self._confirm = 0
            return None

        if self._decided:
            return None

        elapsed_ms = 0.0
        if self._playback_started_at is not None:
            elapsed_ms = max(0.0, (now - self._playback_started_at) * 1000.0)
        if elapsed_ms < self._guard_ms:
            self._confirm = 0
            return None

        if self._pending_since is not None:
            if (now - self._pending_since) >= SEMANTIC_TIMEOUT_S:
                return self._accept("vad", energy_db, "语义确认超时，按能量判定")
            return None

        threshold_db = self._floor.floor_db + self._settings.threshold_boost_db
        if energy_db >= threshold_db:
            self._confirm += 1
        else:
            self._confirm = 0

        if self._confirm < self._settings.confirm_frames:
            return None
        self._confirm = 0

        if self._settings.semantic_check:
            self._pending_since = now
            self.pending += 1
            return BargeInDecision(
                accepted=False,
                pending=True,
                trigger="vad",
                energy_db=energy_db,
                noise_floor_db=self._floor.floor_db,
                reason="能量确认通过，等待语义确认",
            )

        return self._accept("energy", energy_db, "能量多帧确认通过")

    # ------------------------------------------------------------ 语义确认

    def observe_partial(self, text: str, ts: float | None = None) -> BargeInDecision | None:
        """用 ASR partial 做语义确认：高相似判为回声丢弃，其余放行打断（T-4.2）。"""
        if not self.enabled or not self._playing or self._decided:
            return None
        if self._pending_since is None:
            return None

        normalized = _normalize(text)
        if len(normalized) < SEMANTIC_MIN_CHARS:
            return None

        similarity = self._similarity(normalized)
        if similarity >= self._settings.echo_similarity_threshold:
            self._pending_since = None
            self.echo_suppressed += 1
            self._confirm = 0
            return BargeInDecision(
                accepted=False,
                echo_suppressed=True,
                trigger="semantic",
                noise_floor_db=self._floor.floor_db,
                reason=f"与最近播报文本相似度 {similarity:.2f}，判为回声",
                text=text,
            )

        return self._accept("semantic", 0.0, f"语义确认通过（相似度 {similarity:.2f}）", text)

    def reject(self, reason: str = "manual") -> None:
        """人工 / 上层拒绝一次待确认打断。"""
        if self._pending_since is not None:
            self._pending_since = None
            self.rejected += 1
            self._confirm = 0

    def _accept(
        self, trigger: Trigger, energy_db: float, reason: str, text: str = ""
    ) -> BargeInDecision:
        self._pending_since = None
        self._decided = True
        self.accepted += 1
        return BargeInDecision(
            accepted=True,
            trigger=trigger,
            energy_db=energy_db,
            noise_floor_db=self._floor.floor_db,
            reason=reason,
            text=text,
        )

    def _similarity(self, normalized: str) -> float:
        best = 0.0
        for spoken in self._spoken:
            if normalized in spoken or spoken in normalized:
                return 1.0
            best = max(best, SequenceMatcher(None, normalized, spoken).ratio())
        return best

    # ------------------------------------------------------------ 观测

    def snapshot(self) -> dict[str, object]:
        return {
            "mode": self._settings.mode,
            "aec_mode": self._aec_mode,
            "guard_ms": self._guard_ms,
            "threshold_boost_db": self._settings.threshold_boost_db,
            "confirm_frames": self._settings.confirm_frames,
            "semantic_check": self._settings.semantic_check,
            "echo_similarity_threshold": self._settings.echo_similarity_threshold,
            "playing": self._playing,
            "noise_floor_db": round(self._floor.floor_db, 2),
            "counters": {
                "accepted": self.accepted,
                "pending": self.pending,
                "echo_suppressed": self.echo_suppressed,
                "rejected": self.rejected,
            },
        }


def _normalize(text: str) -> str:
    """回声比对口径：去掉空白与标点，只保留文字，避免标点差异干扰相似度。"""
    return "".join(ch for ch in text if ch.isalnum())


__all__ = [
    "DEFAULT_GUARD_MS",
    "SEMANTIC_MIN_CHARS",
    "SEMANTIC_TIMEOUT_S",
    "BargeInDecision",
    "BargeInDetector",
    "resolve_guard_ms",
]
