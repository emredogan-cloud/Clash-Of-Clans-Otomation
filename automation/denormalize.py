"""Reference→device coordinate denormalization (ADR-04 inverse).

THINK runs against the v1.0 reference resolution (1080×1920); ACT
issues `adb shell input` events against the device's native
resolution (e.g. 1080×2408 on the operator's Redmi Note 11R). The
denormalizer is the *single* place where reference-space coordinates
become device-pixel integers — the boundary at which the framework
stops being resolution-agnostic (ADR-09).

Scope (Phase 4):
- Pure mathematical inverse-mapping of `(x_ref, y_ref)` in the
  reference space to `(x_native, y_native)` in the device space, via
  independent per-axis scaling.
- Defensive bounds checking on inputs and outputs.

Out of scope (deferred):
- Letterboxing / pillarboxing for aspect-mismatched devices (ADR-04
  mentions this; Phase 4 ships the simple stretch — adequate for the
  operator's device, which has the same width as the reference and
  scales only the vertical axis).
- Per-device homography from anchor calibration (ADR-04 §Alternatives
  considered).

The Denormalizer holds no per-device state. The native dimensions are
passed per call so a single actuator instance can serve any device
without reconfiguration.
"""
from __future__ import annotations

import math

from .errors import CoordinateError

DEFAULT_REFERENCE_RESOLUTION: tuple[int, int] = (1080, 1920)


class Denormalizer:
    """Inverse-map reference-space coordinates to device-pixel integers.

    Stateless apart from the target reference resolution. Construct
    once, reuse for every action. `to_native` is deterministic — given
    the same `(x_ref, y_ref, native_w, native_h)`, the returned pair
    is byte-identical across processes and Python runs.
    """

    def __init__(
        self,
        reference_resolution: tuple[int, int] = DEFAULT_REFERENCE_RESOLUTION,
    ) -> None:
        rw, rh = reference_resolution
        if not isinstance(rw, int) or not isinstance(rh, int):
            raise CoordinateError(
                f"reference resolution must be integers, got "
                f"({type(rw).__name__}, {type(rh).__name__})"
            )
        if rw <= 0 or rh <= 0:
            raise CoordinateError(
                f"reference resolution must be positive, got {rw}x{rh}"
            )
        self.reference_width: int = rw
        self.reference_height: int = rh

    @property
    def reference_resolution(self) -> tuple[int, int]:
        return (self.reference_width, self.reference_height)

    # ------------------------------------------------------------------

    def to_native(
        self,
        x_ref: float,
        y_ref: float,
        native_width: int,
        native_height: int,
    ) -> tuple[int, int]:
        """Map `(x_ref, y_ref)` to integer `(x_native, y_native)`.

        Inputs:

        - `x_ref`, `y_ref`: reference-space coordinates. May be `int`
          or `float`. Floats are common because jitter sampling
          produces non-integer reference-space deltas; the result is
          rounded to the nearest integer.
        - `native_width`, `native_height`: the device's native
          dimensions in pixels. Must be positive integers (typically
          obtained from `Frame.native_width` / `Frame.native_height`).

        Returns:

        - `(x_native, y_native)`: integer device-pixel coordinates,
          `0 ≤ x_native < native_width`, `0 ≤ y_native < native_height`.

        Raises `CoordinateError` if:

        - inputs are non-finite (NaN, +inf, -inf);
        - native dimensions are non-positive;
        - reference-space coordinates are outside `[0, reference_*)`
          (half-open: `reference_*` exclusive, matching pixel-index
          semantics);
        - the mapped device coordinates would fall outside the device
          screen (a structural guard against scaling bugs — should
          never fire if inputs validate).

        The map is independent per-axis:

            x_native = round(x_ref * native_width  / reference_width)
            y_native = round(y_ref * native_height / reference_height)

        with the result clamped to the device's last valid pixel
        (`native_* - 1`) iff rounding overshoots by one (can happen at
        `x_ref == reference_width - 0.5`).
        """
        # Type / finite check.
        for label, value in (("x_ref", x_ref), ("y_ref", y_ref)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise CoordinateError(
                    f"{label} must be int or float, got {type(value).__name__}"
                )
            if not math.isfinite(float(value)):
                raise CoordinateError(
                    f"{label} must be finite, got {value!r}"
                )
        for label, value in (
            ("native_width", native_width),
            ("native_height", native_height),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise CoordinateError(
                    f"{label} must be int, got {type(value).__name__}"
                )
            if value <= 0:
                raise CoordinateError(
                    f"{label} must be positive, got {value}"
                )

        # Reference-space bounds (half-open: pixel-index semantics).
        if not (0 <= float(x_ref) < self.reference_width):
            raise CoordinateError(
                f"x_ref must be in [0, {self.reference_width}), got {x_ref}"
            )
        if not (0 <= float(y_ref) < self.reference_height):
            raise CoordinateError(
                f"y_ref must be in [0, {self.reference_height}), got {y_ref}"
            )

        # Independent per-axis scaling. `round` is banker's-rounding in
        # CPython 3.x for ties; deterministic given identical inputs.
        x_native = int(round(float(x_ref) * native_width / self.reference_width))
        y_native = int(round(float(y_ref) * native_height / self.reference_height))

        # Clamp to last-valid-pixel iff rounding nudged us past the edge.
        # This is structural: a half-open input range maps to a
        # half-open output range, but `round` can produce
        # `native_width` exactly at the upper boundary.
        if x_native == native_width:
            x_native = native_width - 1
        if y_native == native_height:
            y_native = native_height - 1

        # Defensive guard — should never fire after the clamp above.
        if not (0 <= x_native < native_width and 0 <= y_native < native_height):
            raise CoordinateError(
                f"denormalized coordinates ({x_native}, {y_native}) outside "
                f"device screen ({native_width}x{native_height}); "
                f"inputs were x_ref={x_ref}, y_ref={y_ref}, ref="
                f"{self.reference_width}x{self.reference_height}"
            )
        return x_native, y_native
