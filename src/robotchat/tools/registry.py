"""工具注册表与执行适配器注册（T-5.4 / T-5.6 / T-5.11、design §9）。

- 工具定义由 `config/tools.yaml` 提供，业务层只依赖 `ToolDefinition` /
  `ToolAdapter` 两个契约；
- 执行统一走超时 + 取消令牌，长任务不阻塞对话；
- 每次调用记录参数、结果、耗时与触发轮次，便于审计（C-80 / C-86）。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from robotchat.core.cancel import CancellationToken
from robotchat.health import HealthStatus
from robotchat.observability.logging import get_logger
from robotchat.tools.base import ToolAdapter, ToolCall, ToolDefinition, ToolResult
from robotchat.tools.policy import PolicyDecision, ToolPolicy

logger = get_logger("robotchat.tools.registry")

_ADAPTERS: dict[str, Callable[[dict[str, Any]], ToolAdapter]] = {}


def register_adapter(kind: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """适配器注册装饰器：新增适配器只需一个文件 + 配置，业务代码零改动（C-87）。"""

    def decorator(factory: Callable[..., Any]) -> Callable[..., Any]:
        _ADAPTERS[kind] = factory
        return factory

    return decorator


def adapter_kinds() -> list[str]:
    return sorted(_ADAPTERS)


def create_adapter(kind: str, config: dict[str, Any]) -> ToolAdapter:
    factory = _ADAPTERS.get(kind)
    if factory is None:
        raise KeyError(f"未注册的工具适配器：{kind!r}（可用：{adapter_kinds()}）")
    return factory(config)


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """一次工具调用的审计记录。"""

    turn_id: int
    name: str
    args: dict[str, Any]
    ok: bool
    error: str
    duration_ms: float
    error_kind: str = ""
    ts: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "name": self.name,
            "args": self.args,
            "ok": self.ok,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "error_kind": self.error_kind,
            "ts": self.ts,
        }


class ToolRegistry:
    """工具定义 + 适配器 + 策略的统一入口。"""

    def __init__(
        self,
        policy: ToolPolicy,
        *,
        adapters: dict[str, ToolAdapter] | None = None,
        clock: Callable[[], float] = time.monotonic,
        audit_size: int = 100,
    ) -> None:
        self.policy = policy
        self._definitions: dict[str, ToolDefinition] = {}
        self._adapters = dict(adapters or {})
        self._clock = clock
        self.audit: deque[ToolInvocation] = deque(maxlen=audit_size)
        self.calls = 0
        self.failures = 0
        self.timeouts = 0
        self.denied = 0

    # ------------------------------------------------------------ 注册与查询

    def register(
        self, definition: ToolDefinition, adapter: ToolAdapter | None = None
    ) -> None:
        self._definitions[definition.name] = definition
        if adapter is not None:
            self._adapters[adapter.name] = adapter

    def get(self, name: str) -> ToolDefinition | None:
        return self._definitions.get(name)

    def definitions(self) -> list[ToolDefinition]:
        return sorted(self._definitions.values(), key=lambda item: item.name)

    def enabled_definitions(self) -> list[ToolDefinition]:
        """按策略过滤后可暴露给 LLM 的工具（黑白名单）。"""
        return [
            item
            for item in self.definitions()
            if item.name not in self.policy.blacklist
            and (not self.policy.whitelist or item.name in self.policy.whitelist)
        ]

    # ------------------------------------------------------------ 策略与执行

    def check(self, call: ToolCall) -> tuple[ToolDefinition | None, PolicyDecision]:
        definition = self.get(call.name)
        if definition is None:
            self.denied += 1
            return None, PolicyDecision(False, reason=f"未知工具 {call.name}")
        decision = self.policy.check(
            call.name,
            call.args,
            risk=definition.risk,
            parameters=definition.parameters,
        )
        if not decision.allowed:
            self.denied += 1
        return definition, decision

    async def invoke(
        self,
        call: ToolCall,
        cancel: CancellationToken | None = None,
        *,
        turn_id: int = 0,
        checked: bool = False,
    ) -> ToolResult:
        """执行工具：策略（可选）→ 适配器 → 超时 / 取消 → 审计。"""
        definition, decision = self.check(call) if not checked else (self.get(call.name), None)
        if definition is None:
            return self._fail(call, turn_id, "unknown", "unknown", 0.0)
        if decision is not None and not decision.allowed:
            return self._fail(call, turn_id, "denied", decision.reason, 0.0)
        adapter = self._adapters.get(definition.adapter)
        if adapter is None:
            return self._fail(
                call,
                turn_id,
                "adapter",
                f"适配器 {definition.adapter} 未注册",
                0.0,
            )
        started = self._clock()
        self.calls += 1
        timeout_s = max(0.001, float(definition.timeout_s))
        effective_cancel = cancel or CancellationToken(f"tool-{call.id}")
        try:
            result = await asyncio.wait_for(
                adapter.execute(call, effective_cancel), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            self.timeouts += 1
            elapsed = round((self._clock() - started) * 1000.0, 2)
            logger.warning(
                "tool_timeout",
                tool=call.name,
                turn_id=turn_id,
                timeout_s=timeout_s,
            )
            return self._record(
                ToolInvocation(
                    turn_id=turn_id,
                    name=call.name,
                    args=dict(call.args),
                    ok=False,
                    error=f"执行超时（{timeout_s:g}s）",
                    duration_ms=elapsed,
                    error_kind="timeout",
                    ts=time.time(),
                )
            )
        except asyncio.CancelledError:
            elapsed = round((self._clock() - started) * 1000.0, 2)
            self._record(
                ToolInvocation(
                    turn_id=turn_id,
                    name=call.name,
                    args=dict(call.args),
                    ok=False,
                    error="执行被取消",
                    duration_ms=elapsed,
                    error_kind="cancelled",
                    ts=time.time(),
                )
            )
            raise
        except Exception as exc:
            elapsed = round((self._clock() - started) * 1000.0, 2)
            return self._fail(call, turn_id, "adapter", repr(exc), elapsed)
        elapsed = round((self._clock() - started) * 1000.0, 2)
        if not result.ok:
            self.failures += 1
        invocation = ToolInvocation(
            turn_id=turn_id,
            name=call.name,
            args=dict(call.args),
            ok=result.ok,
            error=result.error or "",
            duration_ms=result.duration_ms or elapsed,
            error_kind=result.error_kind,
            ts=time.time(),
        )
        return self._record(invocation, output=result.output)

    def _fail(
        self,
        call: ToolCall,
        turn_id: int,
        kind: str,
        reason: str,
        elapsed_ms: float,
    ) -> ToolResult:
        self.failures += 1
        return self._record(
            ToolInvocation(
                turn_id=turn_id,
                name=call.name,
                args=dict(call.args),
                ok=False,
                error=reason,
                duration_ms=round(elapsed_ms, 2),
                error_kind=kind,
                ts=time.time(),
            )
        )

    def _record(self, invocation: ToolInvocation, *, output: str = "") -> ToolResult:
        self.audit.append(invocation)
        logger.info("tool_invoked", **invocation.as_dict())
        return ToolResult(
            ok=invocation.ok,
            output=output,
            error=None if invocation.ok else invocation.error,
            duration_ms=invocation.duration_ms,
            error_kind=invocation.error_kind,
        )

    # ------------------------------------------------------------ 观测

    async def health(self) -> HealthStatus:
        unhealthy = []
        for kind, adapter in self._adapters.items():
            try:
                status = await adapter.health()
            except Exception as exc:
                unhealthy.append(f"{kind}: {exc!r}")
                continue
            if status.state != "ok":
                unhealthy.append(f"{kind}: {status.state}")
        if unhealthy:
            return HealthStatus.degraded("；".join(unhealthy))
        return HealthStatus.ok(f"已注册 {len(self._definitions)} 个工具")

    def snapshot(self) -> dict[str, Any]:
        return {
            "definitions": [item.as_dict() for item in self.definitions()],
            "enabled": [item.name for item in self.enabled_definitions()],
            "adapters": sorted(self._adapters),
            "policy": self.policy.snapshot(),
            "calls": self.calls,
            "failures": self.failures,
            "timeouts": self.timeouts,
            "denied": self.denied,
            "recent": [item.as_dict() for item in list(self.audit)[-10:]],
        }


def build_tool_registry(
    tools_settings: Any,
    *,
    adapters: dict[str, ToolAdapter] | None = None,
) -> ToolRegistry:
    """按配置装配工具注册表：内置定义 + 配置覆盖 + 适配器实例化。

    新增工具只需在 `config/tools.yaml` 加定义；新增执行方式只需注册适配器。
    """
    import robotchat.tools.adapters  # noqa: F401
    import robotchat.tools.builtin.announce  # noqa: F401
    from robotchat.tools.builtin import load_definitions

    data = (
        tools_settings.model_dump()
        if hasattr(tools_settings, "model_dump")
        else dict(tools_settings or {})
    )
    policy = ToolPolicy.from_config(data.get("policy", {}))
    definitions = load_definitions(data.get("definitions", {}) or {})
    registry = ToolRegistry(policy, adapters=adapters)

    # 按适配器聚合目标配置，实例化一次即可服务多个工具（ROS2 连接复用）
    by_adapter: dict[str, dict[str, Any]] = {}
    for definition in definitions:
        kind = definition.adapter
        bucket = by_adapter.setdefault(kind, {"tools": {}})
        bucket["tools"][definition.name] = {
            "target": definition.target,
            "options": dict(definition.options),
            "timeout_s": definition.timeout_s,
        }

    for kind, config in by_adapter.items():
        if adapters and kind in adapters:
            continue
        try:
            registry._adapters[kind] = create_adapter(kind, config)
        except KeyError:
            logger.warning("tool_adapter_missing", adapter=kind)
    for definition in definitions:
        registry.register(definition)
    return registry


__all__ = [
    "ToolInvocation",
    "ToolRegistry",
    "adapter_kinds",
    "build_tool_registry",
    "create_adapter",
    "register_adapter",
]
