"""Denormalizer tests — reference→device inverse-mapping (ADR-04)."""
from __future__ import annotations

import math

import pytest

from automation.denormalize import DEFAULT_REFERENCE_RESOLUTION, Denormalizer
from automation.errors import CoordinateError


# ---- construction ------------------------------------------------------------


def test_default_reference_is_1080x1920() -> None:
    d = Denormalizer()
    assert d.reference_resolution == (1080, 1920)
    assert d.reference_width == 1080
    assert d.reference_height == 1920


def test_custom_reference() -> None:
    d = Denormalizer((720, 1280))
    assert d.reference_resolution == (720, 1280)


def test_zero_reference_rejected() -> None:
    with pytest.raises(CoordinateError, match="must be positive"):
        Denormalizer((0, 1920))
    with pytest.raises(CoordinateError, match="must be positive"):
        Denormalizer((1080, 0))


def test_negative_reference_rejected() -> None:
    with pytest.raises(CoordinateError, match="must be positive"):
        Denormalizer((-1, 1920))


def test_non_int_reference_rejected() -> None:
    with pytest.raises(CoordinateError, match="must be integers"):
        Denormalizer((1080.0, 1920))  # type: ignore[arg-type]


# ---- identity mapping (native == reference) ----------------------------------


def test_identity_at_reference_resolution() -> None:
    d = Denormalizer()
    # When native_w == ref_w and native_h == ref_h, the mapping is identity
    # (modulo round() — integer inputs round-trip exactly).
    assert d.to_native(0, 0, 1080, 1920) == (0, 0)
    assert d.to_native(540, 960, 1080, 1920) == (540, 960)
    assert d.to_native(1079, 1919, 1080, 1920) == (1079, 1919)


def test_identity_with_floats_at_reference_resolution() -> None:
    d = Denormalizer()
    # Float input, identity scaling — round to nearest int.
    assert d.to_native(540.0, 960.0, 1080, 1920) == (540, 960)
    assert d.to_native(540.4, 960.4, 1080, 1920) == (540, 960)
    assert d.to_native(540.6, 960.6, 1080, 1920) == (541, 961)


# ---- operator device (1080x2408) --------------------------------------------


def test_operator_device_1080x2408_x_unchanged() -> None:
    """Operator device shares ref width; only y-axis scales."""
    d = Denormalizer()
    nx, ny = d.to_native(540, 0, 1080, 2408)
    assert nx == 540
    assert ny == 0


def test_operator_device_1080x2408_y_scales() -> None:
    d = Denormalizer()
    # y_ref=960 (mid of 1920) → y_native ≈ 960 * 2408/1920 = 1204
    nx, ny = d.to_native(540, 960, 1080, 2408)
    assert nx == 540
    assert ny == 1204


def test_operator_device_corner() -> None:
    d = Denormalizer()
    # x_ref=1079 (last column) is < ref_w; should map to 1079 since ref_w == native_w
    # y_ref=1919 (last row) → y_native ≈ 1919 * 2408/1920 ≈ 2406.74 → 2407
    nx, ny = d.to_native(1079, 1919, 1080, 2408)
    assert nx == 1079
    assert ny == 2407


# ---- scaling examples (other devices) ---------------------------------------


def test_scale_down_to_720x1280() -> None:
    """Reference 1080x1920 → device 720x1280 = exact 2/3 scaling."""
    d = Denormalizer()
    nx, ny = d.to_native(540, 960, 720, 1280)
    assert nx == 360
    assert ny == 640


def test_scale_up_to_1440x2560() -> None:
    """Reference 1080x1920 → device 1440x2560 = exact 4/3 scaling."""
    d = Denormalizer()
    nx, ny = d.to_native(540, 960, 1440, 2560)
    assert nx == 720
    assert ny == 1280


def test_origin_always_maps_to_origin() -> None:
    d = Denormalizer()
    for native_w, native_h in [(720, 1280), (1080, 2408), (1440, 3200)]:
        assert d.to_native(0, 0, native_w, native_h) == (0, 0)


# ---- bounds ------------------------------------------------------------------


def test_x_ref_equal_to_reference_width_rejected() -> None:
    """Half-open: reference_width is exclusive."""
    d = Denormalizer()
    with pytest.raises(CoordinateError, match="x_ref must be in"):
        d.to_native(1080, 0, 1080, 2408)


def test_y_ref_equal_to_reference_height_rejected() -> None:
    d = Denormalizer()
    with pytest.raises(CoordinateError, match="y_ref must be in"):
        d.to_native(0, 1920, 1080, 2408)


