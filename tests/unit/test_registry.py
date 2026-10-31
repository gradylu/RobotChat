"""驱动注册表单测（T-0.5）。"""

from __future__ import annotations

from typing import Any

import pytest

from robotchat.providers.registry import (
    ProviderRegistry,
    UnknownDriverError,
    available_drivers,
    is_registered,
    register_provider,
)


class _Dummy:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config


@register_provider("asr", "dummy_asr")
class _DummyASR(_Dummy):
    pass


def test_create_registered_driver() -> None:
    driver = ProviderRegistry().create("asr", "dummy_asr", config={"x": 1})
    assert isinstance(driver, _DummyASR)
    assert driver.config == {"x": 1}


def test_create_active_uses_driver_config() -> None:
    driver = ProviderRegistry().create_active("asr", "dummy_asr", {"endpoint": "http://x"})
    assert driver.config == {"endpoint": "http://x"}


def test_unknown_driver_reports_available() -> None:
    assert is_registered("asr", "dummy_asr")
    with pytest.raises(UnknownDriverError) as exc_info:
        ProviderRegistry().create("asr", "missing")
    assert "dummy_asr" in str(exc_info.value)
    assert exc_info.value.available == available_drivers("asr")


def test_duplicate_registration_rejected() -> None:
    with pytest.raises(ValueError, match="重复注册"):

        @register_provider("asr", "dummy_asr")
        class _Duplicate(_Dummy):
            pass


def test_unknown_kind_rejected() -> None:
    with pytest.raises(ValueError, match="不支持的驱动类别"):
        register_provider("nope", "x")
