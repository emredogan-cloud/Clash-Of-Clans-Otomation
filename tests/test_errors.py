"""Sanity tests for the exception hierarchy."""
from __future__ import annotations

import pytest

from automation.errors import (
    ADBError,
    AutomationError,
    BootstrapError,
    DeviceNotFoundError,
    USBValidationError,
)


def test_hierarchy() -> None:
    assert issubclass(BootstrapError, AutomationError)
    assert issubclass(ADBError, AutomationError)
    assert issubclass(DeviceNotFoundError, ADBError)
    assert issubclass(USBValidationError, BootstrapError)


def test_can_raise_and_catch_at_base() -> None:
    with pytest.raises(AutomationError):
        raise USBValidationError("link too slow")
    with pytest.raises(BootstrapError):
        raise USBValidationError("link too slow")
    with pytest.raises(ADBError):
        raise DeviceNotFoundError("no device")
