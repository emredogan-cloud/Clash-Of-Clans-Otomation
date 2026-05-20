"""The `Template` container — single immutable record consumed by the matcher.

Phase 3 keeps the template surface deliberately small:

- one grayscale uint8 ndarray (per ADR-03 / ADR-05; full-frame BGR
  matching is forbidden on the hot path by the frozen NFRs);
- a threshold in `(0, 1]`;
- an optional ROI tuple `(x1, y1, x2, y2)` in reference-frame pixels.

Out of scope (deferred to later phases or backlog):

- masks (ADR-03 mask support is a Phase 3+ refinement; the frozen
  Phase 3 prompt scopes only to plain templates);
- multi-scale fallback (ADR-03 `multi_scale` flag — backlog);
- on-disk loading and manifest hashing (ADR-10 — Phase 3+ work item
  not covered by the current prompt).

This file is pure data. No file I/O, no disk paths, no OpenCV calls.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import InvalidROIError


@dataclass(frozen=True)
class Template:
    """A grayscale template used to locate a UI element on a frame.

    Field semantics:

    - `name`        : human-readable template id. Used in logs, debug
      artifacts, and `MatchResult.template_name`. Must be non-empty.
    - `image_gray`  : NumPy uint8 2D ndarray, shape `(H, W)`. The
      matcher will not accept BGR templates; Phase 3 is grayscale-only
      per the frozen NFR for the hot path.
    - `width`       : the second axis of `image_gray`. Must match.
    - `height`      : the first axis of `image_gray`. Must match.
    - `threshold`   : minimum normalized correlation in `(0, 1]` for a
      `HIT`. The matcher returns `found=False` for any peak below this.
    - `roi`         : optional `(x1, y1, x2, y2)` tuple in
      reference-frame pixels. Half-open: x in [x1, x2), y in [y1, y2).
      When set, the matcher restricts search to this rectangle. When
      `None`, the matcher searches the full frame.

    Hashing is disabled because `image_gray` is an unhashable ndarray;
    use `name` as the cache key in any external store.
    """

    name: str
    image_gray: np.ndarray
    width: int
    height: int
    threshold: float
    roi: tuple[int, int, int, int] | None = None

    # The ndarray field makes the auto-generated __hash__ crash, so
    # disable hashing explicitly (frozen=True wants __hash__).
    __hash__ = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Template.name must be non-empty")
        if not isinstance(self.image_gray, np.ndarray):
            raise TypeError(
                f"Template.image_gray must be numpy.ndarray, "
                f"got {type(self.image_gray).__name__}"
            )
        if self.image_gray.dtype != np.uint8:
            raise ValueError(
                f"Template.image_gray must be uint8, got dtype {self.image_gray.dtype}"
            )
        if self.image_gray.ndim != 2:
            raise ValueError(
                f"Template.image_gray must be 2D grayscale, got shape {self.image_gray.shape}"
            )
        if self.image_gray.size == 0:
            raise ValueError("Template.image_gray is empty")
        if self.height != self.image_gray.shape[0] or self.width != self.image_gray.shape[1]:
            raise ValueError(
                f"Template width/height ({self.width}x{self.height}) does not match "
                f"image_gray shape (H={self.image_gray.shape[0]}, W={self.image_gray.shape[1]})"
            )
        if not 0.0 < self.threshold <= 1.0:
            raise ValueError(
                f"Template.threshold must be in (0, 1], got {self.threshold}"
            )

        if self.roi is not None:
            self._validate_roi_tuple_structure(self.roi)

        try:
            self.image_gray.setflags(write=False)
        except ValueError:
            # View into a non-writeable base — acceptable.
            pass

    @staticmethod
    def _validate_roi_tuple_structure(roi: tuple[int, int, int, int]) -> None:
        if not isinstance(roi, tuple) or len(roi) != 4:
            raise InvalidROIError(
                f"Template.roi must be a 4-tuple (x1, y1, x2, y2), got {roi!r}"
            )
        if not all(isinstance(c, int) for c in roi):
            raise InvalidROIError(
                f"Template.roi components must all be int, got {roi!r}"
            )
        x1, y1, x2, y2 = roi
        if x1 < 0 or y1 < 0:
            raise InvalidROIError(
                f"Template.roi top-left must be >= (0, 0), got ({x1}, {y1})"
            )
        if x2 <= x1 or y2 <= y1:
            raise InvalidROIError(
                f"Template.roi must have x1 < x2 and y1 < y2, got {roi!r}"
            )

    # ------------------------------------------------------------------

    def validate_roi(
        self,
        frame_width: int | None = None,
        frame_height: int | None = None,
    ) -> None:
        """Re-validate the ROI, optionally against a frame's dimensions.

        Always validates the ROI tuple's internal shape (same checks
        `__post_init__` performs). If `frame_width` / `frame_height`
        are supplied, also verifies the ROI is fully contained in the
        frame and that the template fits inside the ROI.

        Raises `InvalidROIError` on any failure. A no-op when
        `self.roi is None`.
        """
        if self.roi is None:
            return
        self._validate_roi_tuple_structure(self.roi)
        x1, y1, x2, y2 = self.roi
        if frame_width is not None and frame_height is not None:
            if x2 > frame_width or y2 > frame_height:
                raise InvalidROIError(
                    f"Template.roi {self.roi} extends beyond frame "
                    f"({frame_width}x{frame_height})"
                )
            if (x2 - x1) < self.width or (y2 - y1) < self.height:
                raise InvalidROIError(
                    f"Template ({self.width}x{self.height}) does not fit "
                    f"inside ROI {self.roi} of size {(x2 - x1)}x{(y2 - y1)}"
                )

    def shape_summary(self) -> str:
        """One-line representation."""
        roi_part = "no-roi" if self.roi is None else f"roi={self.roi}"
        return (
            f"Template({self.name!r} {self.width}x{self.height} gray "
            f"threshold={self.threshold:.3f} {roi_part})"
        )
