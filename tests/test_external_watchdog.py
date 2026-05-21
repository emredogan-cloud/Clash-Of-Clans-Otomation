"""ExternalWatchdog tests — HEALTHY / STALE / MISSING / INVALID + artifacts."""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

import pytest

from automation.errors import ExternalWatchdogError
from watchdog.watchdog import (
    DEFAULT_STALE_AFTER_S,
    ExternalWatchdog,
    RECOMMENDATION_NONE,
    RECOMMENDATION_RESET_HARD,
    RECOMMENDATION_RESET_LITE,
    SUPPORTED_HEARTBEAT_SCHEMA_VERSION,
    WatchdogStatus,
    WatchdogStatusKind,
)


_UTC = _dt.timezone.utc
_NOW = _dt.datetime(2026, 5, 21, 16, 0, 0, tzinfo=_UTC)


# ---- helpers ---------------------------------------------------------------


def _write_heartbeat(
    path: Path,
    *,
    ts: _dt.datetime = _NOW,
    schema_version: int = SUPPORTED_HEARTBEAT_SCHEMA_VERSION,
    correlation_id: str = "tick_x_aaaaaa",
    degraded: bool = False,
    health: dict[str, Any] | None = None,
    pid: int = 12345,
    extra: dict[str, Any] | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "ts": ts.isoformat(),
        "correlation_id": correlation_id,
        "degraded": degraded,
        "health": health or {"sensor_ok": True, "matcher_ok": True,
                              "actuator_ok": True, "orchestrator_ok": True,
                              "last_error": None, "degraded": False,
                              "ts": ts.isoformat()},
        "pid": pid,
    }
    if extra:
        payload.update(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))
    return path


# ---- WatchdogStatus container tests ----------------------------------------


def test_watchdog_status_construction_ok() -> None:
    s = WatchdogStatus(
        status="HEALTHY", age_s=2.5,
        recommendation=RECOMMENDATION_NONE, ts=_NOW,
    )
    assert s.status == "HEALTHY"
    assert s.age_s == 2.5


def test_watchdog_status_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="status must be one of"):
        WatchdogStatus(
            status="WEIRD", age_s=1.0,
            recommendation=RECOMMENDATION_NONE, ts=_NOW,
        )


def test_watchdog_status_rejects_unknown_recommendation() -> None:
    with pytest.raises(ValueError, match="recommendation must be one of"):
        WatchdogStatus(
            status="HEALTHY", age_s=1.0,
            recommendation="REBOOT_PHONE", ts=_NOW,
        )


def test_watchdog_status_rejects_negative_age() -> None:
    with pytest.raises(ValueError, match="age_s must be >= 0"):
        WatchdogStatus(
            status="HEALTHY", age_s=-0.1,
            recommendation=RECOMMENDATION_NONE, ts=_NOW,
        )


def test_watchdog_status_rejects_bool_age() -> None:
    with pytest.raises(TypeError, match="age_s must be float or None"):
        WatchdogStatus(
            status="HEALTHY", age_s=True,  # type: ignore[arg-type]
            recommendation=RECOMMENDATION_NONE, ts=_NOW,
        )


