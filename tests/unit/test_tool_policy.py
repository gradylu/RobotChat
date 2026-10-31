"""工具安全策略单测（T-5.10、C-84 / C-85）。"""

from __future__ import annotations

from robotchat.tools.policy import (
    CANCEL_ANSWER,
    CONFIRM_ANSWER,
    ToolPolicy,
    validate_parameters,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "location": {"type": "string", "minLength": 1},
        "speed": {"type": "number", "minimum": 0.1, "maximum": 1.0},
        "level": {"type": "string", "enum": ["a", "b"]},
        "channel": {"type": "integer", "minimum": 0, "maximum": 7},
    },
    "required": ["location"],
}


def test_parameter_validation_rejects_out_of_range_and_unknown() -> None:
    assert validate_parameters(SCHEMA, {"location": "A 点"}) == []
    errors = validate_parameters(
        SCHEMA,
        {"location": "", "speed": 2.0, "level": "c", "channel": 9, "extra": 1},
    )
    assert any("location" in item for item in errors)
    assert any("speed" in item for item in errors)
    assert any("level" in item for item in errors)
    assert any("channel" in item for item in errors)
    assert any("extra" in item for item in errors)


def test_integer_rejects_bool() -> None:
    errors = validate_parameters(SCHEMA, {"location": "A 点", "channel": True})
    assert errors


def test_blacklist_whitelist_and_risk_confirmation() -> None:
    policy = ToolPolicy(whitelist=["navigate_to"], blacklist=["set_io"])
    assert not policy.check("set_io", {}).allowed
    assert not policy.check("speak", {}).allowed
    decision = policy.check(
        "navigate_to", {"location": "A 点"}, risk="high", parameters=SCHEMA
    )
    assert decision.allowed
    assert decision.needs_confirm
    assert policy.denied == 2


def test_rate_limit_sliding_window() -> None:
    clock = {"value": 0.0}
    policy = ToolPolicy(rate_limit_per_minute=2, clock=lambda: clock["value"])
    args = {"location": "A 点"}
    assert policy.check("navigate_to", args, parameters=SCHEMA).allowed
    assert policy.check("navigate_to", args, parameters=SCHEMA).allowed
    assert not policy.check("navigate_to", args, parameters=SCHEMA).allowed
    clock["value"] = 61.0
    assert policy.check("navigate_to", args, parameters=SCHEMA).allowed


def test_confirmation_words_cancel_takes_priority() -> None:
    policy = ToolPolicy()
    assert policy.decide_confirmation("确认执行") == CONFIRM_ANSWER
    assert policy.decide_confirmation("好的，开始吧") == CONFIRM_ANSWER
    # 同时出现确认与否定词时按取消处理，避免歧义下误执行
    assert policy.decide_confirmation("确认？还是算了") == CANCEL_ANSWER
    assert policy.decide_confirmation("不要") == CANCEL_ANSWER
    assert policy.decide_confirmation("随便") == ""
    assert policy.decide_confirmation("") == ""


def test_snapshot_shape() -> None:
    policy = ToolPolicy(whitelist=["speak"], blacklist=["set_io"], confirm_timeout_ms=8000)
    snapshot = policy.snapshot()
    assert snapshot["whitelist"] == ["speak"]
    assert snapshot["blacklist"] == ["set_io"]
    assert snapshot["confirm_timeout_ms"] == 8000
    assert "denied" in snapshot
