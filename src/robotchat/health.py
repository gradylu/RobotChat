"""健康状态模型，供各 provider / 子系统统一上报（/api/health）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

HealthState = Literal["ok", "degraded", "unavailable"]


@dataclass(frozen=True, slots=True)
class HealthStatus:
    state: HealthState
    detail: str = ""
    latency_ms: float | None = None

    @classmethod
    def ok(cls, detail: str = "", latency_ms: float | None = None) -> HealthStatus:
        return cls("ok", detail, latency_ms)

    @classmethod
    def degraded(cls, detail: str = "") -> HealthStatus:
        return cls("degraded", detail)

    @classmethod
    def unavailable(cls, detail: str = "") -> HealthStatus:
        return cls("unavailable", detail)
