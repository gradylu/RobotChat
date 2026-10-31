"""T-3.9 `cli session reset` 单测：HTTP 调用与 Token 透传。"""

from __future__ import annotations

import json

import httpx

from robotchat.cli import _build_parser, reset_session


def test_parser_accepts_session_reset() -> None:
    args = _build_parser().parse_args(["session", "reset", "--session", "s1"])
    assert args.group == "session"
    assert args.command == "reset"
    assert args.session == "s1"


def test_reset_session_posts_to_api() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"reset": True, "state": "IDLE"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = reset_session(
        url="http://127.0.0.1:8080", session_id="default", token="secret", client=client
    )
    client.close()

    assert result == {"reset": True, "state": "IDLE"}
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/session/default/reset"
    assert captured["auth"] == "Bearer secret"


def test_main_session_branch_prints_json(monkeypatch, capsys) -> None:
    from robotchat import cli

    def fake_reset(**kwargs):
        assert kwargs["session_id"] == "default"
        return {"reset": True, "state": "IDLE"}

    monkeypatch.setattr(cli, "reset_session", fake_reset)
    assert cli.main(["session", "reset"]) == 0
    assert json.loads(capsys.readouterr().out)["reset"] is True
