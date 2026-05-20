"""MatchResult container tests."""
from __future__ import annotations

import json

import pytest

from automation.match_result import MatchResult


def _hit(**overrides) -> MatchResult:
    kwargs = dict(
        found=True, confidence=0.95, template_name="t", search_mode="roi_gray",
        capture_latency_ms=940.0, match_latency_ms=2.1,
        x=100, y=200, width=110, height=110,
    )
    kwargs.update(overrides)
    return MatchResult(**kwargs)


def _miss(**overrides) -> MatchResult:
    kwargs = dict(
        found=False, confidence=0.2, template_name="t", search_mode="full_gray",
        capture_latency_ms=940.0, match_latency_ms=33.6,
    )
    kwargs.update(overrides)
    return MatchResult(**kwargs)


def test_hit_constructs_with_valid_fields() -> None:
    r = _hit()
    assert r.found is True
    assert r.confidence == 0.95
    assert r.x == 100 and r.y == 200
    assert r.width == 110 and r.height == 110


def test_miss_omits_coords() -> None:
    r = _miss()
    assert r.found is False
    assert r.x is None and r.y is None
    assert r.width is None and r.height is None


def test_rejects_confidence_below_zero() -> None:
    with pytest.raises(ValueError, match="confidence"):
        _miss(confidence=-0.01)


def test_rejects_confidence_above_one() -> None:
    with pytest.raises(ValueError, match="confidence"):
        _hit(confidence=1.1)


def test_rejects_invalid_search_mode() -> None:
    with pytest.raises(ValueError, match="search_mode"):
        _hit(search_mode="roi_bgr")  # BGR is not a Phase 3 mode


def test_rejects_negative_capture_latency() -> None:
    with pytest.raises(ValueError, match="capture_latency"):
        _hit(capture_latency_ms=-1.0)


def test_rejects_negative_match_latency() -> None:
    with pytest.raises(ValueError, match="match_latency"):
        _hit(match_latency_ms=-0.5)


def test_rejects_empty_template_name() -> None:
    with pytest.raises(ValueError, match="template_name"):
        _hit(template_name="")


def test_rejects_found_with_missing_coords() -> None:
    """All coords None + found=True triggers the dedicated check."""
    with pytest.raises(ValueError, match="found=True"):
        MatchResult(
            found=True, confidence=0.95, template_name="t", search_mode="roi_gray",
            capture_latency_ms=0.0, match_latency_ms=0.0,
            x=None, y=None, width=None, height=None,
        )


def test_rejects_partial_coords() -> None:
    """Mixing None and int across coordinate fields is invalid."""
    with pytest.raises(ValueError, match="all-None or all-int"):
        _miss(x=10)  # x set but others None


def test_rejects_negative_coords() -> None:
    with pytest.raises(ValueError, match=r"x/y must be"):
        _hit(x=-1)


def test_rejects_zero_dimensions() -> None:
    with pytest.raises(ValueError, match=r"width/height"):
        _hit(width=0)


def test_rejects_non_bool_found() -> None:
    with pytest.raises(TypeError, match="found"):
        MatchResult(
            found=1, confidence=0.95, template_name="t", search_mode="roi_gray",  # type: ignore[arg-type]
            capture_latency_ms=0.0, match_latency_ms=0.0,
            x=0, y=0, width=10, height=10,
        )


def test_center_returns_midpoint_for_hit() -> None:
    r = _hit(x=100, y=200, width=110, height=110)
    assert r.center() == (155, 255)


def test_center_returns_none_for_miss() -> None:
    assert _miss().center() is None


def test_to_debug_dict_is_json_safe() -> None:
    r = _hit()
    encoded = json.dumps(r.to_debug_dict())
    decoded = json.loads(encoded)
    assert decoded["found"] is True
    assert decoded["confidence"] == 0.95
    assert decoded["center"] == [155, 255]


def test_to_debug_dict_miss() -> None:
    r = _miss()
    d = r.to_debug_dict()
    assert d["found"] is False
    assert d["center"] is None


def test_summary_hit_and_miss() -> None:
    assert "HIT" in _hit().summary()
    assert "MISS" in _miss().summary()


def test_match_result_is_frozen() -> None:
    r = _hit()
    with pytest.raises(Exception):  # FrozenInstanceError
        r.found = False  # type: ignore[misc]


def test_confidence_at_bounds_accepted() -> None:
    """[0, 1] inclusive."""
    # confidence=0.0 with found=False
    _miss(confidence=0.0)
    # confidence=1.0 with found=True
    _hit(confidence=1.0)
