"""Remap module tests: 1080x2408 → 1080x1920, idempotent on reference,
metadata preservation."""
from __future__ import annotations

import datetime as _dt

import numpy as np
import pytest

from automation.frame import Frame
from automation.remap import Remap


def _frame(w: int, h: int, *, source_mode: str = "raw",
           native_w: int | None = None, native_h: int | None = None,
           latency_ms: float = 100.0) -> Frame:
    return Frame(
        image_bgr=np.full((h, w, 3), 42, dtype=np.uint8),
        width=w,
        height=h,
        source_mode=source_mode,
        capture_latency_ms=latency_ms,
        capture_ts=_dt.datetime(2026, 5, 20, 12, 0, tzinfo=_dt.timezone.utc),
        native_width=native_w if native_w is not None else w,
        native_height=native_h if native_h is not None else h,
    )


def test_remap_default_reference_is_1080x1920() -> None:
    r = Remap()
    assert r.reference_resolution == (1080, 1920)


def test_remap_rejects_invalid_reference_resolution() -> None:
    with pytest.raises(ValueError):
        Remap(reference_resolution=(0, 1920))
    with pytest.raises(ValueError):
        Remap(reference_resolution=(1080, -1))


def test_remap_resizes_xiaomi_1080x2408_to_1080x1920() -> None:
    """The exact dimension change measured in Phase 0."""
    r = Remap()
    native = _frame(1080, 2408)
    out = r.apply(native)
    assert out.width == 1080
    assert out.height == 1920
    assert out.image_bgr.shape == (1920, 1080, 3)
    assert out.image_bgr.dtype == np.uint8


def test_remap_preserves_native_dims_through_resize() -> None:
    r = Remap()
    native = _frame(1080, 2408, native_w=1080, native_h=2408)
    out = r.apply(native)
    assert out.native_width == 1080
    assert out.native_height == 2408


def test_remap_returns_new_frame_when_already_at_reference() -> None:
    """Idempotent on a reference-res frame; returns a new Frame (not same object)."""
    r = Remap()
    src = _frame(1080, 1920, native_w=1080, native_h=2408)
    out = r.apply(src)
    assert out is not src
    assert out.image_bgr is src.image_bgr  # no-copy
    assert (out.width, out.height) == (1080, 1920)
    assert (out.native_width, out.native_height) == (1080, 2408)


def test_remap_carries_source_mode_and_latency() -> None:
    r = Remap()
    src = _frame(1080, 2408, source_mode="png", latency_ms=678.9)
    out = r.apply(src)
    assert out.source_mode == "png"
    assert out.capture_latency_ms == 678.9


def test_remap_upsample_path() -> None:
    """When the source is smaller, INTER_LINEAR upsamples."""
    r = Remap(reference_resolution=(1080, 1920))
    src = _frame(540, 960, native_w=540, native_h=960)
    out = r.apply(src)
    assert out.width == 1080
    assert out.height == 1920
    assert out.image_bgr.shape == (1920, 1080, 3)


def test_remap_custom_reference() -> None:
    r = Remap(reference_resolution=(720, 1280))
    src = _frame(1080, 2400)
    out = r.apply(src)
    assert out.width == 720
    assert out.height == 1280


def test_remap_preserves_capture_ts() -> None:
    r = Remap()
    src = _frame(1080, 2408)
    out = r.apply(src)
    assert out.capture_ts == src.capture_ts
