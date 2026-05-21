"""TickResult container tests.

Pure validation / immutability / JSON-safety tests. No orchestrator.
"""
from __future__ import annotations

import datetime as _dt
import json

import pytest

from automation.state import State
from automation.tick_result import TickResult


def _now() -> _dt.datetime:
    return _dt.datetime(2026, 5, 21, 13, 0, 0, tzinfo=_dt.timezone.utc)


# ---- happy-path construction --------------------------------------------------


def test_construct_successful_tick() -> None:
    r = TickResult(
        state_before=State.IDLE,
        state_after=State.IDLE,
        success=True,
        tick_latency_ms=1300.0,
        capture_latency_ms=940.0,
        match_latency_ms=2.3,
        action_latency_ms=58.0,
        ts=_now(),
    )
    assert r.success is True
    assert r.state_before is State.IDLE
    assert r.state_after is State.IDLE
    assert r.tick_latency_ms == 1300.0


def test_construct_failed_tick_with_action() -> None:
    r = TickResult(
        state_before=State.IDLE,
        state_after=State.FAILED,
        success=False,
        tick_latency_ms=2200.0,
        capture_latency_ms=940.0,
        match_latency_ms=2.3,
        action_latency_ms=60.0,
        ts=_now(),
    )
    assert r.success is False
    assert r.state_after is State.FAILED
    assert r.action_latency_ms == 60.0


def test_construct_failed_tick_without_action() -> None:
    """A SEARCH-miss tick has no action latency."""
    r = TickResult(
        state_before=State.IDLE,
        state_after=State.FAILED,
        success=False,
        tick_latency_ms=950.0,
        capture_latency_ms=940.0,
        match_latency_ms=2.3,
        action_latency_ms=None,
        ts=_now(),
    )
    assert r.action_latency_ms is None


# ---- validation: states ------------------------------------------------------


def test_state_before_must_be_state() -> None:
    with pytest.raises(TypeError, match="state_before must be State"):
        TickResult(
            state_before="IDLE",  # type: ignore[arg-type]
            state_after=State.IDLE, success=True,
            tick_latency_ms=1.0, capture_latency_ms=0.0,
            match_latency_ms=0.0, action_latency_ms=None, ts=_now(),
        )


def test_state_after_must_be_state() -> None:
    with pytest.raises(TypeError, match="state_after must be State"):
        TickResult(
            state_before=State.IDLE,
            state_after="IDLE",  # type: ignore[arg-type]
            success=True,
            tick_latency_ms=1.0, capture_latency_ms=0.0,
            match_latency_ms=0.0, action_latency_ms=None, ts=_now(),
        )


def test_success_must_be_bool() -> None:
    with pytest.raises(TypeError, match="success must be bool"):
        TickResult(
            state_before=State.IDLE, state_after=State.IDLE,
            success=1,  # type: ignore[arg-type]
            tick_latency_ms=1.0, capture_latency_ms=0.0,
            match_latency_ms=0.0, action_latency_ms=None, ts=_now(),
        )


# ---- validation: latencies ---------------------------------------------------


def test_tick_latency_must_be_nonnegative() -> None:
    with pytest.raises(ValueError, match="tick_latency_ms must be >= 0"):
        TickResult(
            state_before=State.IDLE, state_after=State.IDLE, success=True,
            tick_latency_ms=-1.0, capture_latency_ms=0.0,
            match_latency_ms=0.0, action_latency_ms=None, ts=_now(),
        )


def test_capture_latency_must_be_nonnegative() -> None:
    with pytest.raises(ValueError, match="capture_latency_ms must be >= 0"):
        TickResult(
            state_before=State.IDLE, state_after=State.IDLE, success=True,
            tick_latency_ms=1.0, capture_latency_ms=-0.1,
            match_latency_ms=0.0, action_latency_ms=None, ts=_now(),
        )


def test_match_latency_must_be_nonnegative() -> None:
    with pytest.raises(ValueError, match="match_latency_ms must be >= 0"):
        TickResult(
            state_before=State.IDLE, state_after=State.IDLE, success=True,
            tick_latency_ms=1.0, capture_latency_ms=0.0,
            match_latency_ms=-0.5, action_latency_ms=None, ts=_now(),
        )


