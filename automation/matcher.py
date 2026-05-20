"""THINK layer — `Matcher` produces `MatchResult` from `Frame` + `Template`.

Phase 3 implements the primary CV strategy specified by ADR-03:
`cv2.matchTemplate` with `TM_CCOEFF_NORMED`. Grayscale only, one
template per call, ROI-restricted or full-frame.

Out of scope for Phase 3 (deferred):
- Multi-template batch matching (callers loop in v1.0).
- Multi-scale fallback (ADR-03 `multi_scale: true`).
- Masks (ADR-03 binary masks).
- Non-maximum suppression / find-all-instances (utility, not default).
- A `FramePreprocessor` cache for grayscale conversion across calls.

Phase 0 measurements on the operator's hardware (1080×1920 reference,
110×110 template):

  variant            median (ms)
  ROI grayscale       2.2
  ROI BGR             7.0       (BGR is NOT supported in Phase 3)
  full-frame gray    33.6
  full-frame BGR    137.9       (NOT supported)

The matcher reports `search_mode` as either `"roi_gray"` or
`"full_gray"`. BGR matching is structurally not available — templates
are required to be grayscale (`Template` enforces this).

Coordinate convention: `(x, y)` is the TOP-LEFT corner of the matched
template within the reference frame, NOT the centre. `MatchResult.center()`
returns the convenience midpoint.

Debug artifacts: when `MATCHER_DEBUG=1` (or `Matcher(debug=True)`),
each `match()` writes a per-invocation directory under
`var/artifacts/matcher/<ts>_<template>_<uuid>/` containing the BGR
frame (jpg), the grayscale template (jpg), the normalized correlation
heatmap (jpg), and a JSON metadata sidecar. Atomic writes via
`tmp` + rename. No GUI, no `cv2.imshow`.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

import cv2
import numpy as np

from .errors import MatchComputationError
from .frame import Frame
from .match_result import MatchResult
from .paths import ARTIFACTS
from .template import Template

_LOG = logging.getLogger(__name__)

ARTIFACTS_DIR: Path = ARTIFACTS / "matcher"


def _parse_bool_env(var: str, default: bool = False) -> bool:
    raw = os.environ.get(var)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


class Matcher:
    """Match a single grayscale template against a frame.

    Stateless apart from the `debug` flag. Construct once, reuse for
    every match. Thread-safe — `cv2.matchTemplate` releases the GIL,
    and the matcher holds no per-call state on the instance.
    """

    def __init__(self, debug: bool | None = None) -> None:
        self.debug: bool = debug if debug is not None else _parse_bool_env("MATCHER_DEBUG")

    # ------------------------------------------------------------------

    def match(self, frame: Frame, template: Template) -> MatchResult:
        """Locate `template` inside `frame`. Returns a `MatchResult`.

        Steps (see module docstring for ADR mapping):

        1. Convert `frame.image_bgr` to grayscale.
        2. Choose search image: ROI crop if `template.roi` is set,
           otherwise the full grayscale frame.
        3. Validate that the template fits within the search image.
        4. Run `cv2.matchTemplate(TM_CCOEFF_NORMED)`.
        5. Find the peak with `cv2.minMaxLoc`.
        6. Clamp raw correlation to `[0, 1]`; compare against
           `template.threshold` for the found/not-found verdict.
        7. Adjust the peak's coordinates by the ROI offset so the
           returned `(x, y)` is in *frame* coordinates.
        8. Optionally persist debug artifacts.

        Raises:

        - `InvalidROIError` if `template.roi` extends beyond the frame
          or the template does not fit inside the ROI.
        - `MatchComputationError` if `cv2.matchTemplate` fails (most
          commonly: template larger than the search image when no ROI
          is set).
        """
        t0 = time.perf_counter_ns()

        # 1. Grayscale conversion of the frame.
        frame_gray = cv2.cvtColor(frame.image_bgr, cv2.COLOR_BGR2GRAY)

        # 2./3. Determine search region.
        if template.roi is not None:
            # ROI is validated against frame dims here (and also lives
            # in Template.validate_roi for callers that want to check
            # before invoking the matcher).
            template.validate_roi(frame_width=frame.width, frame_height=frame.height)
            x1, y1, x2, y2 = template.roi
            search_image = frame_gray[y1:y2, x1:x2]
            search_mode = "roi_gray"
            offset_x, offset_y = x1, y1
        else:
            if template.width > frame.width or template.height > frame.height:
                raise MatchComputationError(
                    f"template {template.name!r} ({template.width}x{template.height}) "
                    f"is larger than frame ({frame.width}x{frame.height}); cannot "
                    f"perform full-frame match"
                )
            search_image = frame_gray
            search_mode = "full_gray"
            offset_x, offset_y = 0, 0

        # 4./5. Run matchTemplate + find peak.
        try:
            heatmap = cv2.matchTemplate(search_image, template.image_gray, cv2.TM_CCOEFF_NORMED)
        except cv2.error as exc:
            raise MatchComputationError(
                f"cv2.matchTemplate failed for template {template.name!r}: {exc}"
            ) from exc
        _, max_val, _, max_loc = cv2.minMaxLoc(heatmap)
        max_x, max_y = max_loc  # top-left of the matched region in search_image

        # 6. Clamp + threshold.
        confidence = float(max(0.0, min(1.0, float(max_val))))
        found = confidence >= template.threshold

        # 7. Translate to frame coordinates.
        abs_x = int(max_x + offset_x) if found else None
        abs_y = int(max_y + offset_y) if found else None
        match_w = int(template.width) if found else None
        match_h = int(template.height) if found else None

        t1 = time.perf_counter_ns()
        match_latency_ms = (t1 - t0) / 1e6

        result = MatchResult(
            found=found,
            confidence=confidence,
            template_name=template.name,
            search_mode=search_mode,
            capture_latency_ms=frame.capture_latency_ms,
            match_latency_ms=match_latency_ms,
            x=abs_x,
            y=abs_y,
            width=match_w,
            height=match_h,
        )

        if self.debug:
            self._write_artifacts(frame, template, heatmap, result, search_mode)

        _LOG.debug(
            "match template=%s confidence=%.3f mode=%s match=%.2f ms found=%s",
            template.name, confidence, search_mode, match_latency_ms, found,
        )
        return result

    # ------------------------------------------------------------------

    def _write_artifacts(
        self,
        frame: Frame,
        template: Template,
        heatmap: np.ndarray,
        result: MatchResult,
        search_mode: str,
    ) -> None:
        """Write per-match debug artifacts. Best-effort; never raises."""
        try:
            ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
            safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in template.name)
            cap_dir = ARTIFACTS_DIR / f"{ts}_{safe_name}_{uuid.uuid4().hex[:8]}"
            cap_dir.mkdir(parents=True, exist_ok=True)

            # Frame as JPEG (BGR).
            ok, frame_jpg = cv2.imencode(
                ".jpg", frame.image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85]
            )
            if ok:
                _atomic_write_bytes(cap_dir / "frame.jpg", frame_jpg.tobytes())

            # Template as JPEG (grayscale, cv2 will encode single-channel).
            ok, tpl_jpg = cv2.imencode(
                ".jpg", template.image_gray, [int(cv2.IMWRITE_JPEG_QUALITY), 90]
            )
            if ok:
                _atomic_write_bytes(cap_dir / "template.jpg", tpl_jpg.tobytes())

            # Heatmap: normalize float32 [-1, 1] (or [0, 1] under TM_CCOEFF_NORMED
            # but min-max stretches contrast) into uint8 [0, 255] for visibility.
            heatmap_vis = _heatmap_to_uint8(heatmap)
            ok, hm_jpg = cv2.imencode(
                ".jpg", heatmap_vis, [int(cv2.IMWRITE_JPEG_QUALITY), 85]
            )
            if ok:
                _atomic_write_bytes(cap_dir / "heatmap.jpg", hm_jpg.tobytes())

            # Metadata. Combines MatchResult.to_debug_dict with sizes etc.
            metadata: dict[str, object] = {
                "search_mode": search_mode,
                "frame": {
                    "width": frame.width,
                    "height": frame.height,
                    "native_width": frame.native_width,
                    "native_height": frame.native_height,
                    "source_mode": frame.source_mode,
                    "capture_latency_ms": frame.capture_latency_ms,
                },
                "template": {
                    "name": template.name,
                    "width": template.width,
                    "height": template.height,
                    "threshold": template.threshold,
                    "roi": list(template.roi) if template.roi is not None else None,
                },
                "heatmap_shape": list(heatmap.shape),
                "result": dict(result.to_debug_dict()),
            }
            _atomic_write_bytes(
                cap_dir / "metadata.json",
                (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
            _LOG.debug("wrote matcher artifacts to %s", cap_dir)
        except (OSError, ValueError) as exc:
            _LOG.warning("could not write matcher artifacts: %s", exc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _heatmap_to_uint8(heatmap: np.ndarray) -> np.ndarray:
    """Convert a float32 matchTemplate result into a uint8 visualization.

    Stretches the correlation range to `[0, 255]` for human visibility.
    The transform is invertible only up to scale; the JPEG is a debug
    artifact, not a quantitative input.
    """
    arr = np.asarray(heatmap, dtype=np.float32)
    if arr.size == 0:
        return np.zeros((1, 1), dtype=np.uint8)
    lo = float(arr.min())
    hi = float(arr.max())
    if hi <= lo:
        return np.zeros_like(arr, dtype=np.uint8)
    norm = (arr - lo) / (hi - lo)
    return np.clip(norm * 255.0, 0.0, 255.0).astype(np.uint8)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        shutil.move(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
