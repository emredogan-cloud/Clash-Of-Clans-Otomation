"""HeartbeatWriter tests — atomic write, schema, overwrite, validation."""
from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pytest

from automation.errors import HeartbeatError
from watchdog.heartbeat import (
    HEARTBEAT_SCHEMA_VERSION,
    REQUIRED_FIELDS,
    HeartbeatWriter,
)


_UTC = _dt.timezone.utc
_NOW = _dt.datetime(2026, 5, 21, 16, 0, 0, tzinfo=_UTC)


# ---- helpers ---------------------------------------------------------------


@dataclass
class _MockHealth:
    """Minimal RuntimeHealth-shaped object for tests."""

    payload: dict[str, Any]
    raise_on_to_debug_dict: bool = False

    def to_debug_dict(self) -> Mapping[str, Any]:
        if self.raise_on_to_debug_dict:
            raise RuntimeError("synthetic to_debug_dict failure")
        return self.payload


def _healthy_payload() -> dict[str, Any]:
    return {
        "sensor_ok": True, "matcher_ok": True, "actuator_ok": True,
        "orchestrator_ok": True, "last_error": None, "degraded": False,
        "ts": _NOW.isoformat(),
    }


def _degraded_payload(err: str = "test fault") -> dict[str, Any]:
    return {
        "sensor_ok": False, "matcher_ok": True, "actuator_ok": True,
        "orchestrator_ok": True, "last_error": err, "degraded": True,
        "ts": _NOW.isoformat(),
    }


# ---- construction validation ----------------------------------------------


def test_construct_requires_path_instance() -> None:
    with pytest.raises(TypeError, match="must be Path"):
        HeartbeatWriter("var/watchdog/heartbeat.json")  # type: ignore[arg-type]


def test_constants_match_module_surface() -> None:
    assert HEARTBEAT_SCHEMA_VERSION == 1
    assert "ts" in REQUIRED_FIELDS
    assert "correlation_id" in REQUIRED_FIELDS
    assert "degraded" in REQUIRED_FIELDS
    assert "health" in REQUIRED_FIELDS
    assert "pid" in REQUIRED_FIELDS
    assert "schema_version" in REQUIRED_FIELDS


# ---- beat: happy path ------------------------------------------------------


