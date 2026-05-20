"""The `MatchResult` container — the single shape carried out of THINK.

Immutable record produced by `Matcher.match(frame, template)`. Carries
the matched location (top-left in the frame), the matched template's
dimensions, confidence, and both the upstream capture latency (copied
from the originating `Frame`) and the THINK-only match latency.

Coordinates follow the explicit Phase 3 convention: `(x, y)` is the
TOP-LEFT corner of the matched template within the frame, NOT the
centre. The `center()` helper returns the convenience midpoint.

Confidence semantics (per ADR-03):
- `cv2.matchTemplate(TM_CCOEFF_NORMED)` raw output is in `[-1, 1]`.
- We clamp negative values to `0.0` for the `confidence` field so the
  reported range is `[0, 1]` (matches frozen MatchResult NFR).
- A negative raw correlation is always "not found"; clamping does not
  change the found/not-found verdict because any threshold is in
  `(0, 1]` and `0 < threshold` means a clamped 0 will never pass.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

_ALLOWED_SEARCH_MODES: frozenset[str] = frozenset({"roi_gray", "full_gray"})


@dataclass(frozen=True)
class MatchResult:
    """The outcome of one `Matcher.match` invocation.

    Coordinate / dimension fields are `None` iff `found is False`. When
    `found` is True they are non-negative integers (top-left coordinate
    in the reference-resolution frame; width/height equal the
    template's dimensions).

    `capture_latency_ms` is copied from the originating `Frame` so a
    consumer with only a `MatchResult` in hand can still account for
    the upstream SENSE cost. `match_latency_ms` is the THINK-only cost
    measured by the matcher (matchTemplate + peak find + bookkeeping).
    """

    found: bool
    confidence: float
    template_name: str
    search_mode: str
    capture_latency_ms: float
    match_latency_ms: float
    x: int | None = None
    y: int | None = None
    width: int | None = None
    height: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.found, bool):
            raise TypeError(f"MatchResult.found must be bool, got {type(self.found).__name__}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"MatchResult.confidence must be in [0, 1], got {self.confidence}"
            )
        if self.search_mode not in _ALLOWED_SEARCH_MODES:
            raise ValueError(
                f"MatchResult.search_mode must be one of {sorted(_ALLOWED_SEARCH_MODES)}, "
                f"got {self.search_mode!r}"
            )
        if self.capture_latency_ms < 0:
            raise ValueError(
                f"capture_latency_ms must be >= 0, got {self.capture_latency_ms}"
            )
        if self.match_latency_ms < 0:
            raise ValueError(
                f"match_latency_ms must be >= 0, got {self.match_latency_ms}"
            )
        if not self.template_name:
            raise ValueError("template_name must be non-empty")

        coords = (self.x, self.y, self.width, self.height)
        any_set = any(c is not None for c in coords)
        all_set = all(c is not None for c in coords)
        if any_set and not all_set:
            raise ValueError(
                f"x/y/width/height must be all-None or all-int, got {coords}"
            )
        if self.found and not all_set:
            raise ValueError("found=True requires x, y, width, height to be set")
        if all_set:
            # Type-narrow for the linter; runtime asserts are not strictly
            # needed but document the invariant.
            assert self.x is not None and self.y is not None
            assert self.width is not None and self.height is not None
            if self.x < 0 or self.y < 0:
                raise ValueError(f"x/y must be >= 0, got ({self.x}, {self.y})")
            if self.width <= 0 or self.height <= 0:
                raise ValueError(
                    f"width/height must be > 0, got ({self.width}, {self.height})"
                )

    # ------------------------------------------------------------------

    def center(self) -> tuple[int, int] | None:
        """Return the centre of the matched template, or `None` if not found.

        Integer arithmetic (`//`) is used; sub-pixel resolution is not
        needed in the v1.0 input pipeline (ADR-09: device pixels are
        integers at the edge).
        """
        if not self.found:
            return None
        # Validated in __post_init__: when found=True these are all int.
        assert self.x is not None and self.y is not None
        assert self.width is not None and self.height is not None
        return (self.x + self.width // 2, self.y + self.height // 2)

    def to_debug_dict(self) -> Mapping[str, Any]:
        """Serialisable summary suitable for `metadata.json` artifacts."""
        return {
            "found": self.found,
            "confidence": float(self.confidence),
            "template_name": self.template_name,
            "search_mode": self.search_mode,
            "capture_latency_ms": float(self.capture_latency_ms),
            "match_latency_ms": float(self.match_latency_ms),
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "center": list(self.center()) if self.found else None,
        }

    def summary(self) -> str:
        """One-line representation for human inspection / logs."""
        if not self.found:
            return (
                f"MatchResult(MISS template={self.template_name!r} "
                f"confidence={self.confidence:.3f} mode={self.search_mode} "
                f"match={self.match_latency_ms:.2f} ms)"
            )
        return (
            f"MatchResult(HIT template={self.template_name!r} "
            f"confidence={self.confidence:.3f} at ({self.x},{self.y}) "
            f"size={self.width}x{self.height} mode={self.search_mode} "
            f"match={self.match_latency_ms:.2f} ms)"
        )
