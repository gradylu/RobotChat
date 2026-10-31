"""口令鉴权（T-6.4 / C-98、NFR-5）。

- 口令由环境变量注入（`api.auth_token_env`），配置与日志中不出现明文；
- 写操作（POST / PUT / DELETE）统一走 `require_write` 依赖；
- 连续失败 5 次锁定 5 分钟；口令长度少于 8 位视为非法配置，拒绝启动。
"""

from __future__ import annotations

import hmac
import os
import time
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException, Request

from robotchat.config.loader import ConfigError

MIN_TOKEN_LENGTH = 8
MAX_FAILURES = 5
LOCK_SECONDS = 300.0
SESSION_IDLE_SECONDS = 1800.0


class AuthManager:
    """口令校验与失败锁定；未配置口令时视为开发模式（写操作放行并告警）。"""

    def __init__(
        self,
        token: str = "",
        *,
        clock: Any = time.monotonic,
        max_failures: int = MAX_FAILURES,
        lock_seconds: float = LOCK_SECONDS,
    ) -> None:
        self.token = token.strip()
        self._clock = clock
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self.failures = 0
        self.locked_until = 0.0
        self.successes = 0
        self.rejected = 0

    @classmethod
    def from_settings(
        cls, settings: Any, environ: Mapping[str, str] | None = None
    ) -> AuthManager:
        env = os.environ if environ is None else environ
        name = str(getattr(settings.api, "auth_token_env", "") or "")
        token = env.get(name, "").strip() if name else ""
        if token and len(token) < MIN_TOKEN_LENGTH:
            raise ConfigError(
                f"API 口令长度不足 {MIN_TOKEN_LENGTH} 位（来源环境变量 {name}），拒绝启动",
                [f"请设置至少 {MIN_TOKEN_LENGTH} 位的 {name}"],
            )
        return cls(token)

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def _locked(self) -> bool:
        return self._clock() < self.locked_until

    def check(self, token: str) -> tuple[bool, str]:
        """校验口令；返回 (是否通过, 失败原因)。"""
        if not self.enabled:
            return True, "auth_disabled"
        if self._locked():
            self.rejected += 1
            return False, "locked"
        candidate = (token or "").strip()
        if candidate and hmac.compare_digest(candidate, self.token):
            self.failures = 0
            self.successes += 1
            return True, "ok"
        self.failures += 1
        self.rejected += 1
        if self.failures >= self.max_failures:
            self.locked_until = self._clock() + self.lock_seconds
            return False, "locked_by_failures"
        return False, "invalid_token"

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "min_length": MIN_TOKEN_LENGTH,
            "failures": self.failures,
            "locked": self._locked(),
            "locked_remaining_s": round(max(0.0, self.locked_until - self._clock()), 1),
            "session_idle_seconds": SESSION_IDLE_SECONDS,
        }


def bearer_token(request: Request) -> str:
    """从 `Authorization: Bearer` 或 `X-API-Token` 读取口令。"""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-api-token", "").strip()


async def require_write(request: Request) -> None:
    """写操作鉴权依赖：未配置口令时放行（开发模式），否则必须校验通过。"""
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:  # pragma: no cover - 生命周期外不应到达
        raise HTTPException(status_code=503, detail="运行时未就绪")
    ok, reason = runtime.auth.check(bearer_token(request))
    if ok:
        return
    if reason == "locked":
        raise HTTPException(status_code=423, detail="口令错误次数过多，已临时锁定")
    raise HTTPException(status_code=401, detail="未认证或口令错误")


__all__ = [
    "AuthManager",
    "MIN_TOKEN_LENGTH",
    "SESSION_IDLE_SECONDS",
    "bearer_token",
    "require_write",
]
