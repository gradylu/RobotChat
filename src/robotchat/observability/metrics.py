"""延迟指标采集与分位数计算（T-0.6）。

约定（NFR-1）：
- `e2e_first_response` 以「尾点判定完成」为计时起点；
- 尾点判定耗时单独记录在 `endpoint` 阶段，两者独立可见，避免口径混淆。
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

# 阶段常量
ENDPOINT = "endpoint"                    # 尾点判定时长（独立计量）
ASR_PARTIAL = "asr_partial"
ASR_FINAL = "asr_final"
LLM_FIRST_TOKEN = "llm_first_token"
TTS_FIRST_CHUNK = "tts_first_chunk"
E2E_FIRST_RESPONSE = "e2e_first_response"
BARGE_IN = "barge_in"                    # 打断响应
TOOL_EXECUTION = "tool_execution"
WAKE_LATENCY = "wake_latency_ms"         # 唤醒命中 → 反馈开始播放（FR-14.10）

# 计数类指标（FR-14.13 / C-148）：不可用分位数表达，单独聚合
WAKE_COUNT = "wake_count"
WAKE_FALSE_POSITIVE = "wake_false_positive"
ACTIVE_WINDOW_EXPIRED = "active_window_expired"
STANDBY_RESULT_CACHED = "standby_result_cached"
OPEN_EAR_CLOUD_AUDIO = "open_ear_cloud_audio_ms"  # 常开聆听 + 云端 ASR 上传时长（FR-14.8）
# 打断计数（C-07 / C-10）：接受 / 回声抑制 / 拒绝各自可见
BARGE_IN_ACCEPTED = "barge_in_accepted"
BARGE_IN_ECHO_SUPPRESSED = "barge_in_echo_suppressed"
BARGE_IN_REJECTED = "barge_in_rejected"

KNOWN_STAGES = (
    ENDPOINT,
    ASR_PARTIAL,
    ASR_FINAL,
    LLM_FIRST_TOKEN,
    TTS_FIRST_CHUNK,
    E2E_FIRST_RESPONSE,
    BARGE_IN,
    TOOL_EXECUTION,
    WAKE_LATENCY,
)

KNOWN_COUNTERS = (
    WAKE_COUNT,
    WAKE_FALSE_POSITIVE,
    ACTIVE_WINDOW_EXPIRED,
    STANDBY_RESULT_CACHED,
    OPEN_EAR_CLOUD_AUDIO,
    BARGE_IN_ACCEPTED,
    BARGE_IN_ECHO_SUPPRESSED,
    BARGE_IN_REJECTED,
)


def percentile(values: Iterable[float], p: float) -> float:
    """线性插值分位数；空样本返回 0。"""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (p / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)


@dataclass(frozen=True, slots=True)
class StageSummary:
    stage: str
    count: int
    p50: float
    p90: float
    p95: float
    p99: float
    minimum: float
    maximum: float

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "stage": self.stage,
            "count": self.count,
            "p50": round(self.p50, 2),
            "p90": round(self.p90, 2),
            "p95": round(self.p95, 2),
            "p99": round(self.p99, 2),
            "min": round(self.minimum, 2),
            "max": round(self.maximum, 2),
        }


class MetricsCollector:
    """内存环形缓冲：每个阶段独立有界，避免长时间运行无限增长（INV-7）。"""

    def __init__(self, buffer_per_stage: int = 2000) -> None:
        if buffer_per_stage < 1:
            raise ValueError("buffer_per_stage 必须 >= 1")
        self._buffer_per_stage = buffer_per_stage
        self._samples: dict[str, deque[float]] = {}
        self._counters: dict[str, int] = {}
        self._lock = threading.Lock()

    def record(
        self,
        stage: str,
        latency_ms: float,
        *,
        session_id: str | None = None,
        provider: str | None = None,
    ) -> None:
        # session_id / provider 预留给按会话与按驱动维度统计；当前只聚合阶段维度
        del session_id, provider
        if latency_ms < 0:
            raise ValueError("latency_ms 不能为负")
        with self._lock:
            bucket = self._samples.get(stage)
            if bucket is None:
                bucket = deque(maxlen=self._buffer_per_stage)
                self._samples[stage] = bucket
            bucket.append(float(latency_ms))

    def summary(self, stage: str) -> StageSummary:
        with self._lock:
            samples = list(self._samples.get(stage, ()))
        return StageSummary(
            stage=stage,
            count=len(samples),
            p50=percentile(samples, 50),
            p90=percentile(samples, 90),
            p95=percentile(samples, 95),
            p99=percentile(samples, 99),
            minimum=min(samples) if samples else 0.0,
            maximum=max(samples) if samples else 0.0,
        )

    def increment(self, name: str, amount: int = 1) -> int:
        """计数类指标自增；返回自增后的值。"""
        if amount < 0:
            raise ValueError("amount 不能为负")
        with self._lock:
            value = self._counters.get(name, 0) + amount
            self._counters[name] = value
            return value

    def counter(self, name: str) -> int:
        with self._lock:
            return self._counters.get(name, 0)

    def counters(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)

    def snapshot(self) -> dict[str, dict[str, float | int | str]]:
        with self._lock:
            stages = list(self._samples)
        return {stage: self.summary(stage).as_dict() for stage in stages}

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._counters.clear()
