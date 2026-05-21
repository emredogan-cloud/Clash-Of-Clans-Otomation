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


# ---------------------------------------------------------------------------
# SENSE layer (Phase 2)
# ---------------------------------------------------------------------------


class SensorError(AutomationError):
    """Base class for all errors raised inside the SENSE pipeline."""


class CaptureError(SensorError):
    """An ADB capture invocation failed or returned no usable bytes.

    Raised when `adb exec-out screencap` exits non-zero, times out,
    returns an empty buffer, or when `pull` cannot retrieve the file.
    Distinct from `FrameDecodeError` so callers can distinguish
    transport faults from payload-format faults.
    """


class FrameDecodeError(SensorError):
    """Failure decoding a screencap payload into a NumPy ndarray.

    Raised when `cv2.imdecode` returns `None` on the PNG path, or when
    the raw payload's declared dimensions disagree with its byte
    length, or when the RGBA→BGR conversion fails. Header layout
    issues that are recoverable by switching modes are raised here too
    so the auto-mode fallback can catch them at one point.
    """


class UnsupportedPixelFormatError(FrameDecodeError):
    """The raw screencap declares a pixel format we do not handle.

    v1.0 supports only `PIXEL_FORMAT_RGBA_8888` (value `1`) per ADR-02.
    Other values (`RGBX_8888`, `RGB_888`, `RGB_565`, `BGRA_8888`,
    `YV12`, etc.) raise this exception. Callers can catch this to
    fall back to the PNG path.
    """


# ---------------------------------------------------------------------------
# THINK layer (Phase 3)
# ---------------------------------------------------------------------------


class MatcherError(AutomationError):
    """Base class for errors raised inside the THINK pipeline."""


class InvalidROIError(MatcherError):
    """A template's ROI is malformed or out of bounds.

    Raised when:
    - The ROI tuple is not 4 integers.
    - Any coordinate is negative.
    - `x1 >= x2` or `y1 >= y2`.
    - The ROI extends beyond the frame supplied to `Matcher.match`.
    """


class MatchComputationError(MatcherError):
    """`cv2.matchTemplate` could not compute a result.

    The most common cause is a template larger than the search area
    (full frame or ROI). Per `cv2.matchTemplate` semantics, the
    template must be no larger than the image in both dimensions.
    """


# ---------------------------------------------------------------------------
# ACT layer (Phase 4)
# ---------------------------------------------------------------------------


class ActuatorError(AutomationError):
    """Base class for errors raised inside the ACT pipeline."""


class CoordinateError(ActuatorError):
    """A coordinate, dimension, or duration argument was malformed.

    Raised before any ADB invocation. Covers:
    - Non-finite or non-numeric coordinates.
    - Coordinates outside the reference frame (`0 ≤ x < ref_w`, etc.).
    - Non-positive native target dimensions.
    - Non-positive durations on `swipe` / `long_press`.
    - Denormalized coordinates that fall outside the device's native
      screen (a defensive guard; should not occur if inputs validate).
    """


class ActionExecutionError(ActuatorError):
    """The underlying ADB command failed at issuance time.

    Wraps the lower-level `ADBError` so callers (and the future
    orchestrator) can branch on ACT-layer faults without importing the
    ADB layer's exception hierarchy directly. Coordinate-level
    validation does not raise this; it raises `CoordinateError` first.
    """


# ---------------------------------------------------------------------------
# Orchestrator (Phase 5)
# ---------------------------------------------------------------------------


class OrchestratorError(AutomationError):
    """Base class for errors raised inside the orchestrator / FSM."""


class InvalidTransitionError(OrchestratorError):
    """An FSM transition was attempted that is not allowed.

    Raised when the orchestrator is asked to move from a state to one
    not in its allowed-transitions table (e.g. `tick()` invoked while
    in `FAILED`, or `reset()` invoked while in a non-`FAILED` state).
    The FSM is fully explicit; there are no hidden recovery paths in
    Phase 5.
    """


class ValidationError(OrchestratorError):
    """A post-action validation cycle exhausted its single retry budget.

    After ACTING completes, the orchestrator re-captures and re-matches.
    If the template is still present (i.e. the action did not achieve
    the expected state change), one extra capture+match is attempted.
    If that also fails, the orchestrator transitions to `FAILED` and a
    `ValidationError` describes the situation in logs / artifacts.

    The exception is exposed as a typed contract for Phase 6+
    observability; the Phase 5 `tick()` does NOT raise it — failures
    surface via `TickResult.success=False`. Future phases that want
    raising semantics can opt in.
    """
