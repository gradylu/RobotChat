"""工具安全策略（T-5.10、design §9.4、FR-9.10）。

四类校验：黑白名单、参数 schema、风险分级二次确认、调用限流。
策略层只做判定，不执行动作；确认交互由编排层负责播报与接管语音。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from robotchat.observability.logging import get_logger

logger = get_logger("robotchat.tools.policy")

# 语音确认判定词表：确认问题播报期间 ASR 结果只用于这里，不新建轮次
CONFIRM_WORDS = ("确认", "确定", "执行", "是的", "好的", "同意", "yes", "ok", "confirm")
CANCEL_WORDS = ("取消", "不要", "不用", "算了", "否", "不执行", "no", "cancel", "stop")

CONFIRM_ANSWER = "confirm"
CANCEL_ANSWER = "cancel"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    needs_confirm: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "needs_confirm": self.needs_confirm,
            "reason": self.reason,
        }


class ToolPolicy:
    """白名单 / 黑名单 / 参数校验 / 风险确认 / 限流。"""

    def __init__(
        self,
        *,
        whitelist: list[str] | None = None,
        blacklist: list[str] | None = None,
        rate_limit_per_minute: int = 30,
        high_risk_requires_confirm: bool = True,
        confirm_timeout_ms: int = 10000,
        clock=None,
    ) -> None:
        self.whitelist = set(whitelist or ())
        self.blacklist = set(blacklist or ())
        self.rate_limit_per_minute = max(1, int(rate_limit_per_minute))
        self.high_risk_requires_confirm = high_risk_requires_confirm
        self.confirm_timeout_ms = max(1, int(confirm_timeout_ms))
        self._clock = clock or time.monotonic
        self._calls: dict[str, deque[float]] = defaultdict(deque)
        self.denied = 0
        self.invalid = 0
        self.rate_limited = 0

    @classmethod
    def from_config(cls, cfg: Any) -> ToolPolicy:
        data = cfg.model_dump() if hasattr(cfg, "model_dump") else dict(cfg or {})
        return cls(
            whitelist=list(data.get("whitelist", []) or []),
            blacklist=list(data.get("blacklist", []) or []),
            rate_limit_per_minute=int(data.get("rate_limit_per_minute", 30) or 30),
            high_risk_requires_confirm=bool(
                data.get("high_risk_requires_confirm", True)
            ),
            confirm_timeout_ms=int(data.get("confirm_timeout_ms", 10000) or 10000),
        )

    # ------------------------------------------------------------ 判定

    def check(
        self,
        name: str,
        args: dict[str, Any],
        *,
        risk: str = "low",
        parameters: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        if name in self.blacklist:
            self.denied += 1
            return PolicyDecision(False, reason=f"工具 {name} 在黑名单中")
        if self.whitelist and name not in self.whitelist:
            self.denied += 1
            return PolicyDecision(False, reason=f"工具 {name} 不在白名单中")
        errors = validate_parameters(parameters or {}, args)
        if errors:
            self.invalid += 1
            return PolicyDecision(False, reason="参数校验失败：" + "；".join(errors))
        if not self._take_rate_token(name):
            self.rate_limited += 1
            return PolicyDecision(False, reason=f"工具 {name} 调用过于频繁，已限流")
        needs_confirm = self.high_risk_requires_confirm and risk == "high"
        return PolicyDecision(True, needs_confirm=needs_confirm)

    def _take_rate_token(self, name: str) -> bool:
        """滑动一分钟窗口限流，防止误触发导致动作风暴。"""
        window_start = self._clock() - 60.0
        history = self._calls[name]
        while history and history[0] < window_start:
            history.popleft()
        if len(history) >= self.rate_limit_per_minute:
            return False
        history.append(self._clock())
        return True

    # ------------------------------------------------------------ 二次确认

    def decide_confirmation(self, text: str) -> str:
        """把确认问题期间的用户语音判定为 confirm / cancel / 空（无法判定）。

        取消词优先：出现「不」「取消」等否定表述时不执行，避免歧义下误动作。
        """
        normalized = (text or "").strip().lower()
        if not normalized:
            return ""
        if any(word in normalized for word in CANCEL_WORDS):
            return CANCEL_ANSWER
        if any(word in normalized for word in CONFIRM_WORDS):
            return CONFIRM_ANSWER
        return ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "whitelist": sorted(self.whitelist),
            "blacklist": sorted(self.blacklist),
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "high_risk_requires_confirm": self.high_risk_requires_confirm,
            "confirm_timeout_ms": self.confirm_timeout_ms,
            "denied": self.denied,
            "invalid": self.invalid,
            "rate_limited": self.rate_limited,
        }


def validate_parameters(schema: dict[str, Any], args: dict[str, Any]) -> list[str]:
    """按 JSON Schema 子集校验参数：类型 / 必填 / 枚举 / 数值区间 / 长度。"""
    if not schema:
        return []
    if not isinstance(args, dict):
        return ["参数必须是对象"]
    errors: list[str] = []
    properties = schema.get("properties") or {}
    required = schema.get("required") or []
    for key in required:
        if key not in args:
            errors.append(f"缺少必填参数 {key}")
    for key, value in args.items():
        spec = properties.get(key)
        if spec is None:
            if properties:
                errors.append(f"未知参数 {key}")
            continue
        errors.extend(_validate_value(key, spec, value))
    return errors


def _validate_value(key: str, spec: dict[str, Any], value: Any) -> list[str]:
    errors: list[str] = []
    expected = spec.get("type")
    type_map: dict[str, tuple[type, ...]] = {
        "string": (str,),
        "integer": (int,),
        "number": (int, float),
        "boolean": (bool,),
        "object": (dict,),
        "array": (list,),
    }
    if expected and expected in type_map:
        if expected == "integer" and isinstance(value, bool):
            errors.append(f"参数 {key} 应为整数")
            return errors
        if not isinstance(value, type_map[expected]):
            errors.append(f"参数 {key} 应为 {expected}")
            return errors
    enum = spec.get("enum")
    if enum is not None and value not in enum:
        errors.append(f"参数 {key} 必须是 {enum} 之一")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = spec.get("minimum")
        maximum = spec.get("maximum")
        if minimum is not None and value < minimum:
            errors.append(f"参数 {key} 不得小于 {minimum}")
        if maximum is not None and value > maximum:
            errors.append(f"参数 {key} 不得大于 {maximum}")
    if isinstance(value, str):
        min_length = spec.get("minLength")
        max_length = spec.get("maxLength")
        if min_length is not None and len(value) < int(min_length):
            errors.append(f"参数 {key} 长度不得小于 {min_length}")
        if max_length is not None and len(value) > int(max_length):
            errors.append(f"参数 {key} 长度不得大于 {max_length}")
    return errors


__all__ = [
    "CANCEL_ANSWER",
    "CONFIRM_ANSWER",
    "PolicyDecision",
    "ToolPolicy",
    "validate_parameters",
]