def test_action_latency_must_be_nonnegative_when_set() -> None:
    with pytest.raises(ValueError, match="action_latency_ms must be >= 0"):
        TickResult(
            state_before=State.IDLE, state_after=State.IDLE, success=True,
            tick_latency_ms=1.0, capture_latency_ms=0.0,
            match_latency_ms=0.0, action_latency_ms=-1.0, ts=_now(),
        )


def test_action_latency_may_be_none() -> None:
    r = TickResult(
        state_before=State.IDLE, state_after=State.FAILED, success=False,
        tick_latency_ms=1.0, capture_latency_ms=0.0,
        match_latency_ms=0.0, action_latency_ms=None, ts=_now(),
    )
    assert r.action_latency_ms is None


def test_zero_latencies_are_valid() -> None:
    r = TickResult(
        state_before=State.IDLE, state_after=State.IDLE, success=True,
        tick_latency_ms=0.0, capture_latency_ms=0.0,
        match_latency_ms=0.0, action_latency_ms=0.0, ts=_now(),
    )
    assert r.tick_latency_ms == 0.0


def test_latency_must_be_numeric() -> None:
    with pytest.raises(TypeError, match="tick_latency_ms must be a number"):
        TickResult(
            state_before=State.IDLE, state_after=State.IDLE, success=True,
            tick_latency_ms="1.0",  # type: ignore[arg-type]
            capture_latency_ms=0.0,
            match_latency_ms=0.0, action_latency_ms=None, ts=_now(),
        )


def test_action_latency_must_be_numeric_or_none() -> None:
    with pytest.raises(TypeError, match="action_latency_ms must be number or None"):
        TickResult(
            state_before=State.IDLE, state_after=State.IDLE, success=True,
            tick_latency_ms=1.0, capture_latency_ms=0.0,
            match_latency_ms=0.0,
            action_latency_ms="1.0",  # type: ignore[arg-type]
            ts=_now(),
        )


def test_tick_latency_rejects_bool() -> None:
    """bool is subclass of int; must be excluded explicitly."""
    with pytest.raises(TypeError, match="tick_latency_ms must be a number"):
        TickResult(
            state_before=State.IDLE, state_after=State.IDLE, success=True,
            tick_latency_ms=True,  # type: ignore[arg-type]
            capture_latency_ms=0.0,
            match_latency_ms=0.0, action_latency_ms=None, ts=_now(),
        )


def test_action_latency_rejects_bool() -> None:
    with pytest.raises(TypeError, match="action_latency_ms must be number or None"):
        TickResult(
            state_before=State.IDLE, state_after=State.IDLE, success=True,
            tick_latency_ms=1.0, capture_latency_ms=0.0,
            match_latency_ms=0.0,
            action_latency_ms=False,  # type: ignore[arg-type]
            ts=_now(),
        )


# ---- validation: success ↔ state_after coupling -----------------------------


def test_success_true_requires_state_after_idle() -> None:
    with pytest.raises(ValueError, match="success=True requires state_after=IDLE"):
        TickResult(
            state_before=State.IDLE,
            state_after=State.FAILED,
            success=True,
            tick_latency_ms=1.0, capture_latency_ms=0.0,
            match_latency_ms=0.0, action_latency_ms=None, ts=_now(),
        )


def test_success_false_requires_state_after_failed() -> None:
    with pytest.raises(ValueError, match="success=False requires state_after=FAILED"):
        TickResult(
            state_before=State.IDLE,
            state_after=State.IDLE,
            success=False,
            tick_latency_ms=1.0, capture_latency_ms=0.0,
            match_latency_ms=0.0, action_latency_ms=None, ts=_now(),
        )


# ---- validation: ts ----------------------------------------------------------


def test_ts_must_be_datetime() -> None:
    with pytest.raises(TypeError, match="ts must be datetime"):
        TickResult(
            state_before=State.IDLE, state_after=State.IDLE, success=True,
            tick_latency_ms=1.0, capture_latency_ms=0.0,
            match_latency_ms=0.0, action_latency_ms=None,
            ts="2026-05-21T00:00:00Z",  # type: ignore[arg-type]
        )