def test_beat_writes_required_fields(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    hb = HeartbeatWriter(path)
    hb.beat("tick_20260521T160000_abcdef", _MockHealth(_healthy_payload()),
            ts=_NOW)
    payload = json.loads(path.read_text())
    assert set(payload.keys()) >= REQUIRED_FIELDS
    assert payload["schema_version"] == HEARTBEAT_SCHEMA_VERSION
    assert payload["ts"] == _NOW.isoformat()
    assert payload["correlation_id"] == "tick_20260521T160000_abcdef"
    assert payload["degraded"] is False
    assert payload["health"]["sensor_ok"] is True
    assert payload["pid"] == os.getpid()


def test_beat_carries_degraded_flag_when_health_degraded(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    hb = HeartbeatWriter(path)
    hb.beat("tick_x_aaaaaa", _MockHealth(_degraded_payload("ADBError: x")))
    payload = json.loads(path.read_text())
    assert payload["degraded"] is True
    assert payload["health"]["last_error"] == "ADBError: x"


def test_beat_default_ts_is_recent(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    hb = HeartbeatWriter(path)
    before = _dt.datetime.now(tz=_UTC)
    hb.beat("tick_x_aaaaaa", _MockHealth(_healthy_payload()))
    after = _dt.datetime.now(tz=_UTC)
    payload = json.loads(path.read_text())
    ts = _dt.datetime.fromisoformat(payload["ts"])
    assert before <= ts <= after


def test_beat_creates_parent_directory(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "deep" / "heartbeat.json"
    hb = HeartbeatWriter(path)
    hb.beat("tick_x_aaaaaa", _MockHealth(_healthy_payload()))
    assert path.is_file()


def test_beat_overwrites_existing_heartbeat(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    hb = HeartbeatWriter(path)
    hb.beat("tick_x_first", _MockHealth(_healthy_payload()),
            ts=_NOW)
    hb.beat("tick_x_second",
            _MockHealth(_degraded_payload()),
            ts=_NOW + _dt.timedelta(seconds=5))
    payload = json.loads(path.read_text())
    assert payload["correlation_id"] == "tick_x_second"
    assert payload["degraded"] is True


# ---- atomic semantics ------------------------------------------------------


def test_beat_leaves_no_tmp_file(tmp_path: Path) -> None:
    """A successful write removes the .tmp staging file."""
    path = tmp_path / "heartbeat.json"
    hb = HeartbeatWriter(path)
    hb.beat("tick_x_aaaaaa", _MockHealth(_healthy_payload()))
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_beat_atomic_under_repeated_writes(tmp_path: Path) -> None:
    """After N writes, the file is always a complete JSON document."""
    path = tmp_path / "heartbeat.json"
    hb = HeartbeatWriter(path)
    for i in range(50):
        hb.beat(
            f"tick_x_{i:05d}",
            _MockHealth(_healthy_payload()),
            ts=_NOW + _dt.timedelta(seconds=i),
        )
    payload = json.loads(path.read_text())
    assert payload["correlation_id"] == "tick_x_00049"


# ---- error paths -----------------------------------------------------------


def test_beat_rejects_empty_correlation_id(tmp_path: Path) -> None:
    hb = HeartbeatWriter(tmp_path / "heartbeat.json")
    with pytest.raises(HeartbeatError, match="non-empty string"):
        hb.beat("", _MockHealth(_healthy_payload()))


def test_beat_rejects_non_string_correlation_id(tmp_path: Path) -> None:
    hb = HeartbeatWriter(tmp_path / "heartbeat.json")
    with pytest.raises(HeartbeatError, match="non-empty string"):
        hb.beat(123, _MockHealth(_healthy_payload()))  # type: ignore[arg-type]


def test_beat_rejects_health_without_to_debug_dict(tmp_path: Path) -> None:
    hb = HeartbeatWriter(tmp_path / "heartbeat.json")
    with pytest.raises(HeartbeatError, match="to_debug_dict"):
        hb.beat("tick_x_aaaaaa", object())  # type: ignore[arg-type]


def test_beat_propagates_health_to_debug_dict_failure(tmp_path: Path) -> None:
    hb = HeartbeatWriter(tmp_path / "heartbeat.json")
    with pytest.raises(HeartbeatError, match="raised"):
        hb.beat("tick_x_aaaaaa",
                _MockHealth(_healthy_payload(), raise_on_to_debug_dict=True))


def test_beat_rejects_naive_ts(tmp_path: Path) -> None:
    hb = HeartbeatWriter(tmp_path / "heartbeat.json")
    naive = _dt.datetime(2026, 5, 21, 16, 0, 0)
    with pytest.raises(HeartbeatError, match="timezone-aware"):
        hb.beat("tick_x_aaaaaa", _MockHealth(_healthy_payload()), ts=naive)


def test_beat_rejects_non_datetime_ts(tmp_path: Path) -> None:
    hb = HeartbeatWriter(tmp_path / "heartbeat.json")
    with pytest.raises(HeartbeatError, match="datetime"):
        hb.beat("tick_x_aaaaaa", _MockHealth(_healthy_payload()),
                ts="2026-05-21T16:00:00")  # type: ignore[arg-type]


def test_beat_rejects_non_json_encodable_health(tmp_path: Path) -> None:
    """If health.to_debug_dict() returns an object json.dumps can't encode,
    raise HeartbeatError."""
    class _Unencodable:
        pass

    hb = HeartbeatWriter(tmp_path / "heartbeat.json")
    bad = _MockHealth({"x": _Unencodable()})  # type: ignore[arg-type]
    with pytest.raises(HeartbeatError, match="not JSON-encodable"):
        hb.beat("tick_x_aaaaaa", bad)


def test_beat_io_failure_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routine OSError during write is swallowed (logged WARN)."""
    path = tmp_path / "heartbeat.json"
    hb = HeartbeatWriter(path)

    import watchdog.heartbeat as hb_mod
    real_atomic = hb_mod._atomic_write_text

    def _broken_write(p, t):
        raise OSError("ENOSPC")
    monkeypatch.setattr(hb_mod, "_atomic_write_text", _broken_write)

    # Must not raise.
    hb.beat("tick_x_aaaaaa", _MockHealth(_healthy_payload()))
    # File was never created.
    assert not path.exists()


# ---- last_written_ts -------------------------------------------------------


def test_last_written_ts_returns_none_before_first_beat(tmp_path: Path) -> None:
    hb = HeartbeatWriter(tmp_path / "heartbeat.json")
    assert hb.last_written_ts() is None


def test_last_written_ts_returns_tz_aware_after_beat(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    hb = HeartbeatWriter(path)
    hb.beat("tick_x_aaaaaa", _MockHealth(_healthy_payload()))
    out = hb.last_written_ts()
    assert out is not None
    assert out.tzinfo is not None
