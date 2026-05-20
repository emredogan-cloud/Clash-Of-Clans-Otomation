"""Affine remap from device-native resolution to the v1.0 reference.

ADR-04: every captured frame is resampled into a single virtual
reference resolution (1080×1920 portrait) before THINK runs.

Phase 2 scope:
- `Remap.apply(frame: Frame) -> Frame` — produces a new Frame at the
  reference resolution, preserving the source `native_width` /
  `native_height` so downstream phases can de-normalise coordinates.

Out of scope (Phase 4 will add):
- coordinate transforms (normalised → device pixels).
- letterboxing for non-portrait or unusually-tall aspect ratios.

Per the Phase 2 prompt: use `cv2.resize` with `INTER_LINEAR`. The
Phase 0 match-bench confirmed this is fast enough for the per-tick
budget (well under 8 ms on the operator's hardware).
"""
from __future__ import annotations

import logging

import cv2
import numpy as np

from .frame import Frame

_LOG = logging.getLogger(__name__)

DEFAULT_REFERENCE_RESOLUTION: tuple[int, int] = (1080, 1920)


class Remap:
    """Resamples frames into the v1.0 reference resolution.

    Stateless apart from the target reference resolution. Construct
    once, reuse for every capture. `apply(frame)` is idempotent on a
    frame already at the reference resolution (returns a new Frame
    pointing at the same buffer, no resize work performed).
    """

    def __init__(
        self,
        reference_resolution: tuple[int, int] = DEFAULT_REFERENCE_RESOLUTION,
    ) -> None:
        rw, rh = reference_resolution
        if rw <= 0 or rh <= 0:
            raise ValueError(
                f"reference resolution must be positive, got {rw}x{rh}"
            )
        self.reference_width: int = rw
        self.reference_height: int = rh

    @property
    def reference_resolution(self) -> tuple[int, int]:
        return (self.reference_width, self.reference_height)

    def apply(self, frame: Frame) -> Frame:
        """Return a new Frame whose image is at the reference resolution.

        - If `frame` is already at the reference resolution, returns a
          new Frame with the same image (no resize work, no copy).
        - Otherwise applies `cv2.resize` with `INTER_LINEAR`.

        The native dimensions (`native_width` / `native_height`) of the
        input frame are preserved into the output frame; coordinate
        de-normalisation in later phases relies on them.
        """
        if frame.width == self.reference_width and frame.height == self.reference_height:
            # Already at reference: skip the resize cost, but still
            # return a *new* Frame so the contract "apply returns a new
            # Frame" holds and the array is freshly write-locked.
            return Frame(
                image_bgr=frame.image_bgr,
                width=frame.width,
                height=frame.height,
                source_mode=frame.source_mode,
                capture_latency_ms=frame.capture_latency_ms,
                capture_ts=frame.capture_ts,
                native_width=frame.native_width,
                native_height=frame.native_height,
            )

        _LOG.debug(
            "remap %dx%d -> %dx%d (INTER_LINEAR)",
            frame.width, frame.height, self.reference_width, self.reference_height,
        )
        # cv2.resize signature: (src, dsize=(W, H), interpolation=...)
        resized: np.ndarray = cv2.resize(
            frame.image_bgr,
            (self.reference_width, self.reference_height),
            interpolation=cv2.INTER_LINEAR,
        )
        return Frame(
            image_bgr=resized,
            width=self.reference_width,
            height=self.reference_height,
            source_mode=frame.source_mode,
            capture_latency_ms=frame.capture_latency_ms,
            capture_ts=frame.capture_ts,
            native_width=frame.native_width,
            native_height=frame.native_height,
        )
