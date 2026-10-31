"""Phase 6 接入层契约测试（T-6.1 / T-6.2 / T-6.3 / T-6.4）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from robotchat.config.loader import ConfigError, load_settings
from robotchat.main import create_app

REPO_CONFIG = Path(__file__).resolve().parents[2] / "config"


def _offline_env() -> dict[str, str]:
    """单测不触网：关闭公网离线探测并清空云端 fallback。"""
    return {
        "ROBOTCHAT__RESILIENCE__OFFLINE_PROBE_ON_START": "false",
        "ROBOTCHAT__PROVIDERS__ASR__FALLBACK": "",
        "ROBOTCHAT__PROVIDERS__LLM__FALLBACK": "",
        "ROBOTCHAT__PROVIDERS__TTS__FALLBACK": "",
    }


def _client(tmp_path: Path) -> TestClient:
    settings = load_settings(config_dir=REPO_CONFIG, data_dir=tmp_path)
    return TestClient(create_app(settings))


def _apply_offline_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """配置热更新会重新读取进程环境，测试必须与 os.environ 保持一致。"""
    for key, value in _offline_env().items():
        monkeypatch.setenv(key, value)
    # 运行时目录由 resolve_paths 从环境变量读取：隔离到 tmp_path，避免污染仓库 var/
    monkeypatch.setenv("ROBOTCHAT_CONFIG_DIR", str(REPO_CONFIG))
    monkeypatch.setenv("ROBOTCHAT_DATA_DIR", str(tmp_path))


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    _apply_offline_env(monkeypatch, tmp_path)
    with _client(tmp_path) as test_client:
        yield test_client


@pytest.fixture()
def auth_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    _apply_offline_env(monkeypatch, tmp_path)
    monkeypatch.setenv("ROBOTCHAT_API_TOKEN", "test-token-123")
    with _client(tmp_path) as test_client:
        yield test_client


def _receive_until(ws: Any, kind: str, limit: int = 8) -> dict[str, Any]:
    for _ in range(limit):
        message = ws.receive_json()
        if message["type"] == kind:
            return message
    raise AssertionError(f"未在 {limit} 条消息内收到 {kind}")


def _command(payload: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"v": 1, "type": payload["type"], "ts": 0, "turn_id": None, "payload": payload, **extra}


# ---------------------------------------------------------------- T-6.1 WS 网关


def test_ws_sends_snapshot_and_accepts_commands(client: TestClient) -> None:
    with client.websocket_connect("/ws/session/default") as ws:
        snapshot = ws.receive_json()
        assert snapshot["v"] == 1
        assert snapshot["type"] == "session.snapshot"
        assert snapshot["payload"]["protocol_version"] == 1
        assert "state" in snapshot["payload"]

        ws.send_json(_command({"type": "language.set", "mode": "fixed", "lang": "en"}))
        ack = _receive_until(ws, "command.ack")
        assert ack["payload"]["accepted"] is True
        assert ack["payload"]["language"]["current"] == "en"

        ws.send_json(_command({"type": "mic.mute"}))
        ack = _receive_until(ws, "command.ack")
        assert ack["payload"]["accepted"] is True
        ws.send_json(_command({"type": "mic.unmute"}))
        _receive_until(ws, "command.ack")

        ws.send_json(_command({"type": "language.set", "mode": "auto"}))


def test_ws_rejects_bad_protocol_and_unknown_command(client: TestClient) -> None:
    with client.websocket_connect("/ws/session/default") as ws:
        ws.receive_json()
        ws.send_json({"v": 2, "type": "session.start", "ts": 0, "turn_id": None, "payload": {}})
        error = _receive_until(ws, "error")
        assert error["payload"]["code"] == "protocol_mismatch"

        ws.send_json({"v": 1, "type": "evil.command", "ts": 0, "turn_id": None, "payload": {}})
        error = _receive_until(ws, "error")
        assert error["payload"]["code"] == "unknown_command"

        ws.send_json({"v": 1, "turn_id": None, "payload": {}})
        error = _receive_until(ws, "error")
        assert error["payload"]["code"] == "bad_message"


# ---------------------------------------------------------------- T-6.3 配置热更新


def test_config_hot_update_and_rollback(client: TestClient) -> None:
    first = client.put("/api/config", json={"barge_in": {"guard_ms": 600}})
    assert first.status_code == 200
    body = first.json()
    assert body["updated"] is True
    assert "barge_in" in body["applied"]
    assert body["patch"] == {"barge_in": {"guard_ms": 600}}
    assert client.get("/api/barge-in").json()["guard_ms"] == 600

    # 第二次变更才会产生上一版本，回滚点由此建立
    second = client.put("/api/config", json={"barge_in": {"guard_ms": 500}})
    assert second.status_code == 200
    assert client.get("/api/barge-in").json()["guard_ms"] == 500

    rollback = client.post("/api/config/rollback")
    assert rollback.status_code == 200
    assert rollback.json()["rolled_back"] is True
    assert client.get("/api/barge-in").json()["guard_ms"] == 600


def test_config_rejects_invalid_patch(client: TestClient) -> None:
    response = client.put("/api/config", json={"barge_in": {"confirm_frames": 0}})
    assert response.status_code == 400
    body = response.json()
    assert body["updated"] is False
    assert body["errors"]


def test_config_requires_restart_for_non_hot_sections(client: TestClient) -> None:
    response = client.put("/api/config", json={"observability": {"log_level": "DEBUG"}})
    assert response.status_code == 200
    body = response.json()
    assert body["restart_required"] == ["observability"]
    client.post("/api/config/rollback")


# ---------------------------------------------------------------- T-6.2 REST 全集


def test_provider_switch_and_probe(client: TestClient) -> None:
    unknown = client.post("/api/providers/asr/switch", json={"driver": "nope"})
    assert unknown.status_code == 400

    switched = client.post("/api/providers/asr/switch", json={"driver": "dashscope_stream"})
    assert switched.status_code == 200
    assert switched.json()["switched"] is True
    assert client.get("/api/providers").json()["drivers"]["asr"]["active"] == "dashscope_stream"

    back = client.post("/api/providers/asr/switch", json={"driver": "funasr_local"})
    assert back.status_code == 200

    probe = client.post("/api/providers/asr/funasr_local/test")
    assert probe.status_code == 200
    assert {"ok", "kind", "driver", "latency_ms"} <= set(probe.json())


def test_sessions_crud(client: TestClient) -> None:
    created = client.post("/api/sessions", json={"session_id": "pytest-session"})
    assert created.status_code == 200
    assert created.json()["created"] is True

    listing = client.get("/api/sessions").json()
    assert any(item["id"] == "pytest-session" for item in listing["sessions"])

    detail = client.get("/api/sessions/pytest-session")
    assert detail.status_code == 200
    assert detail.json()["session"]["id"] == "pytest-session"

    assert client.get("/api/sessions/pytest-session/turns").json()["count"] == 0
    assert client.get("/api/sessions/pytest-session/messages").json()["count"] == 0

    removed = client.delete("/api/sessions/pytest-session")
    assert removed.json()["deleted"] is True
    assert client.get("/api/sessions/pytest-session").status_code == 404


def test_session_id_validation(client: TestClient) -> None:
    response = client.post("/api/sessions", json={"session_id": "bad id!"})
    assert response.status_code == 400


def test_diagnostics_selfcheck(client: TestClient) -> None:
    response = client.post("/api/diagnostics/selfcheck")
    assert response.status_code == 200
    body = response.json()
    assert body["overall"] in {"PASS", "WARN", "FAIL"}
    assert set(body["counts"]) == {"PASS", "WARN", "FAIL"}
    for item in body["items"]:
        assert {"name", "status", "detail"} <= set(item)


def test_logs_download(client: TestClient) -> None:
    response = client.get("/api/logs/download")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-ndjson"
    assert "attachment" in response.headers["content-disposition"]


# ---------------------------------------------------------------- T-6.4 口令鉴权


def test_auth_blocks_write_requests(auth_client: TestClient) -> None:
    status = auth_client.get("/api/auth/status").json()
    assert status["enabled"] is True

    assert auth_client.post("/api/session/default/reset").status_code == 401

    ok = auth_client.post(
        "/api/session/default/reset", headers={"X-API-Token": "test-token-123"}
    )
    assert ok.status_code == 200

    # 成功校验清零失败计数，随后连续 5 次错误触发锁定
    for _ in range(5):
        assert (
            auth_client.post(
                "/api/session/default/reset", headers={"X-API-Token": "wrong-token"}
            ).status_code
            == 401
        )
    locked = auth_client.post(
        "/api/session/default/reset", headers={"X-API-Token": "wrong-token"}
    )
    assert locked.status_code == 423


def test_auth_allows_ws_with_token(auth_client: TestClient) -> None:
    with auth_client.websocket_connect("/ws/session/default?token=test-token-123") as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "session.snapshot"


def test_short_token_refuses_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _apply_offline_env(monkeypatch, tmp_path)
    monkeypatch.setenv("ROBOTCHAT_API_TOKEN", "short")
    with pytest.raises(ConfigError), _client(tmp_path):
        pass
