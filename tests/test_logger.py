"""StructuredLogger tests — JSONL schema, atomic append, error paths."""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from automation.errors import LoggingError
from automation.logger import (
    ERROR_FIELDS,
    MAX_RECORD_BYTES,
    StructuredLogger,
    TICK_FIELDS,
)


_UTC = _dt.timezone.utc
_NOW = _dt.datetime(2026, 5, 21, 14, 0, 0, tzinfo=_UTC)


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


# ---- log_tick — happy path ---------------------------------------------------


def test_log_tick_writes_one_record(tmp_path: Path) -> None:
    log = StructuredLogger(logs_dir=tmp_path)
    log.log_tick(
        correlation_id="tick_20260521T140000_abc123",
        state_before="IDLE", state_after="IDLE", success=True,
        tick_latency_ms=1234.5, capture_latency_ms=940.0,
        match_latency_ms=2.3, action_latency_ms=58.0,
        retries_used=0, ts=_NOW,
    )
    records = _read_jsonl(tmp_path / "ticks.jsonl")
    assert len(records) == 1
    r = records[0]
    assert set(r.keys()) >= TICK_FIELDS  # may include extra fields


def test_log_tick_required_fields_present(tmp_path: Path) -> None:
    log = StructuredLogger(logs_dir=tmp_path)
    log.log_tick(
        correlation_id="tick_20260521T140000_abc123",
        state_before="IDLE", state_after="FAILED", success=False,
        tick_latency_ms=950.0, capture_latency_ms=940.0,
        match_latency_ms=2.3, action_latency_ms=None,
        retries_used=0, ts=_NOW,
    )
    r = _read_jsonl(tmp_path / "ticks.jsonl")[0]
    for k in TICK_FIELDS:
        assert k in r, f"missing required field: {k}"
    assert r["action_latency_ms"] is None
    assert r["success"] is False
    assert r["ts"] == _NOW.isoformat()


def test_log_tick_multiple_appends(tmp_path: Path) -> None:
    log = StructuredLogger(logs_dir=tmp_path)
    for i in range(5):
        log.log_tick(
            correlation_id=f"tick_20260521T140000_a0000{i}",
            state_before="IDLE", state_after="IDLE", success=True,
            tick_latency_ms=1000.0 + i, capture_latency_ms=900.0,
            match_latency_ms=2.0, action_latency_ms=50.0,
            retries_used=0, ts=_NOW,
        )
    records = _read_jsonl(tmp_path / "ticks.jsonl")
    assert len(records) == 5
    assert [r["tick_latency_ms"] for r in records] == [
        1000.0, 1001.0, 1002.0, 1003.0, 1004.0,
    ]


def test_log_tick_default_ts_is_now(tmp_path: Path) -> None:
    log = StructuredLogger(logs_dir=tmp_path)
    before = _dt.datetime.now(tz=_UTC)
    log.log_tick(
        correlation_id="tick_20260521T140000_abcdef",
        state_before="IDLE", state_after="IDLE", success=True,
        tick_latency_ms=1.0, capture_latency_ms=1.0,
        match_latency_ms=1.0, action_latency_ms=1.0,
        retries_used=0,
    )
    after = _dt.datetime.now(tz=_UTC)
    r = _read_jsonl(tmp_path / "ticks.jsonl")[0]
    ts = _dt.datetime.fromisoformat(r["ts"])
    assert before <= ts <= after


# ---- log_tick — extra fields -------------------------------------------------


def test_log_tick_extra_merged(tmp_path: Path) -> None:
    log = StructuredLogger(logs_dir=tmp_path)
    log.log_tick(
        correlation_id="tick_20260521T140000_aaaaaa",
        state_before="IDLE", state_after="IDLE", success=True,
        tick_latency_ms=2000.0, capture_latency_ms=940.0,
        match_latency_ms=50.0, action_latency_ms=60.0,
        retries_used=1, ts=_NOW,
        extra={"tier": "validated_retry", "template": "demo"},
    )
    r = _read_jsonl(tmp_path / "ticks.jsonl")[0]
    assert r["tier"] == "validated_retry"
    assert r["template"] == "demo"


def test_log_tick_extra_collides_with_required(tmp_path: Path) -> None:
    log = StructuredLogger(logs_dir=tmp_path)
    with pytest.raises(LoggingError, match="collide with required"):
        log.log_tick(
            correlation_id="tick_20260521T140000_aaaaaa",
            state_before="IDLE", state_after="IDLE", success=True,
            tick_latency_ms=1.0, capture_latency_ms=1.0,
            match_latency_ms=1.0, action_latency_ms=1.0,
            retries_used=0, ts=_NOW,
            extra={"correlation_id": "tick_other"},  # collision!
        )


# ---- log_error ---------------------------------------------------------------


def test_log_error_writes_one_record(tmp_path: Path) -> None:
    log = StructuredLogger(logs_dir=tmp_path)
    log.log_error(
        correlation_id="tick_20260521T140000_abcdef",
        error_type="ADBError",
        message="device disconnected",
        state="ACTING",
        ts=_NOW,
    )
    r = _read_jsonl(tmp_path / "errors.jsonl")[0]
    for k in ERROR_FIELDS:
        assert k in r
    assert r["error_type"] == "ADBError"
    assert r["state"] == "ACTING"


