"""RuntimeHealth container tests."""
from __future__ import annotations

import datetime as _dt
import json

import pytest

from automation.runtime_health import RuntimeHealth


_UTC = _dt.timezone.utc
_NOW = _dt.datetime(2026, 5, 21, 15, 0, 0, tzinfo=_UTC)


# ---- construction -----------------------------------------------------------


def test_healthy_factory_produces_clean_snapshot() -> None:
    h = RuntimeHealth.healthy(ts=_NOW)
    assert h.sensor_ok and h.matcher_ok and h.actuator_ok and h.orchestrator_ok
    assert h.last_error is None
    assert h.degraded is False
    assert h.ts == _NOW


def test_healthy_factory_uses_now_when_ts_omitted() -> None:
    before = _dt.datetime.now(tz=_UTC)
    h = RuntimeHealth.healthy()
    after = _dt.datetime.now(tz=_UTC)
    assert before <= h.ts <= after


def test_construct_degraded_with_one_subsystem_unhealthy() -> None:
    h = RuntimeHealth(
        sensor_ok=False, matcher_ok=True, actuator_ok=True,
        orchestrator_ok=True, last_error="CaptureError: bad",
        degraded=True, ts=_NOW,
    )
    assert h.sensor_ok is False
    assert h.degraded is True


def test_construct_degraded_with_last_error_only() -> None:
    """An error-only degraded state (all subsystems still OK)."""
    h = RuntimeHealth(
        sensor_ok=True, matcher_ok=True, actuator_ok=True,
        orchestrator_ok=True, last_error="benign warning",
        degraded=True, ts=_NOW,
    )
    assert h.degraded is True
    assert h.last_error == "benign warning"


# ---- validation: type checks ------------------------------------------------


def test_sensor_ok_must_be_bool() -> None:
    with pytest.raises(TypeError, match="sensor_ok must be bool"):
        RuntimeHealth(
            sensor_ok=1,  # type: ignore[arg-type]
            matcher_ok=True, actuator_ok=True, orchestrator_ok=True,
            last_error=None, degraded=False, ts=_NOW,
        )


def test_last_error_must_be_str_or_none() -> None:
    with pytest.raises(TypeError, match="last_error must be str or None"):
        RuntimeHealth(
            sensor_ok=True, matcher_ok=True, actuator_ok=True, orchestrator_ok=True,
            last_error=123,  # type: ignore[arg-type]
            degraded=False, ts=_NOW,
        )


def test_ts_must_be_datetime() -> None:
    with pytest.raises(TypeError, match="ts must be datetime"):
        RuntimeHealth(
            sensor_ok=True, matcher_ok=True, actuator_ok=True, orchestrator_ok=True,
            last_error=None, degraded=False, ts="2026-05-21T15:00:00",  # type: ignore[arg-type]
        )


def test_ts_must_be_tz_aware() -> None:
    naive = _dt.datetime(2026, 5, 21, 15, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        RuntimeHealth(
            sensor_ok=True, matcher_ok=True, actuator_ok=True, orchestrator_ok=True,
            last_error=None, degraded=False, ts=naive,
        )


# ---- validation: degraded coupling -----------------------------------------


def test_unhealthy_subsystem_without_degraded_flag_rejected() -> None:
    with pytest.raises(ValueError, match="degraded must be True"):
        RuntimeHealth(
            sensor_ok=False, matcher_ok=True, actuator_ok=True, orchestrator_ok=True,
            last_error="x", degraded=False, ts=_NOW,
        )


def test_last_error_without_degraded_flag_rejected() -> None:
    with pytest.raises(ValueError, match="degraded must be True"):
        RuntimeHealth(
            sensor_ok=True, matcher_ok=True, actuator_ok=True, orchestrator_ok=True,
            last_error="something", degraded=False, ts=_NOW,
        )


def test_degraded_true_without_evidence_rejected() -> None:
    """Claiming degraded with no underlying cause is misleading."""
    with pytest.raises(ValueError, match="requires either an"):
        RuntimeHealth(
            sensor_ok=True, matcher_ok=True, actuator_ok=True, orchestrator_ok=True,
            last_error=None, degraded=True, ts=_NOW,
        )


def test_empty_string_last_error_is_treated_as_no_error() -> None:
    """An empty string is not a real error — degraded=False should be allowed
    only if every subsystem is also healthy. degraded=True with empty error
    and all subsystems healthy must fail."""
    with pytest.raises(ValueError, match="requires either an"):
        RuntimeHealth(
            sensor_ok=True, matcher_ok=True, actuator_ok=True, orchestrator_ok=True,
            last_error="", degraded=True, ts=_NOW,
        )


# ---- immutability ----------------------------------------------------------


def test_runtime_health_is_frozen() -> None:
    h = RuntimeHealth.healthy(ts=_NOW)
    with pytest.raises(Exception):
        h.degraded = True  # type: ignore[misc]
    with pytest.raises(Exception):
        h.sensor_ok = False  # type: ignore[misc]


def test_runtime_health_is_hashable() -> None:
    a = RuntimeHealth.healthy(ts=_NOW)
    b = RuntimeHealth.healthy(ts=_NOW)
    assert a == b
    assert hash(a) == hash(b)


# ---- to_debug_dict ----------------------------------------------------------


def test_to_debug_dict_is_json_safe() -> None:
    h = RuntimeHealth(
        sensor_ok=False, matcher_ok=True, actuator_ok=True, orchestrator_ok=True,
        last_error="CaptureError: bad", degraded=True, ts=_NOW,
    )
    blob = h.to_debug_dict()
    decoded = json.loads(json.dumps(blob))
    assert decoded["sensor_ok"] is False
    assert decoded["degraded"] is True
    assert decoded["last_error"] == "CaptureError: bad"
    assert decoded["ts"] == _NOW.isoformat()


def test_to_debug_dict_keys() -> None:
    h = RuntimeHealth.healthy(ts=_NOW)
    assert set(h.to_debug_dict().keys()) == {
        "sensor_ok", "matcher_ok", "actuator_ok", "orchestrator_ok",
        "last_error", "degraded", "ts",
    }


def test_summary_shows_healthy() -> None:
    s = RuntimeHealth.healthy(ts=_NOW).summary()
    assert "HEALTHY" in s
    assert "—" in s  # no impacted subsystems


def test_summary_lists_impacted_subsystems() -> None:
    h = RuntimeHealth(
        sensor_ok=False, matcher_ok=True, actuator_ok=False, orchestrator_ok=True,
        last_error="ADBError: pipe", degraded=True, ts=_NOW,
    )
    s = h.summary()
    assert "DEGRADED" in s
    assert "sensor" in s and "actuator" in s
    assert "ADBError" in s
