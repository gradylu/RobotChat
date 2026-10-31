"""Phase 7 容器化交付与运维契约测试（T-7.1 ~ T-7.13）。"""

from __future__ import annotations

import json
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from robotchat.cli import _format_selfcheck, export_diagnose
from robotchat.config.loader import load_settings
from robotchat.main import create_app
from robotchat.observability.logging import (
    DailyJsonlWriter,
    log_dir_from_env,
    log_retention_days_from_env,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY = REPO_ROOT / "deploy"


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    for key, value in {
        "ROBOTCHAT__RESILIENCE__OFFLINE_PROBE_ON_START": "false",
        "ROBOTCHAT__PROVIDERS__ASR__FALLBACK": "",
        "ROBOTCHAT__PROVIDERS__LLM__FALLBACK": "",
        "ROBOTCHAT__PROVIDERS__TTS__FALLBACK": "",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ROBOTCHAT_CONFIG_DIR", str(REPO_ROOT / "config"))
    monkeypatch.setenv("ROBOTCHAT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ROBOTCHAT_LOG_DIR", raising=False)
    settings = load_settings(config_dir=REPO_ROOT / "config", data_dir=tmp_path)
    return TestClient(create_app(settings))


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _client(tmp_path, monkeypatch) as test_client:
        yield test_client


# ---------------------------------------------------------------- T-7.5 十八项自检


def test_selfcheck_has_eighteen_ordered_items(client: TestClient) -> None:
    body = client.post("/api/diagnostics/selfcheck").json()
    assert body["total"] == 18
    assert [item["index"] for item in body["items"]] == list(range(1, 19))
    keys = [item["key"] for item in body["items"]]
    assert keys[0] == "config"
    assert "device_profile" in keys
    assert "llm_kb" in keys
    assert "audio_aec" in keys
    assert body["counts"]["PASS"] + body["counts"]["WARN"] + body["counts"]["FAIL"] == 18
    for item in body["items"]:
        assert item["status"] in {"PASS", "WARN", "FAIL"}
        assert isinstance(item["hints"], list)


def test_selfcheck_text_format_is_actionable() -> None:
    report = {
        "overall": "WARN",
        "counts": {"PASS": 1, "WARN": 1, "FAIL": 0},
        "items": [
            {
                "status": "WARN",
                "name": "LLM provider",
                "detail": "base_url 未配置",
                "hints": ["设置 LLM_BASE_URL"],
            }
        ],
    }
    text = _format_selfcheck(report)
    assert "[WARN] LLM provider" in text
    assert "hint: 设置 LLM_BASE_URL" in text
    assert "RESULT: WARN" in text


# ---------------------------------------------------------------- T-7.6 三级探测


def test_ready_reports_subsystem_detail(client: TestClient) -> None:
    response = client.get("/api/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["status"] == "ready"
    assert body["uptime_s"] >= 0
    assert {"asr", "llm", "tts"} <= set(body["subsystems"])
    assert isinstance(body["degraded"], list)


def test_live_and_health_are_independent(client: TestClient) -> None:
    assert client.get("/api/live").json() == {"status": "alive"}
    health = client.get("/api/health").json()
    assert health["status"] in {"ok", "degraded"}
    assert "subsystems" in health


# ---------------------------------------------------------------- T-7.10 日志轮转


def test_log_dir_and_retention_from_env() -> None:
    assert log_dir_from_env({"ROBOTCHAT_LOG_DIR": "/var/log/robotchat"}) == Path(
        "/var/log/robotchat"
    )
    assert log_dir_from_env({}) is None
    assert log_retention_days_from_env({"ROBOTCHAT_LOG_RETENTION_DAYS": "7"}) == 7
    assert log_retention_days_from_env({"ROBOTCHAT_LOG_RETENTION_DAYS": "bad"}) == 14


def test_daily_jsonl_writer_creates_file_and_prunes(tmp_path: Path) -> None:
    import os

    old = tmp_path / "robotchat-2020-01-01.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    stale = time.time() - 30 * 86400
    os.utime(old, (stale, stale))

    writer = DailyJsonlWriter(tmp_path, retention_days=14)
    writer(None, "robotchat.test", {"event": "hello"})
    current = [path for path in tmp_path.glob("robotchat-*.jsonl") if path != old]
    assert current, "按天 JSONL 未生成"
    assert '"event": "hello"' in current[0].read_text(encoding="utf-8")
    assert not old.exists(), "超出保留天数的日志应被清理"


# ---------------------------------------------------------------- T-7.1 / 7.2 / 7.3 镜像与编排


def _load_compose() -> dict[str, Any]:
    return yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))


def test_dockerfile_runs_non_root_and_limits_threads() -> None:
    text = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG ROS_DISTRO" in text
    assert "useradd -u 1000" in text
    assert "\nUSER robotchat" in text
    assert "OMP_NUM_THREADS" in text
    assert "HEALTHCHECK" in text
    assert "rm -rf /root/.cache" in text


def test_compose_dds_security_and_limits() -> None:
    compose = _load_compose()
    service = compose["services"]["robotchat"]
    env = service["environment"]
    assert service["network_mode"] == "host"
    assert service["ipc"] == "host"
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["restart"] == "unless-stopped"
    assert service["stop_grace_period"] == "20s"
    assert any(volume.endswith(":ro") for volume in service["volumes"])
    assert env["RMW_IMPLEMENTATION"] == "${RMW_IMPLEMENTATION:-rmw_cyclonedds_cpp}"
    assert env["CYCLONEDDS_URI"].startswith("file://")
    assert env["OMP_NUM_THREADS"] == "${OMP_NUM_THREADS:-2}"
    assert service["deploy"]["resources"]["limits"]["memory"] == "800M"
    assert service["logging"]["options"]["max-file"] == "5"
    assert "api/ready" in " ".join(service["healthcheck"]["test"])


def test_model_compose_binds_loopback_and_isolates_llm() -> None:
    models = yaml.safe_load((DEPLOY / "docker-compose.models.yml").read_text("utf-8"))
    for name in ("funasr", "tts"):
        assert models["services"][name]["ports"][0].startswith("127.0.0.1:")
    llm = models["services"]["llm"]
    assert llm["profiles"] == ["topology2"]
    assert llm["ports"][0].startswith("127.0.0.1:")
    assert llm["deploy"]["resources"]["limits"]["memory"] == "${LLM_MEMORY:-8G}"


def test_deploy_scripts_and_systemd_unit_exist() -> None:
    for name in (
        "install.sh",
        "selfcheck.sh",
        "backup.sh",
        "restore.sh",
        "upgrade.sh",
        "rollback.sh",
        "common.sh",
    ):
        assert (DEPLOY / "scripts" / name).is_file()
    unit = (DEPLOY / "systemd" / "robotchat.service").read_text(encoding="utf-8")
    assert "Requires=docker.service" in unit
    assert "docker compose up -d" in unit


# ---------------------------------------------------------------- T-7.13 诊断包


def test_export_diagnose_packs_sanitized_artifacts(tmp_path: Path) -> None:
    payloads = {
        "/api/config": {"api": {"auth_token_env": "ROBOTCHAT_API_TOKEN"}},
        "/api/metrics": {"stages": {}},
        "/api/health": {"status": "ok"},
        "/api/logs/download": "{\"event\":\"hello\"}\n",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/diagnostics/selfcheck":
            return httpx.Response(200, json={"overall": "WARN", "items": []})
        body = payloads.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        if isinstance(body, str):
            return httpx.Response(200, text=body)
        return httpx.Response(200, json=body)

    target = tmp_path / "diag.tar.gz"
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = export_diagnose(out=target, token="t", client=http)

    assert result["ok"] is True
    assert result["path"] == str(target)
    with tarfile.open(target, "r:gz") as archive:
        names = set(archive.getnames())
        assert {"config.json", "metrics.json", "health.json", "selfcheck.json"} <= names
        assert "logs.jsonl" in names
        manifest = json.loads(archive.extractfile("manifest.json").read().decode("utf-8"))
        assert manifest["token_supplied"] is True
        assert manifest["errors"] == []


def test_export_diagnose_keeps_package_when_endpoint_fails(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "unauthorized"})

    target = tmp_path / "diag.tar.gz"
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = export_diagnose(out=target, client=http)

    assert result["ok"] is False
    assert target.is_file()
    assert len(result["errors"]) == 5