def test_log_error_extra_merged(tmp_path: Path) -> None:
    log = StructuredLogger(logs_dir=tmp_path)
    log.log_error(
        correlation_id="tick_20260521T140000_aaaaaa",
        error_type="MetricsError",
        message="bucket boundary error",
        state="VALIDATING",
        ts=_NOW,
        extra={"bucket": "tap", "value": -1},
    )
    r = _read_jsonl(tmp_path / "errors.jsonl")[0]
    assert r["bucket"] == "tap"
    assert r["value"] == -1


def test_log_error_extra_collision_rejected(tmp_path: Path) -> None:
    log = StructuredLogger(logs_dir=tmp_path)
    with pytest.raises(LoggingError, match="collide with required"):
        log.log_error(
            correlation_id="tick_20260521T140000_aaaaaa",
            error_type="X", message="m", state="IDLE", ts=_NOW,
            extra={"state": "OTHER"},
        )


# ---- error paths -------------------------------------------------------------


def test_non_json_encodable_payload_raises(tmp_path: Path) -> None:
    log = StructuredLogger(logs_dir=tmp_path)
    class _NotJson:
        pass
    with pytest.raises(LoggingError, match="JSON-encode"):
        log.log_tick(
            correlation_id="tick_20260521T140000_aaaaaa",
            state_before="IDLE", state_after="IDLE", success=True,
            tick_latency_ms=1.0, capture_latency_ms=1.0,
            match_latency_ms=1.0, action_latency_ms=1.0,
            retries_used=0, ts=_NOW,
            extra={"not_serializable": _NotJson()},  # type: ignore[dict-item]
        )


def test_oversized_payload_raises(tmp_path: Path) -> None:
    """A record exceeding MAX_RECORD_BYTES must raise rather than risk
    interleaving with concurrent writers."""
    log = StructuredLogger(logs_dir=tmp_path)
    huge = "x" * (MAX_RECORD_BYTES * 2)
    with pytest.raises(LoggingError, match="exceeds"):
        log.log_tick(
            correlation_id="tick_20260521T140000_aaaaaa",
            state_before="IDLE", state_after="IDLE", success=True,
            tick_latency_ms=1.0, capture_latency_ms=1.0,
            match_latency_ms=1.0, action_latency_ms=1.0,
            retries_used=0, ts=_NOW,
            extra={"blob": huge},
        )


def test_naive_timestamp_rejected(tmp_path: Path) -> None:
    log = StructuredLogger(logs_dir=tmp_path)
    naive = _dt.datetime(2026, 5, 21, 14, 0, 0)
    with pytest.raises(LoggingError, match="timezone-aware"):
        log.log_tick(
            correlation_id="tick_20260521T140000_aaaaaa",
            state_before="IDLE", state_after="IDLE", success=True,
            tick_latency_ms=1.0, capture_latency_ms=1.0,
            match_latency_ms=1.0, action_latency_ms=1.0,
            retries_used=0, ts=naive,
        )


# ---- atomic append -----------------------------------------------------------


def test_each_record_is_one_line(tmp_path: Path) -> None:
    """JSONL: one record == one line, no embedded newlines."""
    log = StructuredLogger(logs_dir=tmp_path)
    for i in range(3):
        log.log_tick(
            correlation_id=f"tick_20260521T140000_a0000{i}",
            state_before="IDLE", state_after="IDLE", success=True,
            tick_latency_ms=float(i), capture_latency_ms=0.0,
            match_latency_ms=0.0, action_latency_ms=0.0,
            retries_used=0, ts=_NOW,
        )
    lines = (tmp_path / "ticks.jsonl").read_text().splitlines()
    assert len(lines) == 3
    for line in lines:
        json.loads(line)  # each is parseable on its own


def test_ticks_and_errors_go_to_separate_files(tmp_path: Path) -> None:
    log = StructuredLogger(logs_dir=tmp_path)
    log.log_tick(
        correlation_id="tick_20260521T140000_aaaaaa",
        state_before="IDLE", state_after="IDLE", success=True,
        tick_latency_ms=1.0, capture_latency_ms=1.0,
        match_latency_ms=1.0, action_latency_ms=1.0,
        retries_used=0, ts=_NOW,
    )
    log.log_error(
        correlation_id="tick_20260521T140000_aaaaaa",
        error_type="X", message="m", state="IDLE", ts=_NOW,
    )
    assert (tmp_path / "ticks.jsonl").is_file()
    assert (tmp_path / "errors.jsonl").is_file()
    assert len(_read_jsonl(tmp_path / "ticks.jsonl")) == 1
    assert len(_read_jsonl(tmp_path / "errors.jsonl")) == 1


def test_io_failure_does_not_raise(tmp_path: Path) -> None:
    """If the destination is unwritable, the logger logs a WARN and
    continues — the framework must not crash on observability faults."""
    bad = tmp_path / "does" / "not" / "exist"  # parents won't exist
    # Use a path that mkdir cannot create (a file in the way).
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    log = StructuredLogger(logs_dir=blocker)  # blocker is a file
    # Must not raise; the WARN is captured by the stdlib logger.
    log.log_tick(
        correlation_id="tick_20260521T140000_aaaaaa",
        state_before="IDLE", state_after="IDLE", success=True,
        tick_latency_ms=1.0, capture_latency_ms=1.0,
        match_latency_ms=1.0, action_latency_ms=1.0,
        retries_used=0, ts=_NOW,
    )