def test_watchdog_status_rejects_naive_ts() -> None:
    naive = _dt.datetime(2026, 5, 21, 16, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        WatchdogStatus(
            status="HEALTHY", age_s=0.0,
            recommendation=RECOMMENDATION_NONE, ts=naive,
        )


def test_watchdog_status_age_may_be_none() -> None:
    """For MISSING / INVALID, age_s is None."""
    s = WatchdogStatus(
        status="MISSING", age_s=None,
        recommendation=RECOMMENDATION_RESET_LITE, ts=_NOW,
    )
    assert s.age_s is None


def test_watchdog_status_is_frozen() -> None:
    s = WatchdogStatus(
        status="HEALTHY", age_s=0.0,
        recommendation=RECOMMENDATION_NONE, ts=_NOW,
    )
    with pytest.raises(Exception):
        s.status = "STALE"  # type: ignore[misc]


def test_watchdog_status_debug_dict_keys_and_json_safe() -> None:
    s = WatchdogStatus(
        status="STALE", age_s=20.0,
        recommendation=RECOMMENDATION_RESET_LITE, ts=_NOW,
    )
    blob = s.to_debug_dict()
    decoded = json.loads(json.dumps(blob))
    assert set(decoded.keys()) == {"status", "age_s", "recommendation", "ts"}
    assert decoded["status"] == "STALE"
    assert decoded["age_s"] == 20.0


def test_watchdog_status_summary_format() -> None:
    s = WatchdogStatus(
        status="HEALTHY", age_s=1.0,
        recommendation=RECOMMENDATION_NONE, ts=_NOW,
    )
    assert "HEALTHY" in s.summary()
    assert "1.00s" in s.summary()
    assert "none" in s.summary()


# ---- ExternalWatchdog construction validation ------------------------------


def test_construct_requires_path_instance(tmp_path: Path) -> None:
    with pytest.raises(ExternalWatchdogError, match="must be Path"):
        ExternalWatchdog("not a path")  # type: ignore[arg-type]


def test_construct_rejects_zero_threshold(tmp_path: Path) -> None:
    with pytest.raises(ExternalWatchdogError, match="must be > 0"):
        ExternalWatchdog(tmp_path / "heartbeat.json", stale_after_s=0)


def test_construct_rejects_negative_threshold(tmp_path: Path) -> None:
    with pytest.raises(ExternalWatchdogError, match="must be > 0"):
        ExternalWatchdog(tmp_path / "heartbeat.json", stale_after_s=-1.0)


def test_construct_rejects_non_number_threshold(tmp_path: Path) -> None:
    with pytest.raises(ExternalWatchdogError, match="must be a number"):
        ExternalWatchdog(tmp_path / "heartbeat.json",
                         stale_after_s="15")  # type: ignore[arg-type]


def test_construct_rejects_bool_threshold(tmp_path: Path) -> None:
    with pytest.raises(ExternalWatchdogError, match="must be a number"):
        ExternalWatchdog(tmp_path / "heartbeat.json",
                         stale_after_s=True)  # type: ignore[arg-type]


def test_default_threshold_matches_module_constant(tmp_path: Path) -> None:
    wd = ExternalWatchdog(tmp_path / "heartbeat.json")
    assert wd.stale_after_s == DEFAULT_STALE_AFTER_S


# ---- HEALTHY ---------------------------------------------------------------


def test_check_healthy_fresh_heartbeat(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    _write_heartbeat(path, ts=_NOW)
    wd = ExternalWatchdog(path, stale_after_s=15.0)
    status = wd.check(now=_NOW + _dt.timedelta(seconds=2))
    assert status.status == "HEALTHY"
    assert status.recommendation == RECOMMENDATION_NONE
    assert status.age_s is not None and 1.9 <= status.age_s <= 2.1


def test_check_healthy_at_zero_age(tmp_path: Path) -> None:
    """Same instant the heartbeat was written → age 0."""
    path = tmp_path / "heartbeat.json"
    _write_heartbeat(path, ts=_NOW)
    wd = ExternalWatchdog(path)
    status = wd.check(now=_NOW)
    assert status.status == "HEALTHY"
    assert status.age_s == 0.0


def test_check_healthy_clock_skew_clamps_to_zero(tmp_path: Path) -> None:
    """Writer clock ahead of observer clock → age would be negative; clamp to 0."""
    path = tmp_path / "heartbeat.json"
    _write_heartbeat(path, ts=_NOW + _dt.timedelta(seconds=2))
    wd = ExternalWatchdog(path)
    status = wd.check(now=_NOW)
    assert status.status == "HEALTHY"
    assert status.age_s == 0.0


# ---- STALE -----------------------------------------------------------------


def test_check_stale_when_age_exceeds_threshold(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    _write_heartbeat(path, ts=_NOW)
    wd = ExternalWatchdog(path, stale_after_s=10.0)
    status = wd.check(now=_NOW + _dt.timedelta(seconds=15))
    assert status.status == "STALE"
    assert status.recommendation == RECOMMENDATION_RESET_LITE
    assert status.age_s is not None and 14.9 <= status.age_s <= 15.1


def test_check_boundary_age_equals_threshold_is_healthy(tmp_path: Path) -> None:
    """Age == threshold → not stale (strict >)."""
    path = tmp_path / "heartbeat.json"
    _write_heartbeat(path, ts=_NOW)
    wd = ExternalWatchdog(path, stale_after_s=10.0)
    status = wd.check(now=_NOW + _dt.timedelta(seconds=10))
    assert status.status == "HEALTHY"


# ---- MISSING ---------------------------------------------------------------


def test_check_missing_when_no_file(tmp_path: Path) -> None:
    path = tmp_path / "never_written.json"
    wd = ExternalWatchdog(path)
    status = wd.check(now=_NOW)
    assert status.status == "MISSING"
    assert status.recommendation == RECOMMENDATION_RESET_LITE
    assert status.age_s is None


def test_check_missing_when_parent_dir_absent(tmp_path: Path) -> None:
    """Missing parent dir is still classified as MISSING (not error)."""
    path = tmp_path / "nope" / "heartbeat.json"
    wd = ExternalWatchdog(path)
    status = wd.check(now=_NOW)
    assert status.status == "MISSING"


# ---- INVALID ---------------------------------------------------------------


def test_check_invalid_on_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    path.write_text("{this is not json")
    wd = ExternalWatchdog(path)
    status = wd.check(now=_NOW)
    assert status.status == "INVALID"
    assert status.recommendation == RECOMMENDATION_RESET_HARD


def test_check_invalid_on_array_root(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    path.write_text("[1, 2, 3]")
    wd = ExternalWatchdog(path)
    status = wd.check(now=_NOW)
    assert status.status == "INVALID"


def test_check_invalid_on_missing_required_field(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    # Missing 'pid' field.
    payload = {
        "schema_version": 1, "ts": _NOW.isoformat(),
        "correlation_id": "tick_x_aaaaaa", "degraded": False,
        "health": {},
    }
    path.write_text(json.dumps(payload))
    wd = ExternalWatchdog(path)
    status = wd.check(now=_NOW)
    assert status.status == "INVALID"


def test_check_invalid_on_wrong_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    _write_heartbeat(path, ts=_NOW, schema_version=99)
    wd = ExternalWatchdog(path)
    status = wd.check(now=_NOW)
    assert status.status == "INVALID"
    assert status.recommendation == RECOMMENDATION_RESET_HARD


def test_check_invalid_on_non_int_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    payload = {
        "schema_version": "1", "ts": _NOW.isoformat(),
        "correlation_id": "tick_x_aaaaaa", "degraded": False,
        "health": {}, "pid": 1,
    }
    path.write_text(json.dumps(payload))
    wd = ExternalWatchdog(path)
    status = wd.check(now=_NOW)
    assert status.status == "INVALID"


def test_check_invalid_on_non_string_ts(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    payload = {
        "schema_version": 1, "ts": 12345, "correlation_id": "tick_x_aaaaaa",
        "degraded": False, "health": {}, "pid": 1,
    }
    path.write_text(json.dumps(payload))
    wd = ExternalWatchdog(path)
    status = wd.check(now=_NOW)
    assert status.status == "INVALID"


def test_check_invalid_on_unparseable_ts(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    payload = {
        "schema_version": 1, "ts": "not-an-iso-string",
        "correlation_id": "tick_x_aaaaaa",
        "degraded": False, "health": {}, "pid": 1,
    }
    path.write_text(json.dumps(payload))
    wd = ExternalWatchdog(path)
    status = wd.check(now=_NOW)
    assert status.status == "INVALID"


def test_check_invalid_on_naive_ts(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    payload = {
        "schema_version": 1, "ts": "2026-05-21T16:00:00",  # no tz
        "correlation_id": "tick_x_aaaaaa",
        "degraded": False, "health": {}, "pid": 1,
    }
    path.write_text(json.dumps(payload))
    wd = ExternalWatchdog(path)
    status = wd.check(now=_NOW)
    assert status.status == "INVALID"


def test_check_invalid_on_unreadable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError on read is classified INVALID, not raised."""
    path = tmp_path / "heartbeat.json"
    _write_heartbeat(path, ts=_NOW)

    def _broken_read(self, encoding="utf-8"):
        raise OSError("synthetic read failure")
    monkeypatch.setattr(Path, "read_text", _broken_read)

    wd = ExternalWatchdog(path)
    status = wd.check(now=_NOW)
    assert status.status == "INVALID"
    assert status.recommendation == RECOMMENDATION_RESET_HARD


# ---- escalation policy --------------------------------------------------


def test_escalation_map_complete() -> None:
    """All four statuses must map to a defined recommendation."""
    mapping = {
        "HEALTHY": RECOMMENDATION_NONE,
        "STALE": RECOMMENDATION_RESET_LITE,
        "MISSING": RECOMMENDATION_RESET_LITE,
        "INVALID": RECOMMENDATION_RESET_HARD,
    }
    for kind in WatchdogStatusKind:
        assert kind.value in mapping


# ---- artifacts -------------------------------------------------------------


def test_artifact_written_when_debug_enabled(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    _write_heartbeat(path, ts=_NOW)
    artifacts = tmp_path / "artifacts" / "external_watchdog"
    wd = ExternalWatchdog(
        path, stale_after_s=15.0, debug=True, artifacts_dir=artifacts,
    )
    wd.check(now=_NOW + _dt.timedelta(seconds=2))
    subdirs = list(artifacts.iterdir())
    assert len(subdirs) == 1
    md = json.loads((subdirs[0] / "metadata.json").read_text())
    assert md["status"] == "HEALTHY"
    assert md["recommendation"] == RECOMMENDATION_NONE
    assert md["heartbeat_age_s"] is not None
    assert md["heartbeat_path"] == str(path)
    assert md["stale_after_s"] == 15.0
    assert md["parse_note"] is None


def test_artifact_records_parse_note_on_invalid(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    path.write_text("{garbage")
    artifacts = tmp_path / "artifacts" / "external_watchdog"
    wd = ExternalWatchdog(path, debug=True, artifacts_dir=artifacts)
    wd.check(now=_NOW)
    md = json.loads(
        next(iter(artifacts.iterdir())).joinpath("metadata.json").read_text()
    )
    assert md["status"] == "INVALID"
    assert md["parse_note"] is not None
    assert "json decode" in md["parse_note"]


def test_artifact_skipped_when_debug_disabled(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    _write_heartbeat(path, ts=_NOW)
    artifacts = tmp_path / "artifacts" / "external_watchdog"
    wd = ExternalWatchdog(path, debug=False, artifacts_dir=artifacts)
    wd.check(now=_NOW)
    if artifacts.exists():
        assert not any(artifacts.iterdir())


def test_artifact_env_var_enables_debug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "heartbeat.json"
    _write_heartbeat(path, ts=_NOW)
    artifacts = tmp_path / "artifacts" / "external_watchdog"
    monkeypatch.setenv("WATCHDOG_L2_DEBUG", "1")
    wd = ExternalWatchdog(path, artifacts_dir=artifacts)
    assert wd.debug is True
    wd.check(now=_NOW)
    assert any(artifacts.iterdir())


def test_artifact_atomic_no_tmp_leak(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    _write_heartbeat(path, ts=_NOW)
    artifacts = tmp_path / "artifacts" / "external_watchdog"
    wd = ExternalWatchdog(path, debug=True, artifacts_dir=artifacts)
    wd.check(now=_NOW)
    d = next(iter(artifacts.iterdir()))
    assert any(p.name == "metadata.json" for p in d.iterdir())
    assert not any(p.suffix == ".tmp" for p in d.iterdir())


def test_artifact_failure_does_not_break_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "heartbeat.json"
    _write_heartbeat(path, ts=_NOW)
    wd = ExternalWatchdog(
        path, debug=True,
        artifacts_dir=Path("/proc/forbidden/external_watchdog"),
    )
    status = wd.check(now=_NOW)
    # Status returned correctly even though the artifact write failed.
    assert status.status == "HEALTHY"


# ---- no automation/runtime imports -----------------------------------------


def test_module_does_not_import_orchestrator() -> None:
    """The L2 watchdog must not import from automation/orchestrator etc."""
    import watchdog.watchdog as wd_mod
    # Allowed: stdlib + automation.errors. Forbidden: any other automation.*.
    src = Path(wd_mod.__file__).read_text()
    forbidden = [
        "from automation.orchestrator",
        "from automation.sensor",
        "from automation.matcher",
        "from automation.actuator",
        "from automation.watchdog",  # Phase-7 in-process watchdog
        "from automation.runtime_health",
    ]
    for needle in forbidden:
        assert needle not in src, f"L2 watchdog must not import: {needle}"
