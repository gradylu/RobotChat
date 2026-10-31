"""驱动注册表：新增厂商 = 新文件 + 注册装饰器 + 配置段，业务代码零改动（FR-10.2）。

除注册与实例化外，本模块还承担 Phase 2 的运行时韧性：
- 熔断降级 CLOSED / OPEN / HALF_OPEN + fallback 切换 + 冷却后自动回切（T-2.6、§7.5）
- provider 预热与连接池（T-2.7、ADR-04）
- 公网不可达时锁定云端驱动（T-2.10）
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Literal, TypeVar, cast

import httpx

from robotchat.health import HealthStatus
from robotchat.observability.logging import get_logger

T = TypeVar("T")

logger = get_logger("robotchat.providers.registry")

# 已支持的驱动类别（与 design §12 providers/audio 目录对应）
PROVIDER_KINDS = (
    "asr",
    "llm",
    "tts",
    "kws",
    "knowledge",
    "vad",
    "memory",
    "transport",
    "tool_adapter",
)

_REGISTRY: dict[str, dict[str, Callable[..., Any]]] = {}


class UnknownDriverError(LookupError):
    """配置引用了未注册的驱动。"""

    def __init__(self, kind: str, name: str, available: list[str]) -> None:
        self.kind = kind
        self.name = name
        self.available = available
        available_text = ", ".join(available) if available else "（无）"
        super().__init__(f"未知驱动 {kind}/{name}；已注册：{available_text}")


class ProviderUnavailableError(RuntimeError):
    """主驱动与 fallback 均不可用（熔断 / 离线 / 初始化失败）。"""

    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        super().__init__(f"{kind} provider 不可用：{detail}")


def register_provider(kind: str, name: str) -> Callable[[T], T]:
    """类装饰器：把驱动注册到指定类别。

    例：
        @register_provider("asr", "funasr_local")
        class FunASRLocalASR: ...
    """

    if kind not in PROVIDER_KINDS:
        raise ValueError(f"不支持的驱动类别 {kind!r}；可选：{', '.join(PROVIDER_KINDS)}")
    if not name:
        raise ValueError("驱动名不能为空")

    def decorator(driver: T) -> T:
        bucket = _REGISTRY.setdefault(kind, {})
        if name in bucket:
            raise ValueError(f"驱动重复注册：{kind}/{name}")
        bucket[name] = cast(Callable[..., Any], driver)
        return driver

    return decorator


def available_drivers(kind: str) -> list[str]:
    return sorted(_REGISTRY.get(kind, {}))


def is_registered(kind: str, name: str) -> bool:
    return name in _REGISTRY.get(kind, {})


class ProviderRegistry:
    """按配置实例化驱动；未知驱动给出明确错误而非静默降级。

    同时管理主备槽位（`ProviderSlot`）：熔断、fallback、预热与离线锁定都在这层完成，
    编排层只需 `await registry.acquire(kind)` 取当前可用驱动。
    """

    def __init__(
        self,
        policy: ResiliencePolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy or ResiliencePolicy()
        self._clock = clock
        self._slots: dict[str, ProviderSlot] = {}
        # 按名驱动（T-2A.2）：方言 ASR / 方言 TTS 不占主备槽位，各自独立熔断
        self._named: dict[tuple[str, str], DriverEntry] = {}
        self._named_breakers: dict[tuple[str, str], CircuitBreaker] = {}
        self.offline = False
        self.offline_detail = ""

    def create(self, kind: str, name: str, **kwargs: Any) -> Any:
        factory = _REGISTRY.get(kind, {}).get(name)
        if factory is None:
            raise UnknownDriverError(kind, name, available_drivers(kind))
        return factory(**kwargs)

    def create_active(
        self, kind: str, active: str, driver_config: dict[str, Any] | None = None
    ) -> Any:
        return self.create(kind, active, config=dict(driver_config or {}))

    # ------------------------------------------------------------ 槽位

    def _entry(self, kind: str, name: str, config: dict[str, Any]) -> DriverEntry:
        entry = DriverEntry(name=name, config=dict(config))
        try:
            entry.driver = self.create(kind, name, config=dict(config))
        except UnknownDriverError as exc:
            entry.init_error = str(exc)
            logger.warning("driver_not_registered", kind=kind, driver=name, detail=str(exc))
        except Exception as exc:  # 驱动初始化失败不应拖垮整个服务
            entry.init_error = repr(exc)
            logger.exception("driver_init_failed", kind=kind, driver=name)
        return entry

    def register_slot(
        self,
        kind: str,
        active: str,
        driver_config: dict[str, Any] | None = None,
        fallback: str | None = None,
        fallback_config: dict[str, Any] | None = None,
    ) -> ProviderSlot:
        primary = self._entry(kind, active, driver_config or {})
        fallback_entry = (
            self._entry(kind, fallback, fallback_config or {}) if fallback else None
        )
        slot = ProviderSlot(
            kind=kind,
            primary=primary,
            fallback=fallback_entry,
            breaker=CircuitBreaker(self.policy, self._clock),
        )
        slot.set_offline(self.offline)
        self._slots[kind] = slot
        return slot

    def slot(self, kind: str) -> ProviderSlot | None:
        return self._slots.get(kind)

    def require_slot(self, kind: str) -> ProviderSlot:
        slot = self._slots.get(kind)
        if slot is None:
            raise ProviderUnavailableError(kind, "未注册该类别 provider 槽位")
        return slot

    def slots(self) -> dict[str, ProviderSlot]:
        return dict(self._slots)

    async def acquire(self, kind: str) -> DriverEntry:
        return await self.require_slot(kind).acquire()

    def record_success(self, kind: str) -> None:
        slot = self.slot(kind)
        if slot is not None:
            slot.record_success()

    def record_failure(self, kind: str, reason: str = "") -> None:
        slot = self.slot(kind)
        if slot is not None:
            slot.record_failure(reason)

    # ------------------------------------------------------------ 按名驱动

    def named_entry(
        self, kind: str, name: str, config: dict[str, Any] | None = None
    ) -> DriverEntry:
        """按名取得（首次懒加载）驱动条目；不占用主备槽位（T-2A.2）。"""
        key = (kind, name)
        entry = self._named.get(key)
        if entry is not None:
            return entry
        entry = self._entry(kind, name, dict(config or {}))
        entry.locked = self.offline and bool(entry.config.get("cloud", False))
        if entry.locked:
            entry.locked_reason = "公网不可达，云端驱动已锁定（FR-10.7 / C-134）"
        self._named[key] = entry
        self._named_breakers[key] = CircuitBreaker(self.policy, self._clock)
        return entry

    async def acquire_named(
        self, kind: str, name: str, config: dict[str, Any] | None = None
    ) -> DriverEntry:
        """取按名驱动；熔断 OPEN 时直接抛错，由调用方降级到 fallback 语言（FR-15.15）。"""
        entry = self.named_entry(kind, name, config)
        if not entry.usable:
            detail = entry.locked_reason or entry.init_error or "驱动不可用"
            raise ProviderUnavailableError(kind, f"{name}：{detail}")
        breaker = self._named_breakers[(kind, name)]
        allowed, is_probe = breaker.try_acquire()
        if not allowed:
            raise ProviderUnavailableError(kind, f"{name} 熔断中（冷却后自动恢复）")
        if is_probe:
            healthy = await self._probe_entry(entry, breaker.policy.probe_timeout_s)
            if not healthy:
                breaker.record_failure()
                raise ProviderUnavailableError(kind, f"{name} 半开探测失败")
            breaker.record_success()
        return entry

    def record_named_success(self, kind: str, name: str) -> None:
        breaker = self._named_breakers.get((kind, name))
        if breaker is not None:
            breaker.record_success()

    def record_named_failure(self, kind: str, name: str, reason: str = "") -> None:
        breaker = self._named_breakers.get((kind, name))
        if breaker is not None:
            breaker.record_failure()
        entry = self._named.get((kind, name))
        if entry is not None and reason:
            entry.init_error = entry.init_error or reason

    def driver_languages(self, kind: str, name: str) -> frozenset[str] | None:
        """读取驱动声明的受支持语言；未声明返回 None（视为不限制）。"""
        entry = self._named.get((kind, name))
        driver = entry.driver if entry is not None else None
        if driver is None:
            slot = self._slots.get(kind)
            if slot is not None:
                for candidate in (slot.primary, slot.fallback):
                    if candidate is not None and candidate.name == name and candidate.driver:
                        driver = candidate.driver
                        break
        caps = getattr(driver, "capabilities", None)
        languages = getattr(caps, "languages", None)
        return languages if isinstance(languages, frozenset) else None

    async def _probe_entry(self, entry: DriverEntry, timeout_s: float) -> bool:
        health = getattr(entry.driver, "health", None)
        if health is None:
            return True
        try:
            status = await asyncio.wait_for(health(), timeout=timeout_s)
        except Exception as exc:
            entry.health = HealthStatus.unavailable(repr(exc))
            return False
        entry.health = status
        return status.state != "unavailable"

    def named_snapshot(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for (kind, name), entry in self._named.items():
            breaker = self._named_breakers.get((kind, name))
            result[f"{kind}/{name}"] = {
                "driver": entry.snapshot(),
                "circuit": None if breaker is None else breaker.snapshot(),
            }
        return result

    # ------------------------------------------------------------ 离线 / 预热

    def set_offline(self, offline: bool, detail: str = "") -> None:
        self.offline = offline
        self.offline_detail = detail
        for slot in self._slots.values():
            slot.set_offline(offline)
        for entry in self._named.values():
            if entry.driver is None:
                continue
            cloud = bool(entry.config.get("cloud", False))
            entry.locked = offline and cloud
            entry.locked_reason = (
                "公网不可达，云端驱动已锁定（FR-10.7 / C-134）" if entry.locked else ""
            )
        if offline:
            locked = [
                f"{slot.kind}/{entry.name}"
                for slot in self._slots.values()
                for entry in (slot.primary, slot.fallback)
                if entry is not None and entry.locked
            ]
            if locked:
                logger.warning(
                    "cloud_drivers_locked",
                    detail=detail,
                    drivers=sorted(locked),
                )

    async def detect_offline(
        self,
        url: str,
        timeout_ms: int,
        client: httpx.AsyncClient | None = None,
    ) -> tuple[bool, str]:
        """公网可达性探测；不可达时锁定所有 `cloud: true` 的驱动（T-2.10）。"""
        if not url:
            return False, "未配置公网探测地址，跳过离线检测"
        owns_client = client is None
        http = client or httpx.AsyncClient()
        try:
            response = await asyncio.wait_for(
                http.get(url, timeout=timeout_ms / 1000.0, follow_redirects=False),
                timeout=timeout_ms / 1000.0,
            )
            detail = f"公网可达（HTTP {response.status_code}）"
            offline = False
        except Exception as exc:
            offline = True
            detail = f"公网不可达：{exc!r}"
        finally:
            if owns_client:
                await http.aclose()
        self.set_offline(offline, detail)
        return offline, detail

    async def warmup_all(self) -> dict[str, dict[str, Any]]:
        """按 ADR-04 预热主备驱动，规避模型冷启动；单路失败只告警不影响启动。"""
        results: dict[str, dict[str, Any]] = {}
        for kind, slot in self._slots.items():
            entries = []
            for entry in (slot.primary, slot.fallback):
                if entry is None or not entry.usable:
                    continue
                entries.append(await self._warmup_entry(kind, entry))
            results[kind] = {"drivers": entries}
        logger.info(
            "provider_warmup_summary",
            results={
                kind: [item["name"] for item in value["drivers"]]
                for kind, value in results.items()
            },
        )
        return results

    async def _warmup_entry(self, kind: str, entry: DriverEntry) -> dict[str, Any]:
        driver = entry.driver
        if driver is None:
            return {"name": entry.name, "ok": False, "error": "驱动未初始化"}
        started = time.monotonic()
        try:
            warmup = getattr(driver, "warmup", None)
            if warmup is None:
                entry.health = await asyncio.wait_for(
                    driver.health(), timeout=self.policy.warmup_timeout_s
                )
            else:
                await asyncio.wait_for(warmup(), timeout=self.policy.warmup_timeout_s)
                entry.health = await asyncio.wait_for(
                    driver.health(), timeout=self.policy.warmup_timeout_s
                )
            entry.warmup_ms = round((time.monotonic() - started) * 1000.0, 2)
            logger.info(
                "provider_warmed",
                kind=kind,
                driver=entry.name,
                duration_ms=entry.warmup_ms,
                health=entry.health.state if entry.health else "unknown",
            )
            return {
                "name": entry.name,
                "ok": True,
                "duration_ms": entry.warmup_ms,
                "health": entry.health.state if entry.health else "unknown",
            }
        except Exception as exc:
            entry.warmup_ms = round((time.monotonic() - started) * 1000.0, 2)
            entry.health = HealthStatus.unavailable(repr(exc))
            logger.warning(
                "provider_warmup_failed",
                kind=kind,
                driver=entry.name,
                error=repr(exc),
                duration_ms=entry.warmup_ms,
            )
            return {
                "name": entry.name,
                "ok": False,
                "duration_ms": entry.warmup_ms,
                "error": repr(exc),
            }

    async def probe_health_all(self) -> None:
        for slot in self._slots.values():
            for entry in (slot.primary, slot.fallback):
                if entry is None or entry.driver is None or entry.locked:
                    continue
                driver = entry.driver
                try:
                    entry.health = await asyncio.wait_for(
                        driver.health(), timeout=self.policy.probe_timeout_s
                    )
                except Exception as exc:
                    entry.health = HealthStatus.unavailable(repr(exc))

    async def close(self) -> None:
        for slot in self._slots.values():
            for entry in (slot.primary, slot.fallback):
                if entry is None or entry.driver is None:
                    continue
                close = getattr(entry.driver, "aclose", None)
                if close is None:
                    continue
                try:
                    await close()
                except Exception:  # 关停阶段的清理失败只记录
                    logger.warning("provider_close_failed", kind=slot.kind, driver=entry.name)
        for (kind, name), entry in list(self._named.items()):
            close = getattr(entry.driver, "aclose", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:
                logger.warning("provider_close_failed", kind=kind, driver=name)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {kind: slot.snapshot() for kind, slot in self._slots.items()}


# ---------------------------------------------------------------- 熔断


CircuitState = Literal["CLOSED", "OPEN", "HALF_OPEN"]


@dataclass(frozen=True, slots=True)
class ResiliencePolicy:
    """熔断与预热参数；来源于配置 `resilience` 段，均可现场调整。"""

    failure_threshold: int = 5
    failure_window_s: float = 30.0
    cooldown_s: float = 30.0
    probe_timeout_s: float = 2.0
    warmup_timeout_s: float = 5.0


class CircuitBreaker:
    """CLOSED → 连续失败达阈值 → OPEN → 冷却期满 → HALF_OPEN → 探测成功回切。"""

    def __init__(
        self,
        policy: ResiliencePolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = policy or ResiliencePolicy()
        self._clock = clock
        self._state: CircuitState = "CLOSED"
        self._failures: deque[float] = deque()
        self._opened_at: float | None = None
        self._probing = False
        self._lock = threading.Lock()

    @property
    def policy(self) -> ResiliencePolicy:
        return self._policy

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_open_to_half()
            return self._state

    def _maybe_open_to_half(self) -> None:
        if self._state != "OPEN" or self._opened_at is None:
            return
        if self._clock() - self._opened_at >= self._policy.cooldown_s:
            self._state = "HALF_OPEN"
            self._probing = False

    def try_acquire(self) -> tuple[bool, bool]:
        """返回 (是否放行, 是否为半开探测请求)。"""
        with self._lock:
            self._maybe_open_to_half()
            if self._state == "CLOSED":
                return True, False
            if self._state == "OPEN":
                return False, False
            # HALF_OPEN：只放行一个探测请求，避免恢复瞬间打满主驱动
            if self._probing:
                return False, False
            self._probing = True
            return True, True

    def record_success(self) -> None:
        with self._lock:
            self._failures.clear()
            self._state = "CLOSED"
            self._opened_at = None
            self._probing = False

    def record_failure(self) -> None:
        with self._lock:
            now = self._clock()
            if self._state == "HALF_OPEN":
                self._state = "OPEN"
                self._opened_at = now
                self._probing = False
                return
            self._failures.append(now)
            window_start = now - self._policy.failure_window_s
            while self._failures and self._failures[0] < window_start:
                self._failures.popleft()
            if len(self._failures) >= self._policy.failure_threshold:
                self._state = "OPEN"
                self._opened_at = now
                self._failures.clear()
                logger.warning(
                    "circuit_open",
                    failure_threshold=self._policy.failure_threshold,
                    cooldown_s=self._policy.cooldown_s,
                )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._maybe_open_to_half()
            return {
                "state": self._state,
                "failures": len(self._failures),
                "cooldown_remaining_s": (
                    0.0
                    if self._state != "OPEN" or self._opened_at is None
                    else round(
                        max(0.0, self._policy.cooldown_s - (self._clock() - self._opened_at)),
                        2,
                    )
                ),
            }


# ---------------------------------------------------------------- 驱动槽位


@dataclass
class DriverEntry:
    """一路候选驱动（主或 fallback）及其初始化 / 锁定状态。"""

    name: str
    driver: Any | None = None
    config: dict[str, Any] = field(default_factory=dict)
    init_error: str = ""
    locked: bool = False
    locked_reason: str = ""
    health: HealthStatus | None = None
    warmup_ms: float | None = None

    @property
    def usable(self) -> bool:
        return self.driver is not None and not self.locked

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "usable": self.usable,
            "locked": self.locked,
            "locked_reason": self.locked_reason,
            "init_error": self.init_error,
            "health": None
            if self.health is None
            else {"state": self.health.state, "detail": self.health.detail},
            "warmup_ms": self.warmup_ms,
        }


@dataclass
class ProviderSlot:
    """一类 provider 的主备组合 + 熔断状态。"""

    kind: str
    primary: DriverEntry
    fallback: DriverEntry | None
    breaker: CircuitBreaker
    last_error: str = ""
    last_success_at: float | None = None

    @property
    def available(self) -> bool:
        return self.primary.usable or (self.fallback is not None and self.fallback.usable)

    @property
    def detail(self) -> str:
        return self.last_error or self._unavailable_detail()

    @property
    def effective_name(self) -> str:
        if self.fallback is not None and self.fallback.usable and (
            self.breaker.state != "CLOSED" or not self.primary.usable
        ):
            return self.fallback.name
        return self.primary.name

    async def acquire(self) -> DriverEntry:
        """取当前应使用的驱动；OPEN 直接切 fallback，HALF_OPEN 先探测主驱动。"""
        if self.primary.usable:
            allowed, is_probe = self.breaker.try_acquire()
            if allowed and not is_probe:
                return self.primary
            if allowed and is_probe:
                healthy = await self._probe(self.primary)
                if healthy:
                    self.breaker.record_success()
                    self.last_error = ""
                    logger.info("provider_recovered", kind=self.kind, driver=self.primary.name)
                    return self.primary
                self.breaker.record_failure()
                self.last_error = f"{self.primary.name} 半开探测失败"
                logger.warning(
                    "provider_probe_failed", kind=self.kind, driver=self.primary.name
                )

        if self.fallback is not None and self.fallback.usable:
            return self.fallback

        detail = self.last_error or self._unavailable_detail()
        raise ProviderUnavailableError(self.kind, detail)

    async def _probe(self, entry: DriverEntry) -> bool:
        health = getattr(entry.driver, "health", None)
        if health is None:
            return True
        try:
            status = await asyncio.wait_for(health(), timeout=self.breaker.policy.probe_timeout_s)
        except Exception as exc:  # 探测失败按不可用处理
            entry.health = HealthStatus.unavailable(repr(exc))
            return False
        entry.health = status
        return status.state != "unavailable"

    def record_success(self) -> None:
        self.breaker.record_success()
        self.last_error = ""
        self.last_success_at = time.monotonic()

    def record_failure(self, reason: str = "") -> None:
        self.last_error = reason
        self.breaker.record_failure()

    def set_offline(self, offline: bool) -> None:
        for entry in (self.primary, self.fallback):
            if entry is None or entry.driver is None:
                continue
            if bool(entry.config.get("cloud", False)):
                entry.locked = offline
                entry.locked_reason = (
                    "公网不可达，云端驱动已锁定（FR-10.7 / C-134）" if offline else ""
                )

    def _unavailable_detail(self) -> str:
        details = []
        for entry in (self.primary, self.fallback):
            if entry is None:
                continue
            if entry.locked:
                details.append(f"{entry.name} 锁定：{entry.locked_reason}")
            elif entry.init_error:
                details.append(f"{entry.name} 初始化失败：{entry.init_error}")
            else:
                details.append(f"{entry.name} 不可用")
        return "；".join(details)

    @property
    def state(self) -> str:
        if not self.available:
            return "unavailable"
        if (
            not self.primary.usable
            or self.breaker.state != "CLOSED"
            or (self.primary.health is not None and self.primary.health.state == "unavailable")
        ):
            return "degraded"
        return "ok"

    def snapshot(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "state": self.state,
            "effective": self.effective_name,
            "circuit": self.breaker.snapshot(),
            "primary": self.primary.snapshot(),
            "fallback": None if self.fallback is None else self.fallback.snapshot(),
            "last_error": self.last_error,
        }

    def capabilities(self) -> dict[str, Any]:
        driver = self.primary.driver if self.primary.usable else None
        if self.fallback is not None and self.fallback.usable and (
            self.breaker.state != "CLOSED" or not self.primary.usable
        ):
            driver = self.fallback.driver
        caps = getattr(driver, "capabilities", None)
        return _dataclass_fields(caps) if caps is not None else {}


def _dataclass_fields(obj: Any) -> dict[str, Any]:
    if not is_dataclass(obj):
        return {}
    result: dict[str, Any] = {}
    for item in fields(obj):
        value = getattr(obj, item.name)
        result[item.name] = sorted(value) if isinstance(value, frozenset) else value
    return result
