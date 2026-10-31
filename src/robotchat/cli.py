"""`robotchat` CLI（T-1.9 / T-3.9 / T-3A.8）：音频诊断、唤醒词测试与会话运维命令。

用法：
    robotchat audio probe --duration 3
    robotchat audio loopback --duration 2
    robotchat wake test --input var/wake_test.wav
    robotchat wake status --url http://127.0.0.1:8080
    robotchat session reset --session default --url http://127.0.0.1:8080
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

import httpx

from robotchat.audio.profile import load_profile, resolve_profile
from robotchat.bootstrap import load_drivers
from robotchat.config.loader import ConfigError, load_settings, resolve_paths
from robotchat.observability.diagnostics import (
    AudioProbeResult,
    LoopbackResult,
    ProfileTestResult,
    WakeTestResult,
    loopback,
    probe,
    profile_report,
    profile_test,
    run_wake_test,
)
from robotchat.observability.logging import configure_logging
from robotchat.providers.registry import ProviderRegistry
from robotchat.runtime import build_runtime


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="robotchat", description="RobotChat 诊断与运维命令")
    subparsers = parser.add_subparsers(dest="group", required=True)

    audio = subparsers.add_parser("audio", help="音频链路诊断")
    audio_sub = audio.add_subparsers(dest="command", required=True)

    probe_parser = audio_sub.add_parser("probe", help="采集活性探测")
    probe_parser.add_argument("--duration", type=float, default=3.0, help="探测时长（秒）")
    probe_parser.add_argument("--input", default="var/audio_in.wav", help="file 通道的采集 WAV")

    loopback_parser = audio_sub.add_parser("loopback", help="播放 / 采集回环测试")
    loopback_parser.add_argument("--duration", type=float, default=2.0, help="测试时长（秒）")
    loopback_parser.add_argument(
        "--input", default="var/audio_in.wav", help="file 通道的采集 WAV"
    )
    loopback_parser.add_argument(
        "--output", default="var/audio_out.wav", help="file 通道的播放输出"
    )

    audio_sub.add_parser("devices", help="列出机型 Profile 与当前匹配结论")
    profile_test_parser = audio_sub.add_parser(
        "profile-test", help="按机型 Profile 验证采集 / 播放 / flush"
    )
    profile_test_parser.add_argument(
        "--profile", default="", help="要验证的 Profile 名（默认取配置中的生效 Profile）"
    )
    profile_test_parser.add_argument(
        "--duration", type=float, default=1.0, help="采集验证时长（秒）"
    )

    session = subparsers.add_parser("session", help="会话运维")
    session_sub = session.add_subparsers(dest="command", required=True)
    reset_parser = session_sub.add_parser("reset", help="强制复位异常会话（EV_RESET）")
    reset_parser.add_argument("--session", default="default", help="会话 ID")
    reset_parser.add_argument(
        "--url", default="http://127.0.0.1:8080", help="服务地址"
    )
    reset_parser.add_argument("--token", default="", help="API Token（默认读环境变量）")

    wake = subparsers.add_parser("wake", help="唤醒词诊断")
    wake_sub = wake.add_subparsers(dest="command", required=True)
    wake_test_parser = wake_sub.add_parser("test", help="用一段含唤醒词的音频验证链路")
    wake_test_parser.add_argument(
        "--input", default="var/wake_test.wav", help="包含唤醒词的 16bit WAV"
    )
    wake_test_parser.add_argument(
        "--timeout", type=float, default=5.0, help="等待唤醒事件的上限（秒）"
    )
    wake_status_parser = wake_sub.add_parser("status", help="查看唤醒状态与活跃窗口")
    wake_status_parser.add_argument(
        "--url", default="http://127.0.0.1:8080", help="服务地址"
    )
    wake_status_parser.add_argument("--token", default="", help="API Token（默认读环境变量）")

    doctor = subparsers.add_parser("doctor", help="启动自检（18 项 PASS / WARN / FAIL）")
    doctor.add_argument("--strict", action="store_true", help="存在 FAIL 时以非零码退出")
    doctor.add_argument("--json", action="store_true", help="以 JSON 输出结论")

    diagnose = subparsers.add_parser("diagnose", help="故障诊断包")
    diagnose_sub = diagnose.add_subparsers(dest="command", required=True)
    export_parser = diagnose_sub.add_parser(
        "export", help="导出脱敏配置 / 指标 / 自检 / 日志的诊断包"
    )
    export_parser.add_argument(
        "--out", default="var/robotchat-diag.tar.gz", help="输出 tar.gz 路径"
    )
    export_parser.add_argument("--url", default="http://127.0.0.1:8080", help="服务地址")
    export_parser.add_argument("--token", default="", help="API Token（默认读环境变量）")
    return parser


def reset_session(
    *,
    url: str = "http://127.0.0.1:8080",
    session_id: str = "default",
    token: str = "",
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """调用 `POST /api/session/{id}/reset`；可注入 client 便于单测。"""
    owns_client = client is None
    http = client or httpx.Client(timeout=5.0)
    try:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = http.post(
            f"{url.rstrip('/')}/api/session/{session_id}/reset", headers=headers
        )
        response.raise_for_status()
        return dict(response.json())
    finally:
        if owns_client:
            http.close()


def wake_status(
    *,
    url: str = "http://127.0.0.1:8080",
    token: str = "",
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """调用 `GET /api/wake` 读取唤醒开关、活跃窗口倒计时与 KWS 状态。"""
    owns_client = client is None
    http = client or httpx.Client(timeout=5.0)
    try:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = http.get(f"{url.rstrip('/')}/api/wake", headers=headers)
        response.raise_for_status()
        return dict(response.json())
    finally:
        if owns_client:
            http.close()


def export_diagnose(
    *,
    url: str = "http://127.0.0.1:8080",
    token: str = "",
    out: str | Path = "var/robotchat-diag.tar.gz",
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """导出诊断包（T-7.13 / C-132）：配置脱敏 + 指标 + 自检 + 日志，打包为 tar.gz。

    远端接口不可用时仍生成诊断包，失败原因写入 `manifest.json`，便于现场回传分析。
    """
    owns_client = client is None
    http = client or httpx.Client(timeout=30.0)
    base = url.rstrip("/")
    headers = {"X-API-Token": token} if token else {}
    files: dict[str, str] = {}
    errors: list[str] = []

    def fetch(name: str, method: str, path: str) -> None:
        try:
            response = http.request(method, f"{base}{path}", headers=headers)
            response.raise_for_status()
            files[name] = response.text
        except Exception as exc:  # 单项失败不阻断整体导出
            errors.append(f"{name}: {exc!r}")
            files[name] = json.dumps({"error": repr(exc)}, ensure_ascii=False)

    try:
        fetch("config.json", "GET", "/api/config")
        fetch("metrics.json", "GET", "/api/metrics")
        fetch("health.json", "GET", "/api/health")
        fetch("selfcheck.json", "POST", "/api/diagnostics/selfcheck")
        fetch("logs.jsonl", "GET", "/api/logs/download")
    finally:
        if owns_client:
            http.close()

    files["manifest.json"] = json.dumps(
        {
            "generated_at": round(time.time(), 3),
            "url": base,
            "token_supplied": bool(token),
            "files": sorted(files),
            "errors": errors,
            "note": "config.json 已由服务端脱敏；日志不含原始音频与密钥",
        },
        ensure_ascii=False,
        indent=2,
    )

    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(target, "w:gz") as archive:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(data))
    return {"ok": not errors, "path": str(target), "files": sorted(files), "errors": errors}


def _format_selfcheck(report: dict[str, Any]) -> str:
    lines: list[str] = []
    for item in report.get("items", []):
        lines.append(f"[{item['status']}] {item['name']}  {item['detail']}")
        for hint in item.get("hints", []):
            lines.append(f"       hint: {hint}")
    counts = report.get("counts", {})
    lines.append("-" * 60)
    lines.append(
        f"RESULT: {report.get('overall')} "
        f"(PASS {counts.get('PASS', 0)} / WARN {counts.get('WARN', 0)} / "
        f"FAIL {counts.get('FAIL', 0)})"
    )
    return "\n".join(lines)


def run_doctor(strict: bool = False, json_output: bool = False) -> int:
    """`robotchat doctor`：构建运行时并执行 18 项自检（T-7.5 / C-123）。

    FAIL 默认不阻断（服务仍可降级启动）；`--strict` 时以非零码退出，供 CI / 装机脚本使用。
    """
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"配置校验失败：\n{exc}", file=sys.stderr)
        return 2
    config_dir, data_dir = resolve_paths(os.environ)
    try:
        runtime = build_runtime(settings, config_dir, data_dir)
    except ConfigError as exc:
        print(f"运行时配置非法：\n{exc}", file=sys.stderr)
        return 2
    try:
        asyncio.run(runtime.registry.probe_health_all())
    except Exception as exc:  # 探活失败由自检项体现，不阻断输出
        print(f"provider 探活失败：{exc!r}", file=sys.stderr)
    runtime.refresh_driver_status()
    report = asyncio.run(runtime.selfcheck())
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_format_selfcheck(report))
    if strict and report["overall"] == "FAIL":
        return 1
    return 0


def _transport_config(settings, args: argparse.Namespace) -> dict[str, object]:
    config: dict[str, object] = dict(settings.audio.model_dump())
    if settings.audio.transport == "file":
        config["capture_path"] = args.input
        config["playback_path"] = getattr(args, "output", None)
        config["realtime"] = True
    return config


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # 会话运维走 HTTP 接口，不需要加载音频驱动（避免占用设备 / 模型）
    if args.group == "session":
        token = args.token or os.environ.get("ROBOTCHAT_API_TOKEN", "")
        try:
            reset_result = reset_session(
                url=args.url, session_id=args.session, token=token
            )
        except Exception as exc:
            print(
                json.dumps({"ok": False, "detail": repr(exc)}, ensure_ascii=False),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(reset_result, ensure_ascii=False, indent=2))
        return 0 if reset_result.get("reset") else 1

    # 诊断包导出走 HTTP 接口（T-7.13），同样不占用音频设备与模型
    if args.group == "diagnose":
        token = args.token or os.environ.get("ROBOTCHAT_API_TOKEN", "")
        try:
            export_result = export_diagnose(url=args.url, token=token, out=args.out)
        except Exception as exc:
            print(
                json.dumps({"ok": False, "detail": repr(exc)}, ensure_ascii=False),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(export_result, ensure_ascii=False, indent=2))
        return 0 if export_result.get("ok") else 1

    load_drivers()
    configure_logging(level="INFO", json_output=False)

    # 启动自检（T-7.5）：自行构建运行时并输出 18 项结论，不进入服务进程
    if args.group == "doctor":
        return run_doctor(strict=args.strict, json_output=args.json)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"配置校验失败：\n{exc}", file=sys.stderr)
        return 2

    # 唤醒状态走 HTTP 接口（读取运行中的服务，不重复占用模型）
    if args.group == "wake" and args.command == "status":
        token = args.token or os.environ.get("ROBOTCHAT_API_TOKEN", "")
        try:
            status = wake_status(url=args.url, token=token)
        except Exception as exc:
            print(
                json.dumps({"ok": False, "detail": repr(exc)}, ensure_ascii=False),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0

    registry = ProviderRegistry()

    # 机型 Profile 诊断：只读配置与 DDS 接口，不经过编排器
    if args.group == "audio" and args.command in ("devices", "profile-test"):
        config_dir, _ = resolve_paths(os.environ)
        if args.command == "devices":
            report = profile_report(settings, config_dir, environ=dict(os.environ))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 1
        try:
            if args.profile:
                profile = load_profile(config_dir, args.profile)
            else:
                profile = resolve_profile(
                    settings.audio, config_dir, environ=os.environ
                ).profile
            profile_result: ProfileTestResult = asyncio.run(
                profile_test(profile, duration_s=args.duration)
            )
        except Exception as exc:
            print(
                json.dumps({"ok": False, "detail": repr(exc)}, ensure_ascii=False),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(profile_result.as_dict(), ensure_ascii=False, indent=2))
        return 0 if profile_result.ok else 1

    # 唤醒词测试只读样音频，不打开音频设备
    if args.group == "wake":
        try:
            wakeword = settings.providers.wakeword
            config = dict(wakeword.drivers.get(wakeword.active, {}))
            config["confirm_twice"] = wakeword.confirm_twice
            provider = registry.create_active("kws", wakeword.active, config)
            wake_result: WakeTestResult = asyncio.run(
                run_wake_test(provider, args.input, timeout_s=args.timeout)
            )
        except Exception as exc:
            print(
                json.dumps({"ok": False, "detail": repr(exc)}, ensure_ascii=False),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(wake_result.as_dict(), ensure_ascii=False, indent=2))
        return 0 if wake_result.ok else 1

    transport = registry.create_active(
        "transport", settings.audio.transport, _transport_config(settings, args)
    )

    try:
        result: AudioProbeResult | LoopbackResult
        if args.command == "probe":
            result = asyncio.run(probe(transport, duration_s=args.duration))
        elif args.command == "loopback":
            result = asyncio.run(loopback(transport, duration_s=args.duration))
        else:  # pragma: no cover - argparse 已限制
            parser.error(f"未知命令：{args.command}")
            return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "detail": repr(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
