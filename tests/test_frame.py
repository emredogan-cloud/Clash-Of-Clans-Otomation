"""Frame container tests: validation, immutability, summaries."""
from __future__ import annotations

import datetime as _dt

import numpy as np
import pytest

from automation.frame import Frame


def _bgr(w: int = 4, h: int = 4) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def _now() -> _dt.datetime:
    return _dt.datetime(2026, 5, 20, 12, 0, 0, tzinfo=_dt.timezone.utc)


def _make(image: np.ndarray | None = None, **overrides) -> Frame:
    img = image if image is not None else _bgr(8, 4)
    kwargs = dict(
        image_bgr=img,
        width=img.shape[1],
        height=img.shape[0],
        source_mode="raw",
        capture_latency_ms=12.5,
        capture_ts=_now(),
        native_width=img.shape[1],
        native_height=img.shape[0],
    )
    kwargs.update(overrides)
    return Frame(**kwargs)


def test_frame_constructs_with_valid_inputs() -> None:
    fr = _make()
    assert fr.image_bgr.shape == (4, 8, 3)
    assert fr.width == 8
    assert fr.height == 4
    assert fr.source_mode == "raw"
    assert fr.capture_latency_ms == 12.5


def test_frame_image_is_write_locked() -> None:
    fr = _make()
    with pytest.raises(ValueError):
        fr.image_bgr[0, 0, 0] = 1  # write-locked by __post_init__


def test_frame_dataclass_is_frozen() -> None:
    fr = _make()
    with pytest.raises(Exception):  # FrozenInstanceError, but exact class is impl-detail
        fr.width = 999  # type: ignore[misc]


def test_frame_rejects_non_ndarray_image() -> None:
    with pytest.raises(TypeError):
        Frame(
            image_bgr=[[0, 0, 0]],  # type: ignore[arg-type]
            width=1, height=1, source_mode="raw",
            capture_latency_ms=0.0, capture_ts=_now(),
            native_width=1, native_height=1,
        )


def test_frame_rejects_wrong_dtype() -> None:
    arr = np.zeros((4, 4, 3), dtype=np.float32)
    with pytest.raises(ValueError):
        _make(image=arr)


def test_frame_rejects_grayscale() -> None:
    arr = np.zeros((4, 4), dtype=np.uint8)
    with pytest.raises(ValueError):
        _make(image=arr)


def test_frame_rejects_wrong_channel_count() -> None:
    arr = np.zeros((4, 4, 4), dtype=np.uint8)
    with pytest.raises(ValueError):
        _make(image=arr)


def test_frame_rejects_empty_image() -> None:
    arr = np.zeros((0, 0, 3), dtype=np.uint8)
    with pytest.raises(ValueError):
        _make(image=arr)


def test_frame_rejects_dimension_mismatch() -> None:
    with pytest.raises(ValueError):
        _make(width=99)


def test_frame_rejects_zero_native_dims() -> None:
    with pytest.raises(ValueError):
        _make(native_width=0)
    with pytest.raises(ValueError):
        _make(native_height=0)


def test_frame_rejects_negative_latency() -> None:
    with pytest.raises(ValueError):
        _make(capture_latency_ms=-1.0)


def test_frame_rejects_bad_source_mode() -> None:
    with pytest.raises(ValueError):
        _make(source_mode="auto")  # auto is the requested-mode token, not a source token


def test_frame_rejects_non_datetime_capture_ts() -> None:
    with pytest.raises(TypeError):
        _make(capture_ts=1234567)  # type: ignore[arg-type]


def test_shape_summary_includes_dimensions_mode_and_latency() -> None:
    fr = _make()
    summary = fr.shape_summary()
    assert "8x4" in summary
    assert "raw" in summary
    assert "12.5" in summary


def test_to_debug_dict_is_json_safe() -> None:
    import json

    fr = _make(source_mode="png", capture_latency_ms=200.0,
               native_width=1080, native_height=2408)
    d = fr.to_debug_dict()
    encoded = json.dumps(d)
    decoded = json.loads(encoded)
    assert decoded["source_mode"] == "png"
    assert decoded["capture_latency_ms"] == 200.0
    assert decoded["native_width"] == 1080
    assert decoded["native_height"] == 2408
    assert decoded["channels"] == 3
    assert decoded["dtype"] == "uint8"


def test_frame_is_not_hashable() -> None:
    fr = _make()
    with pytest.raises(TypeError):
        hash(fr)