def test_ts_must_be_timezone_aware() -> None:
    naive = _dt.datetime(2026, 5, 21, 12, 0, 0)
    with pytest.raises(ValueError, match="must be timezone-aware"):
        TickResult(
            state_before=State.IDLE, state_after=State.IDLE, success=True,
            tick_latency_ms=1.0, capture_latency_ms=0.0,
            match_latency_ms=0.0, action_latency_ms=None, ts=naive,
        )


# ---- immutability ------------------------------------------------------------


def test_frozen_rejects_mutation() -> None:
    r = TickResult(
        state_before=State.IDLE, state_after=State.IDLE, success=True,
        tick_latency_ms=1.0, capture_latency_ms=0.0,
        match_latency_ms=0.0, action_latency_ms=None, ts=_now(),
    )
    with pytest.raises(Exception):
        r.success = False  # type: ignore[misc]


def test_tick_result_is_hashable() -> None:
    r1 = TickResult(
        state_before=State.IDLE, state_after=State.IDLE, success=True,
        tick_latency_ms=1.0, capture_latency_ms=0.0,
        match_latency_ms=0.0, action_latency_ms=None, ts=_now(),
    )
    r2 = TickResult(
        state_before=State.IDLE, state_after=State.IDLE, success=True,
        tick_latency_ms=1.0, capture_latency_ms=0.0,
        match_latency_ms=0.0, action_latency_ms=None, ts=_now(),
    )
    assert r1 == r2
    assert hash(r1) == hash(r2)


# ---- debug dict --------------------------------------------------------------


def test_to_debug_dict_is_json_safe() -> None:
    r = TickResult(
        state_before=State.IDLE, state_after=State.IDLE, success=True,
        tick_latency_ms=1300.5, capture_latency_ms=940.0,
        match_latency_ms=2.3, action_latency_ms=58.0, ts=_now(),
    )
    blob = r.to_debug_dict()
    decoded = json.loads(json.dumps(blob))
    assert decoded["state_before"] == "IDLE"
    assert decoded["state_after"] == "IDLE"
    assert decoded["success"] is True
    assert decoded["tick_latency_ms"] == 1300.5
    assert decoded["action_latency_ms"] == 58.0
    assert decoded["ts"] == _now().isoformat()


def test_to_debug_dict_handles_none_action_latency() -> None:
    r = TickResult(
        state_before=State.IDLE, state_after=State.FAILED, success=False,
        tick_latency_ms=950.0, capture_latency_ms=940.0,
        match_latency_ms=2.3, action_latency_ms=None, ts=_now(),
    )
    blob = r.to_debug_dict()
    decoded = json.loads(json.dumps(blob))
    assert decoded["action_latency_ms"] is None
    assert decoded["state_after"] == "FAILED"


def test_to_debug_dict_keys() -> None:
    r = TickResult(
        state_before=State.IDLE, state_after=State.IDLE, success=True,
        tick_latency_ms=1.0, capture_latency_ms=0.0,
        match_latency_ms=0.0, action_latency_ms=0.0, ts=_now(),
    )
    assert set(r.to_debug_dict().keys()) == {
        "state_before", "state_after", "success",
        "tick_latency_ms", "capture_latency_ms",
        "match_latency_ms", "action_latency_ms", "ts",
    }


# ---- summary -----------------------------------------------------------------


def test_summary_shows_ok_for_success() -> None:
    r = TickResult(
        state_before=State.IDLE, state_after=State.IDLE, success=True,
        tick_latency_ms=1300.0, capture_latency_ms=940.0,
        match_latency_ms=2.3, action_latency_ms=58.0, ts=_now(),
    )
    s = r.summary()
    assert "OK" in s
    assert "IDLE→IDLE" in s


def test_summary_shows_fail_for_failure() -> None:
    r = TickResult(
        state_before=State.IDLE, state_after=State.FAILED, success=False,
        tick_latency_ms=950.0, capture_latency_ms=940.0,
        match_latency_ms=2.3, action_latency_ms=None, ts=_now(),
    )
    s = r.summary()
    assert "FAIL" in s
    assert "IDLE→FAILED" in s
    assert "—" in s  # no action latency
