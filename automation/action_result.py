"""The `ActionResult` container — the single shape carried out of ACT.

Immutable record produced by every public method on `Actuator`. Carries
the actuation outcome (`success`), the action class, the wall-clock
latency for the ADB invocation, the device-pixel coordinates that were
actually sent to the device, and the UTC timestamp at which the result
was produced.

Coordinate convention (per ADR-09 / ADR-04):
- `device_x`, `device_y` are integer device pixels — the values
  passed to `adb shell input` *after* reference-to-native
  denormalization (and any jitter sampling) was applied.
- For a `swipe` they are the *start* coordinate of the gesture; the
  full start/end pair is reported in the artifact metadata. The
  per-result `device_x` / `device_y` are kept simple here so that the
  container is uniform across action classes; richer per-action shape
  belongs in Phase 5+.
- They are `None` only when an action class structurally has no
  coordinate (none exist in Phase 4 — `tap`, `swipe`, `long_press`
  all carry a primary anchor; the nullable typing is preserved so a
  future `key` / `text` action can reuse the container without
  re-engineering the validation).

`ActionResult` is a *container only*. No ADB logic, no I/O, no
side effects. Construction validates field invariants and freezes the
record (frozen dataclass).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Mapping

_ALLOWED_ACTION_TYPES: frozenset[str] = frozenset({"tap", "swipe", "long_press"})


@dataclass(frozen=True)
class ActionResult:
    """The outcome of one actuator invocation.

    Field semantics:

    - `success`       : True iff the ADB invocation exited cleanly. A
                        coordinate-validation failure raises
                        `CoordinateError` before construction, so a
                        `success=False` result reflects an actual ADB
                        execution fault, not a malformed request.
    - `action_type`   : one of `"tap"`, `"swipe"`, `"long_press"`.
    - `latency_ms`    : wall-clock time (ms) covering the ADB shell
                        invocation only — measured with
                        `time.perf_counter_ns()` around the
                        `adb shell input ...` call. Excludes
                        denormalization and artifact-write costs.
    - `device_x`      : integer device-pixel x of the primary anchor
                        (start coordinate for swipe / long_press;
                        anchor for tap). May be `None` for action
                        classes without a coordinate (future).
    - `device_y`      : integer device-pixel y of the primary anchor.
    - `ts`            : UTC instant at which the result was produced
                        (timezone-aware datetime).
    """

    success: bool
    action_type: str
    latency_ms: float
    device_x: int | None
    device_y: int | None
    ts: _dt.datetime

    def __post_init__(self) -> None:
        if not isinstance(self.success, bool):
            raise TypeError(
                f"ActionResult.success must be bool, got {type(self.success).__name__}"
            )
        if self.action_type not in _ALLOWED_ACTION_TYPES:
            raise ValueError(
                f"ActionResult.action_type must be one of {sorted(_ALLOWED_ACTION_TYPES)}, "
                f"got {self.action_type!r}"
            )
        if not isinstance(self.latency_ms, (int, float)):
            raise TypeError(
                f"ActionResult.latency_ms must be a number, got "
                f"{type(self.latency_ms).__name__}"
            )
        if self.latency_ms < 0:
            raise ValueError(
                f"ActionResult.latency_ms must be >= 0, got {self.latency_ms}"
            )
        coords = (self.device_x, self.device_y)
        any_set = any(c is not None for c in coords)
        all_set = all(c is not None for c in coords)
        if any_set and not all_set:
            raise ValueError(
                f"device_x/device_y must be both-None or both-int, got {coords}"
            )
        if all_set:
            assert self.device_x is not None and self.device_y is not None
            if not isinstance(self.device_x, int) or isinstance(self.device_x, bool):
                raise TypeError(
                    f"device_x must be int, got {type(self.device_x).__name__}"
                )
            if not isinstance(self.device_y, int) or isinstance(self.device_y, bool):
                raise TypeError(
                    f"device_y must be int, got {type(self.device_y).__name__}"
                )
            if self.device_x < 0 or self.device_y < 0:
                raise ValueError(
                    f"device_x/device_y must be >= 0, got "
                    f"({self.device_x}, {self.device_y})"
                )
        # All Phase-4 action types carry an anchor; structurally enforce.
        if self.action_type in _ALLOWED_ACTION_TYPES and not all_set:
            raise ValueError(
                f"action_type {self.action_type!r} requires device_x and device_y"
            )
        if not isinstance(self.ts, _dt.datetime):
            raise TypeError(
                f"ActionResult.ts must be datetime, got {type(self.ts).__name__}"
            )
        if self.ts.tzinfo is None:
            raise ValueError("ActionResult.ts must be timezone-aware (UTC)")

    # ------------------------------------------------------------------

    def to_debug_dict(self) -> Mapping[str, Any]:
        """JSON-serialisable summary suitable for `metadata.json` artifacts."""
        return {
            "success": self.success,
            "action_type": self.action_type,
            "latency_ms": float(self.latency_ms),
            "device_x": self.device_x,
            "device_y": self.device_y,
            "ts": self.ts.isoformat(),
        }

    def summary(self) -> str:
        """One-line human-readable summary."""
        status = "OK" if self.success else "FAIL"
        return (
            f"ActionResult({status} {self.action_type} "
            f"at ({self.device_x},{self.device_y}) "
            f"latency={self.latency_ms:.2f} ms)"
        )
