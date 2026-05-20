"""Matcher end-to-end tests using synthetic frames and templates.

Strategy:
- Build a "frame" with a known patch at a known location.
- Crop that patch into a Template and run the matcher.
- Verify the matcher finds the patch at the expected coordinates with
  high confidence.
- For miss / threshold-fail / out-of-bounds cases, synthesize the
  appropriate inputs.

All tests are deterministic — no randomness, no real device.
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from automation.errors import InvalidROIError, MatchComputationError
from automation.frame import Frame
from automation.matcher import Matcher
from automation.match_result import MatchResult
from automation.template import Template


# ---- helpers -----------------------------------------------------------------


def _now() -> _dt.datetime:
    return _dt.datetime(2026, 5, 20, 12, 0, 0, tzinfo=_dt.timezone.utc)


def make_frame(width: int = 1080, height: int = 1920, fill: int = 30) -> np.ndarray:
    """Solid-color BGR frame used as a background."""
    return np.full((height, width, 3), fill, dtype=np.uint8)


def stamp_patch(
    frame_bgr: np.ndarray,
    x: int, y: int,
    patch_w: int = 64, patch_h: int = 64,
    pattern: int = 0,
) -> np.ndarray:
    """Stamp a deterministic high-contrast patch at (x, y).

    The patch is 4 quadrants of distinct gray levels — distinctive
    enough that cv2.matchTemplate finds it reliably without
    accidentally matching elsewhere on a uniform background.
    """
    h, w = patch_h // 2, patch_w // 2
    # Distinct quadrant values; vary with `pattern` so two templates
    # in the same test do not collide.
    a, b, c, d = (40 + pattern, 90 + pattern, 160 + pattern, 230 + pattern)
    frame_bgr[y:y + h, x:x + w] = a
    frame_bgr[y:y + h, x + w:x + patch_w] = b
    frame_bgr[y + h:y + patch_h, x:x + w] = c
    frame_bgr[y + h:y + patch_h, x + w:x + patch_w] = d
    return frame_bgr


def make_frame_obj(width: int = 1080, height: int = 1920, *,
                   image: np.ndarray | None = None,
                   capture_latency_ms: float = 940.0) -> Frame:
    img = image if image is not None else make_frame(width, height)
    return Frame(
        image_bgr=img,
        width=img.shape[1],
        height=img.shape[0],
        source_mode="raw",
        capture_latency_ms=capture_latency_ms,
        capture_ts=_now(),
        native_width=1080,
        native_height=2408,
    )


def template_from_frame(
    frame_bgr: np.ndarray,
    x: int, y: int, w: int, h: int,
    *, name: str = "patch", threshold: float = 0.9,
    roi: tuple[int, int, int, int] | None = None,
) -> Template:
    """Crop a region from a BGR frame and build a grayscale Template."""
    crop_bgr = frame_bgr[y:y + h, x:x + w].copy()
    crop_gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    return Template(
        name=name, image_gray=crop_gray, width=w, height=h,
        threshold=threshold, roi=roi,
    )


# ---- exact-match positive paths ----------------------------------------------


def test_match_exact_full_frame() -> None:
    bgr = make_frame()
    bgr = stamp_patch(bgr, x=200, y=400)
    frame = make_frame_obj(image=bgr)
    template = template_from_frame(bgr, x=200, y=400, w=64, h=64, threshold=0.9)

    result = Matcher().match(frame, template)

    assert isinstance(result, MatchResult)
    assert result.found is True
    assert result.search_mode == "full_gray"
    assert result.x == 200 and result.y == 400
    assert result.width == 64 and result.height == 64
    assert result.confidence > 0.99
    assert result.match_latency_ms >= 0
    assert result.capture_latency_ms == 940.0


def test_match_exact_with_roi() -> None:
    bgr = make_frame()
    bgr = stamp_patch(bgr, x=500, y=800)
    frame = make_frame_obj(image=bgr)
    template = template_from_frame(
        bgr, x=500, y=800, w=64, h=64, threshold=0.9,
        roi=(400, 700, 700, 900),
    )

    result = Matcher().match(frame, template)
    assert result.found is True
    assert result.search_mode == "roi_gray"
    # Match coordinates must be in FRAME space, not ROI space.
    assert result.x == 500 and result.y == 800


def test_match_below_threshold_returns_miss() -> None:
    """A structurally unrelated template never appears in the frame.

    TM_CCOEFF_NORMED is invariant to linear brightness/contrast shifts,
    so the template must be a *different shape*, not just a different
    fill value. Here we use a random-textured template that does not
    occur in a solid background.
    """
    bgr = make_frame()
    bgr = stamp_patch(bgr, x=200, y=400)  # frame has a 4-quadrant patch
    frame = make_frame_obj(image=bgr)
    rng = np.random.default_rng(seed=42)
    random_gray = rng.integers(0, 256, size=(64, 64), dtype=np.uint8)
    template = Template(
        name="random", image_gray=random_gray, width=64, height=64,
        threshold=0.99, roi=None,
    )

    result = Matcher().match(frame, template)
    assert result.found is False
    assert result.x is None and result.y is None
    assert result.confidence < 0.99


def test_roi_excludes_actual_match_returns_miss() -> None:
    """The match is at (200, 400) but the ROI only covers (700, 800)–(900, 1000)."""
    bgr = make_frame()
    bgr = stamp_patch(bgr, x=200, y=400)
    frame = make_frame_obj(image=bgr)
    template = template_from_frame(
        bgr, x=200, y=400, w=64, h=64, threshold=0.95,
        roi=(700, 800, 900, 1000),
    )
    result = Matcher().match(frame, template)
    # Inside the ROI there's nothing matching → low confidence → miss.
    assert result.found is False
    assert result.search_mode == "roi_gray"


def test_threshold_at_actual_confidence_is_inclusive() -> None:
    """A perfect match (confidence ≈ 1.0) with threshold 0.999 still matches."""
    bgr = make_frame()
    bgr = stamp_patch(bgr, x=100, y=100)
    frame = make_frame_obj(image=bgr)
    template = template_from_frame(bgr, x=100, y=100, w=64, h=64, threshold=0.999)
    result = Matcher().match(frame, template)
    assert result.found is True


# ---- error paths -------------------------------------------------------------


def test_template_larger_than_full_frame_raises_match_computation_error() -> None:
    frame = make_frame_obj(width=64, height=64)
    big = np.full((128, 128), 100, dtype=np.uint8)
    template = Template(
        name="too_big", image_gray=big, width=128, height=128, threshold=0.9, roi=None,
    )
    with pytest.raises(MatchComputationError, match="larger than frame"):
        Matcher().match(frame, template)


def test_roi_beyond_frame_raises_invalid_roi() -> None:
    bgr = make_frame()
    bgr = stamp_patch(bgr, x=100, y=100)
    frame = make_frame_obj(image=bgr)
    template = template_from_frame(
        bgr, x=100, y=100, w=64, h=64, threshold=0.9,
        roi=(0, 0, 10000, 10000),  # beyond 1080×1920
    )
    with pytest.raises(InvalidROIError, match="extends beyond"):
        Matcher().match(frame, template)


def test_template_larger_than_roi_raises_invalid_roi() -> None:
    bgr = make_frame()
    bgr = stamp_patch(bgr, x=100, y=100)
    frame = make_frame_obj(image=bgr)
    big_gray = np.full((100, 100), 100, dtype=np.uint8)
    template = Template(
        name="big_t", image_gray=big_gray, width=100, height=100, threshold=0.9,
        roi=(0, 0, 50, 50),  # 50×50 ROI; template 100×100 does not fit
    )
    with pytest.raises(InvalidROIError, match="does not fit"):
        Matcher().match(frame, template)


# ---- debug artifacts ---------------------------------------------------------


def test_debug_artifacts_written_when_enabled(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    bgr = make_frame()
    bgr = stamp_patch(bgr, x=300, y=600)
    frame = make_frame_obj(image=bgr)
    template = template_from_frame(bgr, x=300, y=600, w=64, h=64, threshold=0.9,
                                    name="test/patch with spaces")

    artifacts = tmp_path / "var" / "artifacts" / "matcher"
    monkeypatch.setattr("automation.matcher.ARTIFACTS_DIR", artifacts)

    m = Matcher(debug=True)
    result = m.match(frame, template)
    assert result.found is True

    subdirs = list(artifacts.iterdir())
    assert len(subdirs) == 1
    d = subdirs[0]
    assert (d / "frame.jpg").is_file()
    assert (d / "template.jpg").is_file()
    assert (d / "heatmap.jpg").is_file()
    md = json.loads((d / "metadata.json").read_text())
    assert md["result"]["found"] is True
    assert md["template"]["name"] == "test/patch with spaces"
    assert md["search_mode"] == "full_gray"
    assert "frame" in md and md["frame"]["width"] == 1080


def test_debug_artifacts_skipped_when_disabled(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    bgr = make_frame()
    bgr = stamp_patch(bgr, x=100, y=100)
    frame = make_frame_obj(image=bgr)
    template = template_from_frame(bgr, x=100, y=100, w=64, h=64, threshold=0.9)
    artifacts = tmp_path / "var" / "artifacts" / "matcher"
    monkeypatch.setattr("automation.matcher.ARTIFACTS_DIR", artifacts)

    Matcher(debug=False).match(frame, template)
    if artifacts.exists():
        assert not any(artifacts.iterdir())


def test_debug_env_var_enables_artifacts(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    bgr = make_frame()
    bgr = stamp_patch(bgr, x=100, y=100)
    frame = make_frame_obj(image=bgr)
    template = template_from_frame(bgr, x=100, y=100, w=64, h=64, threshold=0.9)
    artifacts = tmp_path / "var" / "artifacts" / "matcher"
    monkeypatch.setattr("automation.matcher.ARTIFACTS_DIR", artifacts)
    monkeypatch.setenv("MATCHER_DEBUG", "1")

    m = Matcher()  # debug param defaults to env var
    assert m.debug is True
    m.match(frame, template)
    assert any(artifacts.iterdir())


# ---- latency / metadata ------------------------------------------------------


def test_match_latency_is_nonzero_and_finite() -> None:
    bgr = make_frame()
    bgr = stamp_patch(bgr, x=100, y=100)
    frame = make_frame_obj(image=bgr)
    template = template_from_frame(bgr, x=100, y=100, w=64, h=64, threshold=0.9)
    result = Matcher().match(frame, template)
    assert 0 < result.match_latency_ms < 10000


def test_capture_latency_is_copied_from_frame() -> None:
    bgr = make_frame()
    bgr = stamp_patch(bgr, x=100, y=100)
    frame = make_frame_obj(image=bgr, capture_latency_ms=1234.5)
    template = template_from_frame(bgr, x=100, y=100, w=64, h=64, threshold=0.9)
    result = Matcher().match(frame, template)
    assert result.capture_latency_ms == 1234.5


def test_match_returns_immutable_result() -> None:
    bgr = make_frame()
    bgr = stamp_patch(bgr, x=100, y=100)
    frame = make_frame_obj(image=bgr)
    template = template_from_frame(bgr, x=100, y=100, w=64, h=64, threshold=0.9)
    result = Matcher().match(frame, template)
    with pytest.raises(Exception):  # frozen dataclass
        result.found = False  # type: ignore[misc]


# ---- ROI offset correctness --------------------------------------------------


def test_roi_offset_correctly_translates_to_frame_coords() -> None:
    """The match inside the ROI must report frame coordinates, not ROI coordinates."""
    bgr = make_frame()
    # Patch at exactly (600, 900) in the frame.
    bgr = stamp_patch(bgr, x=600, y=900)
    frame = make_frame_obj(image=bgr)
    template = template_from_frame(
        bgr, x=600, y=900, w=64, h=64, threshold=0.9,
        roi=(500, 800, 800, 1000),  # ROI offset is (500, 800)
    )
    result = Matcher().match(frame, template)
    assert result.found is True
    # If the matcher reported ROI-relative coords, this would be (100, 100).
    assert result.x == 600 and result.y == 900
    assert result.center() == (600 + 32, 900 + 32) == (632, 932)


# ---- a quick smoke through the public API of MatchResult ---------------------


def test_match_result_summary_is_human_readable() -> None:
    bgr = make_frame()
    bgr = stamp_patch(bgr, x=100, y=100)
    frame = make_frame_obj(image=bgr)
    template = template_from_frame(bgr, x=100, y=100, w=64, h=64, threshold=0.9,
                                    name="ok")
    result = Matcher().match(frame, template)
    summary = result.summary()
    assert "HIT" in summary
    assert "ok" in summary
