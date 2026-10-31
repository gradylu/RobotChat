"""应用运行时状态：配置、驱动注册表、指标与各子系统状态。"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from robotchat.api.auth import AuthManager
from robotchat.audio.adapters import create_device_adapter
from robotchat.audio.aec import AecCapabilities, resolve_aec_mode
from robotchat.audio.device import AdapterAudioTransport
from robotchat.audio.profile import ProfileResolution, resolve_profile
from robotchat.bootstrap import load_drivers
from robotchat.config.loader import (
    ConfigError,
    ConfigStore,
    changed_sections,
    masked_settings,
)
from robotchat.config.schema import Settings
from robotchat.core.orchestrator import DialogOrchestrator, build_provider_report
from robotchat.dialog.memory import SqliteMemoryStore
from robotchat.dialog.rag import RagService
from robotchat.dialog.session import SessionStore
from robotchat.observability.diagnostics import loopback
from robotchat.observability.logging import get_logger
from robotchat.observability.metrics import WAKE_FALSE_POSITIVE, MetricsCollector
from robotchat.observability.selfcheck import run_selfcheck
from robotchat.providers.registry import (
    ProviderRegistry,
    ResiliencePolicy,
    UnknownDriverError,
)
from robotchat.tools.registry import ToolRegistry, build_tool_registry

logger = get_logger("robotchat.runtime")


@dataclass(frozen=True, slots=True)
class DriverStatus:
    kind: str
    active: str
    state: str  # ok | degraded | unavailable
    detail: str = ""
    circuit: str = ""
    effective: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "active": self.active,
            "state": self.state,
            "detail": self.detail,
            "circuit": self.circuit,
            "effective": self.effective,
        }


@dataclass
class AppRuntime:
    settings: Settings
    registry: ProviderRegistry
    metrics: MetricsCollector
    config_dir: Path
    data_dir: Path
    rag: RagService | None = None
    sessions: SessionStore | None = None
    memory: SqliteMemoryStore | None = None
    tools: ToolRegistry | None = None
    dialogue: DialogOrchestrator | None = None
    started_at: float = field(default_factory=time.monotonic)
    ready: bool = False
    drivers: dict[str, DriverStatus] = field(default_factory=dict)
    legacy_statuses: dict[str, DriverStatus] = field(default_factory=dict)
    aec_mode: str = "none"
    aec_detail: str = ""
    device_profile: str = ""
    device_profile_source: str = ""
    device_profile_warnings: list[str] = field(default_factory=list)
    offline: bool = False
    offline_detail: str = ""
    warmup: dict[str, dict[str, Any]] = field(default_factory=dict)
    auth: AuthManager = field(default_factory=AuthManager)
    config: ConfigStore | None = None
    transport: Any | None = None

    def uptime_s(self) -> float:
        return round(time.monotonic() - self.started_at, 3)

    async def startup(self) -> None:
        """启动探测（离线 / 预热 / 健康），完成后才置 ready（deployment §7.1）。"""
        resilience = self.settings.resilience
        if resilience.offline_probe_on_start:
            offline, detail = await self.registry.detect_offline(
                resilience.offline_probe_url, resilience.offline_probe_timeout_ms
            )
            self.offline = offline
            self.offline_detail = detail
        if resilience.warmup_on_start:
            self.warmup = await self.registry.warmup_all()
        else:
            await self.registry.probe_health_all()
        self.refresh_driver_status()
        self.ready = True
        # 会话编排依赖音频设备与模型连接；现场设备缺失时降级运行，不影响其余端点
        if self.dialogue is not None:
            try:
                await self.dialogue.start()
            except Exception as exc:
                logger.exception("dialog_start_failed", detail=repr(exc))
        logger.info(
            "runtime_ready",
            env=self.settings.app.env,
            device_profile=self.settings.audio.device_profile,
            drivers={kind: status.state for kind, status in self.drivers.items()},
            offline=self.offline,
            offline_detail=self.offline_detail,
            warmup={
                kind: [item["name"] for item in value.get("drivers", [])]
                for kind, value in self.warmup.items()
            },
            aec_requested=self.settings.audio.aec.mode,
            aec_effective=self.aec_mode,
            aec_detail=self.aec_detail,
            aec_fallback_notice=self.settings.audio.aec.fallback_notice,
        )

    async def shutdown(self) -> None:
        """优雅关停：完成当前轮次 → 释放音频与模型连接 → 关闭落库（T-3.10 / C-29）。"""
        self.ready = False
        if self.dialogue is not None:
            try:
                await self.dialogue.shutdown(graceful=True)
            except Exception:
                logger.exception("dialog_shutdown_failed")
        if self.sessions is not None:
            try:
                self.sessions.close()
            except Exception:
                logger.warning("session_store_close_failed")
        if self.rag is not None:
            await self.rag.aclose()
        await self.registry.close()

    async def reset_session(self, session_id: str) -> dict[str, Any]:
        """强制复位：取消当前轮次并回到可用状态（T-3.9 / EV_RESET）。"""
        if self.dialogue is None:
            raise RuntimeError("会话编排未初始化")
        return await self.dialogue.reset(session_id)

    def language_snapshot(self) -> dict[str, Any]:
        """当前语言模式、生效语言与语言维度指标（T-2A.7）。"""
        if self.dialogue is not None:
            return self.dialogue.language.snapshot()
        language = self.settings.language
        return {
            "mode": language.mode,
            "current": language.fixed if language.mode == "fixed" else language.fallback,
            "fixed": language.fixed,
            "fallback": language.fallback,
            "supported": sorted(language.supported),
            "pending": None,
            "pending_count": 0,
            "stats": {},
        }

    def language_stats(self) -> dict[str, Any]:
        stats = self.language_snapshot().get("stats")
        return dict(stats) if isinstance(stats, dict) else {}

    def wake_snapshot(self) -> dict[str, Any]:
        """唤醒开关、活跃窗口倒计时与 KWS 状态（T-3A.8）。"""
        if self.dialogue is not None:
            return self.dialogue.wake_snapshot()
        wakeword = self.settings.providers.wakeword
        return {
            "enabled": wakeword.enabled,
            "state": "unknown",
            "active_window_ms": wakeword.active_window_ms,
            "active_window_remaining_s": None,
            "kws": None,
            "counters": self.metrics.counters(),
        }

    def barge_in_snapshot(self) -> dict[str, Any]:
        """打断参数、保护窗口、计数器与最近一次打断耗时（T-4.6 / C-10）。"""
        if self.dialogue is not None:
            return self.dialogue.barge_in_snapshot()
        settings = self.settings.barge_in
        return {
            "mode": settings.mode,
            "aec_mode": self.aec_mode,
            "guard_ms": settings.guard_ms,
            "threshold_boost_db": settings.threshold_boost_db,
            "confirm_frames": settings.confirm_frames,
            "semantic_check": settings.semantic_check,
            "echo_similarity_threshold": settings.echo_similarity_threshold,
            "state": "unknown",
            "counters": {},
            "last": None,
        }

    async def set_barge_in(self, params: dict[str, Any]) -> dict[str, Any]:
        """运行时热更新打断参数（T-4.6）：无需重启立即生效。"""
        if self.dialogue is None:
            raise RuntimeError("会话编排未初始化")
        return await self.dialogue.set_barge_in(params)

    def mark_false_wake(self, reason: str = "operator") -> int:
        if self.dialogue is None:
            return self.metrics.increment(WAKE_FALSE_POSITIVE)
        return self.dialogue.mark_false_wake(reason)

    async def set_language(
        self, mode: str | None = None, lang: str | None = None
    ) -> dict[str, Any]:
        """运行时切换语言开关（T-2A.7 / FR-15.9）。"""
        if self.dialogue is None:
            raise RuntimeError("会话编排未初始化")
        return await self.dialogue.set_language(mode, lang)

    # ------------------------------------------------------------ 配置热更新（T-6.3）

    async def apply_config_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        """校验 → 预检 → 原子生效 → 热应用；非法配置抛 ConfigError（C-57 / C-94）。"""
        if self.config is None:
            raise ConfigError("配置存储未初始化")
        previous = self.settings
        settings, merged_patch = self.config.apply(patch)
        changed = changed_sections(previous, settings)
        self._precheck(settings, changed)
        applied, restart = await self._hot_apply(settings, changed)
        logger.info("config_updated", changed={key: len(value) for key, value in changed.items()})
        return {
            "updated": True,
            "applied": applied,
            "restart_required": restart,
            "changed": changed,
            "patch": merged_patch,
            "config": masked_settings(settings),
        }

    async def rollback_config(self) -> dict[str, Any]:
        """回滚到上一版本配置并热应用（C-57）。"""
        if self.config is None:
            raise ConfigError("配置存储未初始化")
        previous = self.settings
        settings, patch = self.config.rollback()
        changed = changed_sections(previous, settings)
        self._precheck(settings, changed)
        applied, restart = await self._hot_apply(settings, changed)
        logger.info(
            "config_rolled_back",
            changed={key: len(value) for key, value in changed.items()},
        )
        return {
            "rolled_back": True,
            "applied": applied,
            "restart_required": restart,
            "changed": changed,
            "patch": patch,
            "config": masked_settings(settings),
        }

    def _precheck(self, settings: Settings, changed: dict[str, list[str]]) -> None:
        """预检：实例化即将生效的驱动配置，避免把不可用驱动写入生效配置（T-6.3）。"""
        if "providers" not in changed:
            return
        for kind in ("asr", "llm", "tts"):
            section = getattr(settings.providers, kind)
            for name in {section.active, section.fallback}:
                if not name:
                    continue
                try:
                    self.registry.create(kind, name, config=dict(section.drivers.get(name, {})))
                except UnknownDriverError:
                    continue  # 未注册驱动由运行期健康探测暴露，不阻塞配置保存
                except Exception as exc:
                    raise ConfigError(f"驱动 {kind}/{name} 预检失败：{exc!r}") from exc

    async def _hot_apply(
        self, settings: Settings, changed: dict[str, list[str]]
    ) -> tuple[list[str], list[str]]:
        applied: list[str] = []
        restart: list[str] = []
        self.settings = settings
        if "barge_in" in changed:
            if self.dialogue is not None:
                await self.dialogue.set_barge_in(settings.barge_in.model_dump())
            applied.append("barge_in")
        if "language" in changed:
            if self.dialogue is not None:
                await self.dialogue.reload_language()
            applied.append("language")
        if "providers" in changed:
            kinds = {
                part.split(".")[1]
                for part in changed["providers"]
                if part.count(".") >= 2
            }
            for kind in sorted(kinds):
                self._rebuild_provider(kind)
            applied.append("providers")
        if "tools" in changed:
            self.tools = build_tool_registry(settings.tools)
            if self.dialogue is not None:
                self.dialogue.tools = self.tools
            applied.append("tools")
        restart.extend(section for section in changed if section not in applied)
        self.refresh_driver_status()
        return applied, restart

    def _rebuild_provider(self, kind: str) -> None:
        """按新配置重建 provider 槽位（热切换不打断当前会话，后续轮次生效）。"""
        if kind == "wakeword":
            return  # 唤醒词模型 / 阈值变更需重启（KWS 资产在启动时加载）
        if kind == "knowledge":
            knowledge = self.settings.providers.knowledge
            cfg = knowledge.drivers.get(knowledge.active, {})
            fallback = knowledge.fallback if knowledge.fallback != knowledge.active else None
            self.registry.register_slot(
                "knowledge", knowledge.active, cfg, fallback,
                knowledge.drivers.get(fallback, {}) if fallback else None,
            )
            if self.rag is not None:
                slot = self.registry.slot("knowledge")
                retriever = slot.primary.driver if slot and slot.primary.usable else None
                self.rag = RagService.from_config(
                    retriever, cfg, enabled=retriever is not None
                )
                if self.dialogue is not None:
                    self.dialogue.rag = self.rag
            return
        sections = {"asr": self.settings.providers.asr, "llm": self.settings.providers.llm,
                    "tts": self.settings.providers.tts}
        section = sections.get(kind)
        if section is None:
            return
        fallback = section.fallback if section.fallback != section.active else None
        self.registry.register_slot(
            kind,
            section.active,
            section.drivers.get(section.active, {}),
            fallback,
            section.drivers.get(fallback, {}) if fallback else None,
        )
        if kind == "asr" and self.dialogue is not None:
            self.dialogue._asr_cloud = self.dialogue._detect_cloud_asr()

    async def switch_provider(self, kind: str, driver: str) -> dict[str, Any]:
        """热切换驱动（T-6.8 / C-53）：写入配置补丁并重建槽位，当前会话不中断。"""
        section = self._provider_section(kind)
        if driver not in section.drivers:
            raise ConfigError(
                f"providers.{kind}.drivers 中不存在 {driver!r}",
                [f"可用驱动：{sorted(section.drivers)}"],
            )
        settings, _ = self.config.apply(
            {"providers": {kind: {"active": driver}}}
        ) if self.config is not None else (self.settings, {})
        self.settings = settings
        self._rebuild_provider(kind)
        self.refresh_driver_status()
        logger.info("provider_switched", kind=kind, driver=driver)
        return {
            "switched": True,
            "kind": kind,
            "driver": driver,
            "providers": self.provider_report(),
        }

    async def test_provider(self, kind: str, driver: str) -> dict[str, Any]:
        """连通性测试（T-6.8 / C-95）：新实例探活，不影响生效驱动。"""
        section = self._provider_section(kind)
        if driver not in section.drivers:
            raise ConfigError(f"providers.{kind}.drivers 中不存在 {driver!r}")
        started = time.monotonic()
        instance: Any | None = None
        try:
            instance = self.registry.create(
                kind, driver, config=dict(section.drivers.get(driver, {}))
            )
            health = getattr(instance, "health", None)
            if health is None:
                result: Any = {"state": "ok"}
            else:
                result = await asyncio.wait_for(
                    health(), timeout=self.settings.resilience.half_open_probe_timeout_ms / 1000.0
                )
            if isinstance(result, dict):
                state = str(result.get("state", "ok"))
            else:
                state = str(getattr(result, "state", "ok"))
            ok = state == "ok"
            detail = str(getattr(result, "detail", ""))
        except Exception as exc:
            ok = False
            detail = repr(exc)
        finally:
            close = getattr(instance, "aclose", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()
        latency = round((time.monotonic() - started) * 1000.0, 2)
        return {
            "ok": ok,
            "kind": kind,
            "driver": driver,
            "latency_ms": latency,
            "detail": detail,
        }

    def _provider_section(self, kind: str) -> Any:
        mapping = {
            "asr": self.settings.providers.asr,
            "llm": self.settings.providers.llm,
            "tts": self.settings.providers.tts,
            "knowledge": self.settings.providers.knowledge,
        }
        section = mapping.get(kind)
        if section is None:
            raise ConfigError(f"未知 provider 类别：{kind!r}", [f"可用：{sorted(mapping)}"])
        return section

    # ------------------------------------------------------------ 运维诊断（T-6.11）

    async def selfcheck(self) -> dict[str, Any]:
        """启动自检 18 项（T-7.5 / C-123）：逐项 PASS / WARN / FAIL + 可操作 hint。"""
        return await run_selfcheck(self)

    async def run_audio_loopback(self, duration_s: float = 1.5) -> dict[str, Any]:
        """音频回环测试（C-99）：会话运行中拒绝执行，避免与会话争用设备。"""
        if self.dialogue is not None and self.dialogue.snapshot()["running"]:
            raise RuntimeError("会话运行中，请先结束会话再做音频回环测试")
        transport: Any = self.transport or (
            self.dialogue.transport if self.dialogue is not None else None
        )
        if transport is None:
            if self.settings.audio.transport == "file":
                config: dict[str, Any] = dict(self.settings.audio.model_dump())
                config["capture_path"] = str(self.data_dir / "audio_in.wav")
                config["playback_path"] = str(self.data_dir / "audio_loopback_out.wav")
                config["realtime"] = True
                transport = self.registry.create_active("transport", "file", config)
            else:
                raise RuntimeError("音频传输尚未初始化，无法执行回环测试")
        result = await loopback(transport, duration_s=duration_s)
        return result.as_dict()

    def refresh_driver_status(self) -> None:
        statuses = dict(self.legacy_statuses)
        for kind, slot in self.registry.slots().items():
            statuses[kind] = DriverStatus(
                kind=kind,
                active=slot.primary.name,
                state=slot.state,
                detail=slot.detail,
                circuit=slot.breaker.state,
                effective=slot.effective_name,
            )
        self.drivers = statuses

    def provider_report(self) -> dict[str, Any]:
        return build_provider_report(self.registry)

    def tools_snapshot(self) -> dict[str, Any]:
        """工具清单、策略与调用统计（C-80 / T-5.4）。"""
        if self.dialogue is not None:
            return self.dialogue.tools_snapshot()
        if self.tools is None:
            return {
                "enabled": False,
                "definitions": [],
                "enabled_tools": [],
                "adapters": [],
                "policy": {},
                "calls": 0,
                "failures": 0,
                "timeouts": 0,
                "denied": 0,
                "recent": [],
            }
        return {"enabled": True, **self.tools.snapshot()}

    def memory_snapshot(self) -> dict[str, Any]:
        """记忆驱动状态与滑窗 / 摘要规模（T-5.1 / T-5.3）。"""
        if self.dialogue is not None:
            return self.dialogue.memory_snapshot()
        if self.memory is None:
            return {"backend": "none"}
        return dict(self.memory.snapshot())


def _provider_sections(settings: Settings) -> list[tuple[str, Any]]:
    providers = settings.providers
    return [("asr", providers.asr), ("llm", providers.llm), ("tts", providers.tts)]


def _register_provider_slots(settings: Settings, registry: ProviderRegistry) -> None:
    for kind, section in _provider_sections(settings):
        fallback = section.fallback if section.fallback != section.active else None
        registry.register_slot(
            kind,
            section.active,
            section.drivers.get(section.active, {}),
            fallback,
            section.drivers.get(fallback, {}) if fallback else None,
        )

    knowledge = settings.providers.knowledge
    if knowledge.active == "none":
        logger.warning("knowledge_disabled", reason="active=none")
        return
    cfg = knowledge.drivers.get(knowledge.active, {})
    base_url = str(cfg.get("base_url", "") or "").strip()
    if not base_url or "${" in base_url:
        logger.warning("knowledge_disabled", reason="base_url 未配置", active=knowledge.active)
        return
    fallback = knowledge.fallback if knowledge.fallback != knowledge.active else None
    registry.register_slot(
        "knowledge",
        knowledge.active,
        cfg,
        fallback,
        knowledge.drivers.get(fallback, {}) if fallback else None,
    )


def _build_rag(settings: Settings, registry: ProviderRegistry) -> RagService:
    knowledge = settings.providers.knowledge
    cfg = knowledge.drivers.get(knowledge.active, {})
    slot = registry.slot("knowledge")
    retriever = slot.primary.driver if slot is not None and slot.primary.usable else None
    return RagService.from_config(retriever, cfg, enabled=retriever is not None)


def _legacy_driver_configs(settings: Settings) -> dict[str, tuple[str, dict[str, Any]]]:
    """非模型类驱动（Phase 1 已有）：KWS / 传输 / VAD / 记忆。"""
    configs: dict[str, tuple[str, dict[str, Any]]] = {}
    if settings.providers.wakeword.enabled:
        wakeword = settings.providers.wakeword
        configs["kws"] = (wakeword.active, wakeword.drivers.get(wakeword.active, {}))
    configs["transport"] = (settings.audio.transport, settings.audio.model_dump())
    configs["vad"] = (
        "silero_vad" if settings.vad.backend == "silero" else "energy_vad",
        settings.vad.model_dump(),
    )
    configs["memory"] = (settings.memory.backend, settings.memory.model_dump())
    return configs


def _instantiate_legacy(
    settings: Settings, registry: ProviderRegistry
) -> dict[str, DriverStatus]:
    statuses: dict[str, DriverStatus] = {}
    for kind, (active, driver_config) in _legacy_driver_configs(settings).items():
        try:
            registry.create_active(kind, active, driver_config)
        except UnknownDriverError as exc:
            statuses[kind] = DriverStatus(kind, active, "unavailable", str(exc))
            logger.warning("driver_not_registered", kind=kind, driver=active, detail=str(exc))
            continue
        except Exception as exc:  # 驱动初始化失败不应拖垮整个服务
            statuses[kind] = DriverStatus(kind, active, "unavailable", repr(exc))
            logger.exception("driver_init_failed", kind=kind, driver=active)
            continue
        statuses[kind] = DriverStatus(kind, active, "ok")
    return statuses


def _policy(settings: Settings) -> ResiliencePolicy:
    resilience = settings.resilience
    return ResiliencePolicy(
        failure_threshold=resilience.failure_threshold,
        failure_window_s=float(resilience.failure_window_seconds),
        cooldown_s=float(resilience.cooldown_seconds),
        probe_timeout_s=resilience.half_open_probe_timeout_ms / 1000.0,
        warmup_timeout_s=resilience.warmup_timeout_ms / 1000.0,
    )


def _session_store_path(settings: Settings, data_dir: Path) -> Path:
    """解析落库路径：配置的绝对目录不可用时回落到数据目录（容器 / 开发环境一致）。"""
    configured = Path(settings.memory.sqlite_path)
    if not configured.is_absolute():
        return data_dir / configured
    if configured.parent.exists():
        return configured
    return data_dir / configured.name


def build_runtime(settings: Settings, config_dir: Path, data_dir: Path) -> AppRuntime:
    load_drivers()
    registry = ProviderRegistry(_policy(settings))
    metrics = MetricsCollector(settings.observability.metrics_buffer_per_stage)
    legacy = _instantiate_legacy(settings, registry)
    _register_provider_slots(settings, registry)

    # 机型 Profile 选路（T-1A.2 / FR-16.9、FR-16.11）：失败直接拒绝进入就绪态
    profile_resolution: ProfileResolution | None = None
    device_transport: AdapterAudioTransport | None = None
    if settings.audio.transport in {"ros2_native", "ros2_bridge"}:
        profile_resolution = resolve_profile(settings.audio, config_dir, environ=os.environ)
        for warning in profile_resolution.warnings:
            logger.warning("device_profile_fallback", detail=warning)
        device_transport = AdapterAudioTransport(
            create_device_adapter(
                profile_resolution.profile,
                target_sample_rate=settings.audio.capture.target_sample_rate,
                target_channels=settings.audio.capture.target_channels,
                target_bit_depth=settings.audio.capture.target_bit_depth,
            )
        )

    capabilities = (
        AecCapabilities(
            hardware_aec=profile_resolution.profile.capabilities.hardware_aec,
            echo_reference=profile_resolution.profile.capabilities.echo_reference,
        )
        if profile_resolution is not None
        else AecCapabilities()
    )
    aec_mode, aec_detail = resolve_aec_mode(
        settings.audio.aec.mode, capabilities, settings.audio.aec.fallback_notice
    )
    sessions = SessionStore(_session_store_path(settings, data_dir))
    rag = _build_rag(settings, registry)
    memory = (
        SqliteMemoryStore(
            sessions, summary_max_chars=settings.memory.summary_max_chars
        )
        if settings.memory.backend == "sqlite"
        else None
    )
    tools = build_tool_registry(settings.tools)
    runtime = AppRuntime(
        settings=settings,
        registry=registry,
        metrics=metrics,
        config_dir=config_dir,
        data_dir=data_dir,
        rag=rag,
        sessions=sessions,
        memory=memory,
        tools=tools,
        dialogue=DialogOrchestrator(
            settings,
            registry,
            metrics,
            sessions,
            rag=rag,
            memory=memory,
            tools=tools,
            session_id="default",
            transport=device_transport,
            aec_mode=aec_mode,
        ),
        legacy_statuses=legacy,
        aec_mode=aec_mode,
        aec_detail=aec_detail,
        device_profile=(
            profile_resolution.profile.profile if profile_resolution is not None else ""
        ),
        device_profile_source=(
            profile_resolution.source if profile_resolution is not None else ""
        ),
        device_profile_warnings=(
            list(profile_resolution.warnings) if profile_resolution is not None else []
        ),
        auth=AuthManager.from_settings(settings, os.environ),
        config=ConfigStore(config_dir, data_dir, environ=os.environ),
        transport=device_transport,
    )
    runtime.refresh_driver_status()
    return runtime
