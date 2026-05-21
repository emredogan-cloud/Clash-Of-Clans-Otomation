"""ActionResult container tests.

All tests are deterministic — no randomness, no I/O.
"""
from __future__ import annotations

import datetime as _dt
import json

import pytest

from automation.action_result import ActionResult


def _now() -> _dt.datetime:
    return _dt.datetime(2026, 5, 21, 12, 0, 0, tzinfo=_dt.timezone.utc)


# ---- happy-path construction --------------------------------------------------


def test_construct_tap_result() -> None:
    r = ActionResult(
        success=True,
        action_type="tap",
        latency_ms=42.5,
        device_x=540,
        device_y=1200,
        ts=_now(),
    )
    assert r.success is True
    assert r.action_type == "tap"
    assert r.latency_ms == 42.5
    assert r.device_x == 540
    assert r.device_y == 1200
    assert r.ts == _now()


def test_construct_swipe_result() -> None:
    r = ActionResult(
        success=True, action_type="swipe", latency_ms=320.0,
        device_x=100, device_y=200, ts=_now(),
    )
    assert r.action_type == "swipe"


def test_construct_long_press_result() -> None:
    r = ActionResult(
        success=False, action_type="long_press", latency_ms=10.0,
        device_x=540, device_y=1200, ts=_now(),
    )
    assert r.action_type == "long_press"
    assert r.success is False


# ---- validation --------------------------------------------------------------


def test_action_type_must_be_known() -> None:
    with pytest.raises(ValueError, match="action_type must be one of"):
        ActionResult(success=True, action_type="key", latency_ms=1.0,
                     device_x=0, device_y=0, ts=_now())


def test_action_type_must_be_string() -> None:
    with pytest.raises(ValueError):
        ActionResult(success=True, action_type="", latency_ms=1.0,
                     device_x=0, device_y=0, ts=_now())


def test_success_must_be_bool() -> None:
    with pytest.raises(TypeError, match="success must be bool"):
        ActionResult(success=1, action_type="tap", latency_ms=1.0,  # type: ignore[arg-type]
                     device_x=0, device_y=0, ts=_now())


def test_latency_must_be_nonnegative() -> None:
    with pytest.raises(ValueError, match="latency_ms must be >= 0"):
        ActionResult(success=True, action_type="tap", latency_ms=-0.001,
                     device_x=0, device_y=0, ts=_now())


def test_latency_zero_is_valid() -> None:
    r = ActionResult(success=True, action_type="tap", latency_ms=0.0,
                     device_x=0, device_y=0, ts=_now())
    assert r.latency_ms == 0.0


def test_latency_must_be_numeric() -> None:
    with pytest.raises(TypeError, match="latency_ms must be a number"):
        ActionResult(success=True, action_type="tap",
                     latency_ms="100",  # type: ignore[arg-type]
                     device_x=0, device_y=0, ts=_now())


def test_device_x_and_y_must_both_be_set_or_neither() -> None:
    with pytest.raises(ValueError, match="both-None or both-int"):
        ActionResult(success=True, action_type="tap", latency_ms=1.0,
                     device_x=100, device_y=None, ts=_now())


def test_device_coords_must_be_nonnegative() -> None:
    with pytest.raises(ValueError, match="must be >= 0"):
        ActionResult(success=True, action_type="tap", latency_ms=1.0,
                     device_x=-1, device_y=0, ts=_now())


def test_phase4_action_requires_coords() -> None:
    """tap / swipe / long_press all require non-None coords."""
    with pytest.raises(ValueError, match="requires device_x and device_y"):
        ActionResult(success=True, action_type="tap", latency_ms=1.0,
                     device_x=None, device_y=None, ts=_now())


def test_device_x_bool_rejected() -> None:
    """bool is a subclass of int; ActionResult must reject it explicitly."""
    with pytest.raises(TypeError, match="device_x must be int"):
        ActionResult(success=True, action_type="tap", latency_ms=1.0,
                     device_x=True,  # type: ignore[arg-type]
                     device_y=0, ts=_now())


def test_device_y_bool_rejected() -> None:
    with pytest.raises(TypeError, match="device_y must be int"):
        ActionResult(success=True, action_type="tap", latency_ms=1.0,
                     device_x=0,
                     device_y=False,  # type: ignore[arg-type]
                     ts=_now())


def test_ts_must_be_datetime() -> None:
    with pytest.raises(TypeError, match="ts must be datetime"):
        ActionResult(success=True, action_type="tap", latency_ms=1.0,
                     device_x=0, device_y=0,
                     ts="2026-05-21T00:00:00Z")  # type: ignore[arg-type]


def test_ts_must_be_timezone_aware() -> None:
    naive = _dt.datetime(2026, 5, 21, 12, 0, 0)
    with pytest.raises(ValueError, match="must be timezone-aware"):
        ActionResult(success=True, action_type="tap", latency_ms=1.0,
                     device_x=0, device_y=0, ts=naive)


# ---- immutability -----------------------------------------------------------


def test_frozen_dataclass_rejects_mutation() -> None:
    r = ActionResult(success=True, action_type="tap", latency_ms=1.0,
                     device_x=0, device_y=0, ts=_now())
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
        r.success = False  # type: ignore[misc]
    with pytest.raises(Exception):
        r.latency_ms = 99.0  # type: ignore[misc]


def test_action_result_is_hashable() -> None:
    """Identical primitive fields → equal dataclasses → hashable."""
    r1 = ActionResult(success=True, action_type="tap", latency_ms=1.0,
                      device_x=0, device_y=0, ts=_now())
    r2 = ActionResult(success=True, action_type="tap", latency_ms=1.0,
                      device_x=0, device_y=0, ts=_now())
    assert r1 == r2
    assert hash(r1) == hash(r2)


# ---- debug dict --------------------------------------------------------------


def test_to_debug_dict_is_json_safe() -> None:
    r = ActionResult(success=True, action_type="swipe", latency_ms=42.0,
                     device_x=100, device_y=200, ts=_now())
    blob = r.to_debug_dict()
    encoded = json.dumps(blob)
    decoded = json.loads(encoded)
    assert decoded["success"] is True
    assert decoded["action_type"] == "swipe"
    assert decoded["device_x"] == 100
    assert decoded["device_y"] == 200
    assert decoded["latency_ms"] == 42.0
    assert decoded["ts"] == _now().isoformat()


def test_to_debug_dict_keys() -> None:
    r = ActionResult(success=False, action_type="long_press", latency_ms=1.0,
                     device_x=0, device_y=0, ts=_now())
    keys = set(r.to_debug_dict().keys())
    assert keys == {
        "success", "action_type", "latency_ms",
        "device_x", "device_y", "ts",
    }


def test_summary_includes_status_and_coords() -> None:
    ok = ActionResult(success=True, action_type="tap", latency_ms=12.34,
                      device_x=540, device_y=1200, ts=_now())
    fail = ActionResult(success=False, action_type="swipe", latency_ms=99.9,
                        device_x=1, device_y=2, ts=_now())
    assert "OK" in ok.summary() and "tap" in ok.summary()
    assert "(540,1200)" in ok.summary()
    assert "FAIL" in fail.summary() and "swipe" in fail.summary()
