"""Template container tests."""
from __future__ import annotations

import numpy as np
import pytest

from automation.errors import InvalidROIError
from automation.template import Template


def _gray(w: int = 32, h: int = 32, value: int = 200) -> np.ndarray:
    return np.full((h, w), value, dtype=np.uint8)


def _t(image: np.ndarray | None = None, **overrides) -> Template:
    img = image if image is not None else _gray()
    kwargs = dict(
        name="play_button",
        image_gray=img,
        width=img.shape[1],
        height=img.shape[0],
        threshold=0.92,
        roi=None,
    )
    kwargs.update(overrides)
    return Template(**kwargs)


def test_template_constructs_with_valid_inputs() -> None:
    t = _t()
    assert t.name == "play_button"
    assert t.image_gray.shape == (32, 32)
    assert t.threshold == 0.92
    assert t.roi is None


def test_template_is_frozen() -> None:
    t = _t()
    with pytest.raises(Exception):  # FrozenInstanceError
        t.name = "other"  # type: ignore[misc]


def test_template_image_is_write_locked() -> None:
    t = _t()
    with pytest.raises(ValueError):
        t.image_gray[0, 0] = 1


def test_template_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="name"):
        _t(name="")


def test_template_rejects_non_ndarray() -> None:
    with pytest.raises(TypeError):
        Template(
            name="x", image_gray=[[0]],  # type: ignore[arg-type]
            width=1, height=1, threshold=0.5, roi=None,
        )


def test_template_rejects_non_uint8() -> None:
    arr = np.zeros((8, 8), dtype=np.float32)
    with pytest.raises(ValueError, match="uint8"):
        _t(image=arr)


def test_template_rejects_3d_image() -> None:
    arr = np.zeros((8, 8, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match="2D grayscale"):
        _t(image=arr)


def test_template_rejects_empty_image() -> None:
    arr = np.zeros((0, 0), dtype=np.uint8)
    with pytest.raises(ValueError, match="empty"):
        _t(image=arr)


def test_template_rejects_dimension_mismatch() -> None:
    with pytest.raises(ValueError, match="does not match"):
        _t(width=999)


def test_template_rejects_threshold_zero() -> None:
    """Threshold = 0 is not in (0, 1]."""
    with pytest.raises(ValueError, match="threshold"):
        _t(threshold=0.0)


def test_template_rejects_threshold_above_one() -> None:
    with pytest.raises(ValueError, match="threshold"):
        _t(threshold=1.01)


def test_template_accepts_threshold_at_one() -> None:
    _t(threshold=1.0)


def test_template_rejects_non_tuple_roi() -> None:
    with pytest.raises(InvalidROIError, match="4-tuple"):
        _t(roi=[0, 0, 10, 10])  # type: ignore[arg-type]


def test_template_rejects_short_roi() -> None:
    with pytest.raises(InvalidROIError, match="4-tuple"):
        _t(roi=(0, 0, 10))  # type: ignore[arg-type]


def test_template_rejects_negative_roi_coord() -> None:
    with pytest.raises(InvalidROIError, match="top-left"):
        _t(roi=(-1, 0, 10, 10))


def test_template_rejects_inverted_roi() -> None:
    with pytest.raises(InvalidROIError, match="x1 < x2"):
        _t(roi=(50, 50, 10, 10))


def test_template_rejects_non_int_roi() -> None:
    with pytest.raises(InvalidROIError, match="int"):
        _t(roi=(0.0, 0.0, 10.0, 10.0))  # type: ignore[arg-type]


def test_template_with_valid_roi() -> None:
    t = _t(roi=(100, 200, 300, 400))
    assert t.roi == (100, 200, 300, 400)


def test_validate_roi_no_op_when_no_roi() -> None:
    _t(roi=None).validate_roi(frame_width=1080, frame_height=1920)  # no exception


def test_validate_roi_accepts_roi_inside_frame() -> None:
    t = _t(roi=(100, 200, 300, 400))
    t.validate_roi(frame_width=1080, frame_height=1920)


def test_validate_roi_rejects_roi_beyond_frame() -> None:
    t = _t(roi=(100, 200, 2000, 400))
    with pytest.raises(InvalidROIError, match="extends beyond"):
        t.validate_roi(frame_width=1080, frame_height=1920)


def test_validate_roi_rejects_template_larger_than_roi() -> None:
    # 32x32 template in a 20x20 ROI
    t = _t(roi=(0, 0, 20, 20))
    with pytest.raises(InvalidROIError, match="does not fit"):
        t.validate_roi(frame_width=1080, frame_height=1920)


def test_shape_summary() -> None:
    t = _t(roi=(100, 200, 300, 400), threshold=0.95)
    s = t.shape_summary()
    assert "play_button" in s
    assert "32x32" in s
    assert "0.950" in s
    assert "roi=" in s


def test_shape_summary_no_roi() -> None:
    s = _t().shape_summary()
    assert "no-roi" in s


def test_template_is_not_hashable() -> None:
    with pytest.raises(TypeError):
        hash(_t())
