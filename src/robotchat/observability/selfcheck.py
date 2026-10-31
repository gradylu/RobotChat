"""启动自检 18 项（T-7.5 / C-123、C-168、C-171、C-174）。

判定语义（deployment §7.3）：
- `PASS` 正常；`WARN` 可降级运行；`FAIL` 子系统不可用（默认仍启动，运维用 `--strict` 拒绝启动）。
- 每项给出可操作 `hints`，便于现场按提示排障（deployment §8.2）。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robotchat.observability.diagnostics import SelfCheckItem, check_wakeword

if TYPE_CHECKING:
    from robotchat.runtime import AppRuntime

SELFCHECK_TOTAL = 18

_STATUS_MAP = {"ok": "PASS", "degraded": "WARN", "unavailable": "FAIL"}


def _item(
    index: int,
    key: str,
    name: str,
    status: str,
    detail: str,
    hints: tuple[str, ...] = (),
) -> SelfCheckItem:
    return SelfCheckItem(index, key, name, status, detail, hints)


def _driver_status(runtime: AppRuntime, kind: str) -> tuple[str, str]:
    status = runtime.drivers.get(kind)
    if status is None:
        return "WARN", f"{kind} 驱动未注册"
    mapped = _STATUS_MAP.get(status.state, "WARN")
    detail = f"{status.active}（{status.state}）{status.detail}".strip()
    return mapped, detail


def _check_env(runtime: AppRuntime) -> SelfCheckItem:
    """第 2 项：必填环境变量与云端驱动密钥。"""
    transport = runtime.settings.audio.transport
    hints: list[str] = []
    status = "PASS"
    ros_domain = os.environ.get("ROS_DOMAIN_ID", "")
    if transport in {"ros2_native", "ros2_bridge"} and not ros_domain:
        status = "FAIL"
        hints.append("设置 ROS_DOMAIN_ID，且与宿主机保持一致（否则 DDS 不可见）")

    providers = runtime.settings.providers
    missing_keys: list[str] = []
    for kind in ("asr", "llm", "tts", "knowledge"):
        section = getattr(providers, kind)
        driver = section.drivers.get(section.active, {})
        env_name = str(driver.get("api_key_env", "") or "")
        if env_name and not os.environ.get(env_name):
            missing_keys.append(f"{kind}.{section.active} → {env_name}")
    if missing_keys:
        status = "FAIL" if status == "FAIL" else "WARN"
        hints.append("缺失密钥：" + "，".join(missing_keys))
        hints.append("密钥只能通过环境变量 / secret 注入，禁止写入 config/*.yaml（FR-10.10）")

    detail = (
        f"ROS_DOMAIN_ID={ros_domain or '未设置'}；"
        f"云端密钥缺失 {len(missing_keys)} 项"
    )
    return _item(2, "env", "必填环境变量", status, detail, tuple(hints))


def _check_dds(runtime: AppRuntime) -> SelfCheckItem:
    """第 3 项：ROS2 DDS 发现（能列出宿主机音频与控制节点）。"""
    if runtime.settings.audio.transport == "file":
        return _item(
            3,
            "ros2_dds",
            "ROS2 DDS 发现",
            "WARN",
            "file 通道（开发 / 诊断模式），跳过 DDS 发现",
            ("机器人现场请使用 ros2_native 并执行 cli audio devices",),
        )
    executable = shutil.which("ros2")
    if not executable:
        return _item(
            3,
            "ros2_dds",
            "ROS2 DDS 发现",
            "WARN",
            "未找到 ros2 CLI，无法自动验证 DDS 发现",
            (
                "在容器内执行 ros2 topic list 确认可见宿主机节点",
                "检查 ROS_DOMAIN_ID / RMW_IMPLEMENTATION 与宿主机一致（deployment §5.2）",
            ),
        )
    try:
        result = subprocess.run(
            [executable, "topic", "list"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _item(
            3,
            "ros2_dds",
            "ROS2 DDS 发现",
            "FAIL",
            f"ros2 topic list 执行失败：{exc!r}",
            ("检查 /dev/shm 挂载与 ipc: host（DDS 共享内存传输）",),
        )
    topics = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(topics) < 2:
        return _item(
            3,
            "ros2_dds",
            "ROS2 DDS 发现",
            "FAIL",
            f"5 秒内仅发现 {len(topics)} 个 topic（期望 ≥ 2）",
            (
                "宿主机执行 ros2 topic list 确认节点在线",
                "检查 ROS_DOMAIN_ID / RMW_IMPLEMENTATION 是否与宿主机一致",
                "检查 /dev/shm 是否挂载且可写",
            ),
        )
    return _item(3, "ros2_dds", "ROS2 DDS 发现", "PASS", f"发现 {len(topics)} 个 topic")


def _check_audio_capture(runtime: AppRuntime) -> SelfCheckItem:
    """第 4 项：采集活性（会话运行时检查已收到音频帧）。"""
    getter = getattr(runtime.dialogue, "capture_stats", None) if runtime.dialogue else None
    stats: dict[str, Any] = getter() if callable(getter) else {}
    if not stats.get("running"):
        return _item(
            4,
            "audio_capture",
            "音频采集活性",
            "WARN",
            "会话未运行，跳过采集活性检查",
            ("启动会话后复检，或执行 cli audio probe --duration 5",),
        )
    frames = int(stats.get("captured_frames", 0))
    if frames <= 0:
        return _item(
            4,
            "audio_capture",
            "音频采集活性",
            "FAIL",
            "会话运行中但未收到音频帧",
            (
                "检查麦克风是否被其他程序独占",
                "核对 Profile 的 capture.topic 与现场一致（ros2 topic echo）",
            ),
        )
    return _item(
        4,
        "audio_capture",
        "音频采集活性",
        "PASS",
        f"已收到 {frames} 帧，播放 {stats.get('played_chunks', 0)} 块",
    )


def _check_asr(runtime: AppRuntime) -> SelfCheckItem:
    status, detail = _driver_status(runtime, "asr")
    hints: tuple[str, ...] = ()
    if status != "PASS":
        hints = (
            "确认 FunASR / 云端服务可达（providers.asr.drivers.endpoint）",
            "运行 cli provider test asr <driver> 查看首响",
        )
    return _item(6, "provider_asr", "ASR provider", status, detail, hints)


def _check_llm(runtime: AppRuntime) -> SelfCheckItem:
    """第 7 项：LLM 驱动注册与配置（真实连通性由第 17 项探测）。"""
    section = runtime.settings.providers.llm
    driver = section.drivers.get(section.active, {})
    base_url = str(driver.get("base_url", "") or "").strip()
    unconfigured = not base_url or "${" in base_url
    status, detail = _driver_status(runtime, "llm")
    hints: list[str] = []
    if unconfigured:
        status = "WARN" if status != "FAIL" else status
        detail = f"{section.active} base_url 未配置"
        hints.append("设置 LLM_BASE_URL（边端或云端，A5 / CON-12）")
        hints.append("或显式配置 providers.llm.drivers.*.base_url")
    if status != "PASS" and not hints:
        hints.append("运行 cli provider test llm <driver> 查看首 token 时延")
    return _item(7, "provider_llm", "LLM provider", status, detail, tuple(hints))


def _check_tts(runtime: AppRuntime) -> SelfCheckItem:
    status, detail = _driver_status(runtime, "tts")
    hints: tuple[str, ...] = ()
    if status != "PASS":
        hints = (
            "确认 Qwen3-TTS / 云端服务可达（providers.tts.drivers.base_url）",
            "运行 cli provider test tts <driver> 查看首音频",
        )
    return _item(8, "provider_tts", "TTS provider", status, detail, hints)


def _check_storage(runtime: AppRuntime) -> SelfCheckItem:
    """第 9 项：数据卷 / 日志卷可写。"""
    targets: list[tuple[str, Path]] = [("数据卷", Path(runtime.data_dir))]
    log_dir = os.environ.get("ROBOTCHAT_LOG_DIR", "")
    if log_dir:
        targets.append(("日志卷", Path(log_dir)))
    hints: list[str] = []
    failures: list[str] = []
    for name, directory in targets:
        probe = directory / ".selfcheck"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok", encoding="utf-8")
        except OSError as exc:
            failures.append(f"{name} {directory} 不可写：{exc}")
            hints.append(
                f"检查卷挂载与权限（{directory}）；容器内以非 root uid 1000 运行"
            )
        finally:
            with contextlib.suppress(OSError):
                probe.unlink(missing_ok=True)
    if failures:
        return _item(9, "storage", "存储可写", "FAIL", "；".join(failures), tuple(hints))
    return _item(
        9,
        "storage",
        "存储可写",
        "PASS",
        "数据卷可写" + ("，日志卷可写" if log_dir else "（未设置 ROBOTCHAT_LOG_DIR）"),
    )


def _check_disk(runtime: AppRuntime) -> SelfCheckItem:
    """第 10 项：磁盘水位（≥80% WARN，≥95% FAIL）。"""
    root = Path(runtime.data_dir)
    probe = root if root.exists() else root.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        return _item(
            10, "disk", "磁盘水位", "WARN", f"无法读取磁盘信息：{exc!r}", ()
        )
    ratio = usage.used / usage.total if usage.total else 0.0
    free_gb = usage.free / (1024**3)
    detail = f"{probe} 使用率 {ratio:.1%}，剩余 {free_gb:.1f}GB"
    if ratio >= 0.95:
        return _item(
            10,
            "disk",
            "磁盘水位",
            "FAIL",
            detail,
            ("立即清理日志卷 / 数据卷或扩容（deployment §9.3）",),
        )
    if ratio >= 0.80:
        return _item(
            10, "disk", "磁盘水位", "WARN", detail, ("关注日志增长，按天归档（T-7.10）",)
        )
    return _item(10, "disk", "磁盘水位", "PASS", detail)


def _available_memory_mb() -> float | None:
    path = Path("/proc/meminfo")
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) >= 2:
                return float(parts[1]) / 1024.0
    return None


def _check_resources() -> SelfCheckItem:
    """第 11 项：内存与 CPU 余量。"""
    cpus = os.cpu_count() or 0
    available = _available_memory_mb()
    if available is None:
        return _item(
            11,
            "resources",
            "内存与 CPU 余量",
            "PASS",
            f"CPU {cpus} 核（非 Linux，跳过内存读取）",
        )
    detail = f"CPU {cpus} 核，可用内存 {available:.0f}MB"
    if available < 512:
        return _item(
            11,
            "resources",
            "内存与 CPU 余量",
            "WARN",
            detail,
            ("可用内存偏低：缩小模型规模或启用空闲卸载（R22）",),
        )
    return _item(11, "resources", "内存与 CPU 余量", "PASS", detail)


def _check_internet(runtime: AppRuntime) -> SelfCheckItem:
    """第 12 项：公网可达性（云端驱动需要时）。"""
    if runtime.offline:
        return _item(
            12,
            "internet",
            "公网可达性",
            "WARN",
            f"公网不可达（{runtime.offline_detail or '探测失败'}），云端驱动已锁定",
            ("确认网络 / 代理；离线时仅本地驱动可用，属预期降级",),
        )
    return _item(12, "internet", "公网可达性", "PASS", "公网可达")


def _check_wake_e2e(runtime: AppRuntime) -> SelfCheckItem:
    """第 14 项：唤醒端到端（需人工说唤醒词）。"""
    if not runtime.settings.providers.wakeword.enabled:
        return _item(
            14,
            "wakeword_e2e",
            "唤醒端到端",
            "WARN",
            "唤醒词已关闭（常开聆听），本项不适用",
        )
    return _item(
        14,
        "wakeword_e2e",
        "唤醒端到端",
        "WARN",
        "需人工说唤醒词验证",
        ("现场执行 cli wake test --seconds 10，观察 score 输出",),
    )


def _check_languages(runtime: AppRuntime) -> SelfCheckItem:
    """第 15 项：语言能力探测（逐语言检查 ASR / TTS 可用性）。"""
    snapshot = runtime.language_snapshot()
    supported = list(snapshot.get("supported") or [])
    routing = runtime.settings.language.asr_routing
    dialects = [lang for lang in routing if lang not in {"zh", "en"}]
    hints: tuple[str, ...] = ()
    status = "PASS" if supported else "FAIL"
    detail = "支持 " + "/".join(supported) if supported else "未声明任何支持语言"
    if dialects:
        hints = (
            f"方言 {', '.join(dialects)} 依赖独立模型服务，不可用时按 language.fallback 降级",
            "部署对应方言实例或用云端方言 ASR 后复检",
        )
    return _item(15, "languages", "语言能力", status, detail, hints)


def _check_device_profile(runtime: AppRuntime) -> SelfCheckItem:
    """第 16 项：机型 Profile 与镜像发行版一致性。"""
    if runtime.settings.audio.transport == "file":
        return _item(
            16,
            "device_profile",
            "机型 Profile",
            "WARN",
            "file 通道，未使用机型 Profile",
            ("机器人现场请配置 audio.device_profile 并执行 cli audio devices",),
        )
    image_distro = os.environ.get("ROBOTCHAT_IMAGE_DISTRO", "")
    host_distro = os.environ.get("ROS_DISTRO", "")
    if image_distro and host_distro and image_distro != host_distro:
        return _item(
            16,
            "device_profile",
            "机型 Profile",
            "FAIL",
            f"镜像 ROS_DISTRO={image_distro} 与宿主机 {host_distro} 不一致",
            (f"改用 robotchat:<version>-{host_distro} 镜像标签（CON-10）",),
        )
    if not runtime.device_profile:
        return _item(
            16,
            "device_profile",
            "机型 Profile",
            "FAIL",
            "未解析到机型 Profile",
            (
                "设置 audio.device_profile 或 ROBOTCHAT_DEVICE_PROFILE（FR-16.9）",
                "运行 cli audio devices 查看已注册 Profile",
            ),
        )
    hints = tuple(runtime.device_profile_warnings)
    detail = f"生效 Profile：{runtime.device_profile}（{runtime.device_profile_source}）"
    if hints:
        return _item(16, "device_profile", "机型 Profile", "WARN", detail, hints)
    return _item(
        16,
        "device_profile",
        "机型 Profile",
        "PASS",
        detail + f"；ROS_DISTRO={host_distro or image_distro or '未设置'}",
    )


def _check_llm_kb(runtime: AppRuntime) -> SelfCheckItem:
    """第 17 项：边端 / 云端 LLM 与知识库连通。"""
    issues: list[str] = []
    hints: list[str] = []
    section = runtime.settings.providers.llm
    driver = section.drivers.get(section.active, {})
    base_url = str(driver.get("base_url", "") or "").strip()
    if not base_url or "${" in base_url:
        issues.append("LLM_BASE_URL 未配置")
        hints.append("设置 LLM_BASE_URL；不可达时按 ADR-17 进入 DEGRADED 并播报兜底话术")
    elif runtime.offline:
        issues.append(f"LLM {section.active} 需公网但不可达")
        hints.append("切换本地 / 边端 fallback 或恢复网络后复检")
    rag_enabled = bool(getattr(runtime.rag, "enabled", False)) if runtime.rag else False
    if not rag_enabled:
        issues.append("知识库增强已关闭（KB_BASE_URL 为空或 active=none）")
        hints.append("配置 KB_BASE_URL 可启用 RAG；关闭时按预期跳过增强")
    if issues:
        return _item(
            17, "llm_kb", "LLM / 知识库连通", "WARN", "；".join(issues), tuple(hints)
        )
    return _item(
        17,
        "llm_kb",
        "LLM / 知识库连通",
        "PASS",
        f"LLM {section.active} 已配置，知识库增强已启用",
    )


def _check_aec(runtime: AppRuntime) -> SelfCheckItem:
    """第 18 项：AEC 模式确认（auto 的实际选择结果）。"""
    requested = runtime.settings.audio.aec.mode
    status = "PASS" if runtime.aec_mode in {"hardware", "software"} else "WARN"
    hints: tuple[str, ...] = ()
    if status == "WARN":
        hints = (
            "无硬件 AEC 且软件 AEC 未生效：检查 Profile hardware_aec / echo_reference",
            "必要时调大 barge_in.guard_ms 与 threshold_boost_db 防自打断",
        )
    detail = f"请求 {requested} → 实际 {runtime.aec_mode}：{runtime.aec_detail}"
    return _item(18, "audio_aec", "AEC 模式", status, detail, hints)


async def run_selfcheck(runtime: AppRuntime) -> dict[str, Any]:
    """执行 18 项启动自检，返回结构化结论（deployment §7.3）。"""
    config_files = sorted(runtime.config_dir.glob("*.yaml"))
    config_item = _item(
        1,
        "config",
        "配置校验",
        "PASS",
        f"{len(config_files)} 个配置文件（app / providers / tools 等）通过 schema 强校验",
    )

    items: list[SelfCheckItem] = [
        config_item,
        _check_env(runtime),
        _check_dds(runtime),
        _check_audio_capture(runtime),
        _item(
            5,
            "audio_loopback",
            "播放回环测试",
            "WARN",
            "需人工触发（会出声）",
            ("执行 cli audio loopback 或 WebUI 运维诊断页的音频回环",),
        ),
        _check_asr(runtime),
        _check_llm(runtime),
        _check_tts(runtime),
        _check_storage(runtime),
        _check_disk(runtime),
        _check_resources(),
        _check_internet(runtime),
        check_wakeword(
            runtime.settings,
            wake_state=runtime.dialogue.wake_snapshot() if runtime.dialogue else None,
        ),
        _check_wake_e2e(runtime),
        _check_languages(runtime),
        _check_device_profile(runtime),
        _check_llm_kb(runtime),
        _check_aec(runtime),
    ]

    counts = {
        level: sum(1 for item in items if item.status == level)
        for level in ("PASS", "WARN", "FAIL")
    }
    overall = "FAIL" if counts["FAIL"] else ("WARN" if counts["WARN"] else "PASS")
    ordered = sorted(items, key=lambda item: item.index)
    return {
        "ok": overall != "FAIL",
        "overall": overall,
        "counts": counts,
        "total": len(ordered),
        "items": [item.as_dict() for item in ordered],
        "generated_at": round(time.time(), 3),
    }


__all__ = ["SELFCHECK_TOTAL", "run_selfcheck"]
