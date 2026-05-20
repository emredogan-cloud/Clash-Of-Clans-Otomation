"""The `Frame` container — the single shape carried out of SENSE.

`Frame` is intentionally a thin immutable record. It owns:

- the decoded BGR ndarray (write-locked at construction);
- the dimensions of that ndarray (`width`, `height`) which after
  `Remap.apply` equal the v1.0 reference resolution (1080×1920);
- the device-native dimensions that the frame originated from
  (`native_width`, `native_height`), preserved through remap so
  downstream coordinate work (Phase 4) can de-normalise back to the
  device;
- end-to-end capture latency (ns-resolution timer, exported as ms);
- the `source_mode` token the sensor used (`"raw"`, `"png"`, `"pull"`).

Frame is a *container*. It does no OpenCV work, no I/O, and no
validation beyond shape and dtype. Pipeline stages own those.

ADRs:
- ADR-02 (BGR ndarray shape).
- ADR-04 (resolution independence; native vs reference fields).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np


_ALLOWED_SOURCE_MODES: frozenset[str] = frozenset({"raw", "png", "pull"})


@dataclass(frozen=True)
class Frame:
    """An immutable captured frame at the v1.0 reference resolution.

    Use the constructor directly; do not subclass. Equality and hashing
    are dataclass-default (identity-by-fields), but the `image_bgr`
    array is compared by `id` not value — two frames carrying byte-equal
    images are not equal unless they share the same ndarray buffer.
    This is a pragmatic choice: frames are short-lived, never used as
    dict keys, and image-equality comparisons should be done by
    `np.array_equal(a.image_bgr, b.image_bgr)` when needed.

    Field semantics:

    - `image_bgr`        : NumPy uint8 ndarray, shape `(height, width, 3)`, BGR.
    - `width`            : the second axis of `image_bgr` (= reference W).
    - `height`           : the first axis of `image_bgr` (= reference H).
    - `source_mode`      : `"raw"` | `"png"` | `"pull"`.
    - `capture_latency_ms`: end-to-end capture-to-BGR-ndarray latency.
    - `capture_ts`       : UTC capture instant (timezone-aware datetime).
    - `native_width`     : the device's native width before any remap.
    - `native_height`    : the device's native height before any remap.
    """

    image_bgr: np.ndarray
    width: int
    height: int
    source_mode: str
    capture_latency_ms: float
    capture_ts: _dt.datetime
    native_width: int
    native_height: int

    # Disable hashing — the underlying ndarray is unhashable. Frozen
    # dataclasses would otherwise auto-generate __hash__ via fields,
    # but that crashes on ndarray. We set eq=True (default) and
    # disable the hash explicitly.
    __hash__ = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # Validate the image first; subsequent dimension asserts depend on it.
        if not isinstance(self.image_bgr, np.ndarray):
            raise TypeError(
                f"Frame.image_bgr must be numpy.ndarray, got {type(self.image_bgr).__name__}"
            )
        if self.image_bgr.dtype != np.uint8:
            raise ValueError(
                f"Frame.image_bgr must be uint8, got dtype {self.image_bgr.dtype}"
            )
        if self.image_bgr.ndim != 3 or self.image_bgr.shape[2] != 3:
            raise ValueError(
                f"Frame.image_bgr must be (H, W, 3) BGR, got shape {self.image_bgr.shape}"
            )
        if self.image_bgr.size == 0:
            raise ValueError("Frame.image_bgr is empty")

        if self.height != self.image_bgr.shape[0] or self.width != self.image_bgr.shape[1]:
            raise ValueError(
                f"Frame width/height ({self.width}x{self.height}) does not match "
                f"image_bgr shape (H={self.image_bgr.shape[0]}, W={self.image_bgr.shape[1]})"
            )
        if self.native_width <= 0 or self.native_height <= 0:
            raise ValueError(
                f"native dimensions must be positive, got "
                f"{self.native_width}x{self.native_height}"
            )
        if self.source_mode not in _ALLOWED_SOURCE_MODES:
            raise ValueError(
                f"source_mode must be one of {sorted(_ALLOWED_SOURCE_MODES)}, "
                f"got {self.source_mode!r}"
            )
        if self.capture_latency_ms < 0:
            raise ValueError(
                f"capture_latency_ms must be >= 0, got {self.capture_latency_ms}"
            )
        if not isinstance(self.capture_ts, _dt.datetime):
            raise TypeError(
                f"capture_ts must be datetime, got {type(self.capture_ts).__name__}"
            )

        # Lock the array against accidental in-place mutation by
        # downstream code. The frozen dataclass prevents the *binding*
        # from changing; this prevents the *buffer* from changing.
        # Note: this is advisory — np.frombuffer-style views may share
        # memory with another array that is still writable.
        try:
            self.image_bgr.setflags(write=False)
        except ValueError:
            # The array is a view whose base is non-writeable; that's fine.
            pass

    # ------------------------------------------------------------------

    def shape_summary(self) -> str:
        """One-line representation of the frame's dimensions and source."""
        return (
            f"Frame({self.width}x{self.height} BGR, "
            f"native={self.native_width}x{self.native_height}, "
            f"mode={self.source_mode}, "
            f"latency={self.capture_latency_ms:.1f} ms)"
        )

    def to_debug_dict(self) -> Mapping[str, Any]:
        """Serializable summary suitable for `metadata.json` artifacts.

        Excludes the `image_bgr` ndarray (too large for a JSON metadata
        file). All values are JSON-encodable.
        """
        return {
            "width": self.width,
            "height": self.height,
            "channels": int(self.image_bgr.shape[2]),
            "dtype": str(self.image_bgr.dtype),
            "source_mode": self.source_mode,
            "capture_latency_ms": float(self.capture_latency_ms),
            "capture_ts": self.capture_ts.isoformat(),
            "native_width": self.native_width,
            "native_height": self.native_height,
        }