def test_negative_x_ref_rejected() -> None:
    d = Denormalizer()
    with pytest.raises(CoordinateError, match="x_ref must be in"):
        d.to_native(-1, 0, 1080, 2408)


def test_negative_y_ref_rejected() -> None:
    d = Denormalizer()
    with pytest.raises(CoordinateError, match="y_ref must be in"):
        d.to_native(0, -0.001, 1080, 2408)


def test_native_width_must_be_positive() -> None:
    d = Denormalizer()
    with pytest.raises(CoordinateError, match="native_width must be positive"):
        d.to_native(0, 0, 0, 2408)


def test_native_height_must_be_positive() -> None:
    d = Denormalizer()
    with pytest.raises(CoordinateError, match="native_height must be positive"):
        d.to_native(0, 0, 1080, -1)


def test_native_dims_must_be_int() -> None:
    d = Denormalizer()
    with pytest.raises(CoordinateError, match="native_width must be int"):
        d.to_native(0, 0, 1080.0, 2408)  # type: ignore[arg-type]
    with pytest.raises(CoordinateError, match="native_height must be int"):
        d.to_native(0, 0, 1080, 2408.0)  # type: ignore[arg-type]


def test_nan_x_ref_rejected() -> None:
    d = Denormalizer()
    with pytest.raises(CoordinateError, match="must be finite"):
        d.to_native(math.nan, 0, 1080, 2408)


def test_infinite_y_ref_rejected() -> None:
    d = Denormalizer()
    with pytest.raises(CoordinateError, match="must be finite"):
        d.to_native(0, math.inf, 1080, 2408)


def test_string_coordinate_rejected() -> None:
    d = Denormalizer()
    with pytest.raises(CoordinateError, match="x_ref must be int or float"):
        d.to_native("0", 0, 1080, 2408)  # type: ignore[arg-type]


def test_bool_coordinate_rejected() -> None:
    """bool is subclass of int; must be excluded explicitly."""
    d = Denormalizer()
    with pytest.raises(CoordinateError, match="x_ref must be int or float"):
        d.to_native(True, 0, 1080, 2408)  # type: ignore[arg-type]


# ---- determinism / no mutation ----------------------------------------------


def test_repeated_calls_are_deterministic() -> None:
    d = Denormalizer()
    out1 = d.to_native(540.5, 960.5, 1080, 2408)
    out2 = d.to_native(540.5, 960.5, 1080, 2408)
    assert out1 == out2


def test_two_denormalizers_with_same_config_agree() -> None:
    d1 = Denormalizer()
    d2 = Denormalizer(DEFAULT_REFERENCE_RESOLUTION)
    cases = [(0, 0), (100, 200), (540, 960), (1079, 1919)]
    for x, y in cases:
        assert d1.to_native(x, y, 1080, 2408) == d2.to_native(x, y, 1080, 2408)


def test_no_mutation_of_inputs() -> None:
    """Trivial check that the call doesn't mutate the Denormalizer state."""
    d = Denormalizer()
    snapshot = (d.reference_width, d.reference_height)
    d.to_native(540, 960, 1080, 2408)
    assert (d.reference_width, d.reference_height) == snapshot


# ---- output bounds-invariant -------------------------------------------------


@pytest.mark.parametrize("native_w,native_h", [
    (720, 1280),
    (1080, 1920),
    (1080, 2408),
    (1440, 3200),
    (2400, 1080),  # landscape-shaped device, exercised for math only
    (33, 47),      # tiny weird dims
])
def test_outputs_always_within_native_bounds(native_w: int, native_h: int) -> None:
    d = Denormalizer()
    cases = [
        (0, 0), (1, 1), (540, 960), (1079, 1919),
        (1079.999, 1919.999), (0.0, 0.0),
    ]
    for x_ref, y_ref in cases:
        nx, ny = d.to_native(x_ref, y_ref, native_w, native_h)
        assert 0 <= nx < native_w, f"x: {nx} not in [0, {native_w})"
        assert 0 <= ny < native_h, f"y: {ny} not in [0, {native_h})"


def test_upper_edge_clamping() -> None:
    """A reference coord just under the upper bound stays inside the device."""
    d = Denormalizer()
    # x_ref=1079.999 → x_native = round(1079.999 * 720/1080) = round(719.999...) = 720
    # which gets clamped to 719.
    nx, ny = d.to_native(1079.999, 0, 720, 1280)
    assert nx == 719
    assert 0 <= nx < 720
