"""Typed exception hierarchy for the framework.

Kept intentionally small in Phase 1. Subsystem-specific subclasses will
be added in later phases (e.g. `MatchError`, `ActionError`,
`StateMachineError`) where they belong.
"""
from __future__ import annotations


class AutomationError(Exception):
    """Base class for all framework exceptions."""


class BootstrapError(AutomationError):
    """A precondition for running the framework failed at startup."""


class ADBError(AutomationError):
    """An ADB command failed, timed out, or returned unexpected output."""


class DeviceNotFoundError(ADBError):
    """No connected device, or the connected device is not in state `device`.

    Raised when `adb get-state` returns anything other than `device` and
    when downstream code requires a live device. The bootstrap surfaces
    this with operator-facing remediation instructions.
    """


class USBValidationError(BootstrapError):
    """The device's USB link speed is below the configured minimum.

    Frozen at ≥ 480 Mbps for v1.0 (USB 2.0 high-speed or USB 3.x). A
    typical cause is the device being plugged through a USB 1.1 hub
    (keyboards, monitors, dock-style adapters) which silently
    downgrades the link to 12 Mbps. See `docs/frozen_nfrs_v1.md` §5.
    """
