"""Phase 8B action-layer tests — RestartActionResult, RestartLimiter, executor."""
from __future__ import annotations

import datetime as _dt
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from automation.errors import WatchdogActionError
from watchdog.action import (
    DEFAULT_MAX_RESTARTS,
    DEFAULT_RESET_HARD_COMMAND,
    DEFAULT_RESET_LITE_COMMAND,
    DEFAULT_WINDOW_S,
    RestartActionResult,
    RestartLimiter,
    WatchdogActionExecutor,
)
from watchdog.watchdog import WatchdogStatus


_UTC = _dt.timezone.utc
_NOW = _dt.datetime(2026, 5, 21, 17, 0, 0, tzinfo=_UTC)


def _status(
    recommendation: str = "none",
    *,
    status: str = "HEALTHY",
    age_s: float | None = 0.0,
) -> WatchdogStatus:
    return WatchdogStatus(
        status=status,
        age_s=age_s,
        recommendation=recommendation,
        ts=_NOW,
    )


def _make_completed(returncode: int = 0, stderr: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["pkill", "-TERM", "-f", "automation"],
        returncode=returncode,
        stdout=b"",
        stderr=stderr,
    )


# =============================================================================
# RestartActionResult
# =============================================================================


def test_result_construction_ok() -> None:
    r = RestartActionResult(
        action_type="RESET_LITE",
        attempted=True,
        blocked=False,
        primary_exit_code=0,
        primary_stderr=None,
        relaunched=False,
        relaunch_exit_code=None,
        recent_restart_count=1,
        notes=None,
        ts=_NOW,
    )
    assert r.action_type == "RESET_LITE"
    assert r.attempted is True


def test_result_rejects_unknown_action_type() -> None:
    with pytest.raises(WatchdogActionError, match="action_type must be one of"):
        RestartActionResult(
            action_type="REBOOT",
            attempted=False, blocked=False,
            primary_exit_code=None, primary_stderr=None,
            relaunched=False, relaunch_exit_code=None,
            recent_restart_count=0, notes=None, ts=_NOW,
        )


def test_result_blocked_with_attempted_true_rejected() -> None:
    with pytest.raises(WatchdogActionError, match="blocked=True is incompatible"):
        RestartActionResult(
            action_type="RESET_LITE",
            attempted=True, blocked=True,
            primary_exit_code=None, primary_stderr=None,
            relaunched=False, relaunch_exit_code=None,
            recent_restart_count=3, notes="rate-limited", ts=_NOW,
        )


def test_result_negative_recent_count_rejected() -> None:
    with pytest.raises(WatchdogActionError, match="must be >= 0"):
        RestartActionResult(
            action_type="none",
            attempted=False, blocked=False,
            primary_exit_code=None, primary_stderr=None,
            relaunched=False, relaunch_exit_code=None,
            recent_restart_count=-1, notes=None, ts=_NOW,
        )


def test_result_naive_ts_rejected() -> None:
    naive = _dt.datetime(2026, 5, 21, 17, 0, 0)
    with pytest.raises(WatchdogActionError, match="timezone-aware"):
        RestartActionResult(
            action_type="none",
            attempted=False, blocked=False,
            primary_exit_code=None, primary_stderr=None,
            relaunched=False, relaunch_exit_code=None,
            recent_restart_count=0, notes=None, ts=naive,
        )


def test_result_frozen() -> None:
    r = RestartActionResult(
        action_type="none",
        attempted=False, blocked=False,
        primary_exit_code=None, primary_stderr=None,
        relaunched=False, relaunch_exit_code=None,
        recent_restart_count=0, notes=None, ts=_NOW,
    )
    with pytest.raises(Exception):
        r.action_type = "RESET_LITE"  # type: ignore[misc]


def test_result_to_debug_dict_json_safe() -> None:
    r = RestartActionResult(
        action_type="RESET_LITE", attempted=True, blocked=False,
        primary_exit_code=0, primary_stderr="hello", relaunched=True,
        relaunch_exit_code=0, recent_restart_count=2, notes=None, ts=_NOW,
    )
    decoded = json.loads(json.dumps(r.to_debug_dict()))
    assert decoded["action_type"] == "RESET_LITE"
    assert decoded["primary_stderr"] == "hello"


def test_result_summary_shows_status() -> None:
    blocked = RestartActionResult(
        action_type="RESET_LITE", attempted=False, blocked=True,
        primary_exit_code=None, primary_stderr=None, relaunched=False,
        relaunch_exit_code=None, recent_restart_count=3, notes="x", ts=_NOW,
    )
    assert "BLOCKED" in blocked.summary()
    assert "RESET_LITE" in blocked.summary()


# =============================================================================
# RestartLimiter
# =============================================================================


def test_limiter_defaults_match_module() -> None:
    assert DEFAULT_MAX_RESTARTS == 3
    assert DEFAULT_WINDOW_S == 300.0


def test_limiter_construct_rejects_non_path(tmp_path: Path) -> None:
    with pytest.raises(WatchdogActionError, match="log_path must be Path"):
        RestartLimiter("var/restarts.log")  # type: ignore[arg-type]


def test_limiter_construct_rejects_zero_max(tmp_path: Path) -> None:
    with pytest.raises(WatchdogActionError, match="max_restarts must be > 0"):
        RestartLimiter(tmp_path / "log", max_restarts=0)


def test_limiter_construct_rejects_negative_window(tmp_path: Path) -> None:
    with pytest.raises(WatchdogActionError, match="window_s must be > 0"):
        RestartLimiter(tmp_path / "log", window_s=-1.0)


def test_limiter_construct_rejects_bool_window(tmp_path: Path) -> None:
    with pytest.raises(WatchdogActionError, match="window_s must be a number"):
        RestartLimiter(tmp_path / "log", window_s=True)  # type: ignore[arg-type]


def test_limiter_empty_log_is_allowed(tmp_path: Path) -> None:
    log = tmp_path / "restarts.log"
    lim = RestartLimiter(log, max_restarts=3, window_s=300)
    allowed, count = lim.is_allowed(now=_NOW)
    assert allowed is True
    assert count == 0


def test_limiter_under_ceiling_is_allowed(tmp_path: Path) -> None:
    log = tmp_path / "restarts.log"
    lim = RestartLimiter(log, max_restarts=3, window_s=300)
    lim.record("RESET_LITE", now=_NOW)
    lim.record("RESET_LITE", now=_NOW + _dt.timedelta(seconds=10))
    allowed, count = lim.is_allowed(now=_NOW + _dt.timedelta(seconds=20))
    assert allowed is True
    assert count == 2


def test_limiter_at_ceiling_blocks(tmp_path: Path) -> None:
    log = tmp_path / "restarts.log"
    lim = RestartLimiter(log, max_restarts=3, window_s=300)
    for i in range(3):
        lim.record("RESET_LITE", now=_NOW + _dt.timedelta(seconds=i))
    allowed, count = lim.is_allowed(now=_NOW + _dt.timedelta(seconds=10))
    assert allowed is False
    assert count == 3


def test_limiter_window_aging_lets_old_events_drop_off(tmp_path: Path) -> None:
    log = tmp_path / "restarts.log"
    lim = RestartLimiter(log, max_restarts=3, window_s=60)
    lim.record("RESET_LITE", now=_NOW)
    lim.record("RESET_LITE", now=_NOW + _dt.timedelta(seconds=10))
    # An old event well outside the window:
    lim.record("RESET_LITE", now=_NOW - _dt.timedelta(seconds=600))
    allowed, count = lim.is_allowed(now=_NOW + _dt.timedelta(seconds=20))
    assert allowed is True
    assert count == 2  # only the two in-window events


def test_limiter_records_both_kinds(tmp_path: Path) -> None:
    log = tmp_path / "restarts.log"
    lim = RestartLimiter(log, max_restarts=10, window_s=300)
    lim.record("RESET_LITE", now=_NOW)
    lim.record("RESET_HARD", now=_NOW + _dt.timedelta(seconds=1))
    contents = log.read_text()
    assert "RESET_LITE" in contents
    assert "RESET_HARD" in contents


def test_limiter_record_rejects_unknown_kind(tmp_path: Path) -> None:
    log = tmp_path / "restarts.log"
    lim = RestartLimiter(log)
    with pytest.raises(WatchdogActionError, match="RESET_LITE or RESET_HARD"):
        lim.record("REBOOT", now=_NOW)


def test_limiter_record_rejects_naive_ts(tmp_path: Path) -> None:
    log = tmp_path / "restarts.log"
    lim = RestartLimiter(log)
    naive = _dt.datetime(2026, 5, 21, 17, 0, 0)
    with pytest.raises(WatchdogActionError, match="timezone-aware"):
        lim.record("RESET_LITE", now=naive)


def test_limiter_persists_across_instances(tmp_path: Path) -> None:
    """The log file is the only state — a fresh limiter sees prior events."""
    log = tmp_path / "restarts.log"
    a = RestartLimiter(log, max_restarts=3, window_s=300)
    a.record("RESET_LITE", now=_NOW)
    a.record("RESET_HARD", now=_NOW + _dt.timedelta(seconds=10))
    b = RestartLimiter(log, max_restarts=3, window_s=300)
    allowed, count = b.is_allowed(now=_NOW + _dt.timedelta(seconds=20))
    assert allowed is True
    assert count == 2


def test_limiter_creates_parent_dir(tmp_path: Path) -> None:
    log = tmp_path / "nested" / "deep" / "restarts.log"
    lim = RestartLimiter(log)
    lim.record("RESET_LITE", now=_NOW)
    assert log.is_file()


def test_limiter_corrupt_lines_skipped(tmp_path: Path) -> None:
    """A half-written or garbled line must not crash the limiter."""
    log = tmp_path / "restarts.log"
    log.write_text(
        f"{_NOW.isoformat()} RESET_LITE\n"
        "garbage line\n"
        "another bad line with spaces\n"
        f"{(_NOW + _dt.timedelta(seconds=1)).isoformat()} RESET_HARD\n"
    )
    lim = RestartLimiter(log, max_restarts=10, window_s=300)
    _, count = lim.is_allowed(now=_NOW + _dt.timedelta(seconds=10))
    assert count == 2  # two parseable lines, two garbage skipped


def test_limiter_read_failure_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = tmp_path / "restarts.log"
    log.write_text(f"{_NOW.isoformat()} RESET_LITE\n")
    lim = RestartLimiter(log, max_restarts=3, window_s=300)

    def _broken_read(self, encoding="utf-8"):
        raise OSError("synthetic read failure")
    monkeypatch.setattr(Path, "read_text", _broken_read)
    allowed, count = lim.is_allowed(now=_NOW + _dt.timedelta(seconds=10))
    # On read failure, the limiter assumes empty (defensive — better
    # to let one extra restart through than to refuse recovery).
    assert allowed is True
    assert count == 0


def test_limiter_record_io_failure_is_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = tmp_path / "restarts.log"
    lim = RestartLimiter(log)
    real_open = Path.open

    def _broken_open(self, *args, **kwargs):
        if str(self).endswith("restarts.log"):
            raise OSError("ENOSPC")
        return real_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", _broken_open)
    # Must not raise.
    lim.record("RESET_LITE", now=_NOW)


# =============================================================================
# WatchdogActionExecutor
# =============================================================================


def test_executor_default_commands_match_spec() -> None:
    assert DEFAULT_RESET_LITE_COMMAND == ["pkill", "-TERM", "-f", "automation"]
    assert DEFAULT_RESET_HARD_COMMAND == ["pkill", "-KILL", "-f", "automation"]


def test_executor_rejects_missing_command_key(tmp_path: Path) -> None:
    with pytest.raises(WatchdogActionError, match="must include"):
        WatchdogActionExecutor(commands={"RESET_LITE": ["pkill"]})


def test_executor_rejects_empty_command_list(tmp_path: Path) -> None:
    with pytest.raises(WatchdogActionError, match="non-empty list"):
        WatchdogActionExecutor(commands={
            "RESET_LITE": [],
            "RESET_HARD": ["pkill"],
        })


def test_executor_rejects_non_string_command_element(tmp_path: Path) -> None:
    with pytest.raises(WatchdogActionError, match="only strings"):
        WatchdogActionExecutor(commands={
            "RESET_LITE": ["pkill", 123],  # type: ignore[list-item]
            "RESET_HARD": ["pkill"],
        })


def test_executor_rejects_empty_relaunch_command(tmp_path: Path) -> None:
    with pytest.raises(WatchdogActionError, match="relaunch_command"):
        WatchdogActionExecutor(relaunch_command=[])


def test_executor_rejects_zero_subprocess_timeout(tmp_path: Path) -> None:
    with pytest.raises(WatchdogActionError, match="subprocess_timeout_s must be > 0"):
        WatchdogActionExecutor(subprocess_timeout_s=0)


def test_executor_none_recommendation_is_noop(tmp_path: Path) -> None:
    lim = RestartLimiter(tmp_path / "log.log")
    ex = WatchdogActionExecutor(limiter=lim)
    r = ex.execute(_status("none"), now=_NOW)
    assert r.action_type == "none"
    assert r.attempted is False
    assert r.blocked is False
    assert r.primary_exit_code is None
    # Limiter was NOT touched for a no-op.
    assert not (tmp_path / "log.log").exists()


def test_executor_unknown_recommendation_raises(tmp_path: Path) -> None:
    lim = RestartLimiter(tmp_path / "log.log")
    ex = WatchdogActionExecutor(limiter=lim)
    bad = WatchdogStatus(
        status="HEALTHY", age_s=0.0, recommendation="none", ts=_NOW,
    )
    # Mutate via the __dict__ (frozen dataclass) — only way to inject
    # an invalid recommendation for the unknown-token path. The
    # production WatchdogStatus rejects bad recommendations at construction.
    object.__setattr__(bad, "recommendation", "REBOOT")
    with pytest.raises(WatchdogActionError, match="unknown recommendation"):
        ex.execute(bad, now=_NOW)


def test_executor_status_without_recommendation_attribute_raises(
    tmp_path: Path,
) -> None:
    lim = RestartLimiter(tmp_path / "log.log")
    ex = WatchdogActionExecutor(limiter=lim)

    @dataclass
    class _BadStatus:
        pass

    with pytest.raises(WatchdogActionError, match="lacks .recommendation"):
        ex.execute(_BadStatus(), now=_NOW)  # type: ignore[arg-type]


def test_executor_status_with_non_string_recommendation_raises(
    tmp_path: Path,
) -> None:
    lim = RestartLimiter(tmp_path / "log.log")
    ex = WatchdogActionExecutor(limiter=lim)

    @dataclass
    class _BadStatus:
        recommendation: Any = 123

    with pytest.raises(WatchdogActionError, match="must be str"):
        ex.execute(_BadStatus(), now=_NOW)  # type: ignore[arg-type]


def test_executor_reset_lite_invokes_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _make_completed(returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    lim = RestartLimiter(tmp_path / "log.log")
    ex = WatchdogActionExecutor(limiter=lim)
    r = ex.execute(_status("RESET_LITE", status="STALE", age_s=20.0), now=_NOW)
    assert r.attempted is True
    assert r.action_type == "RESET_LITE"
    assert r.primary_exit_code == 0
    assert calls[0] == DEFAULT_RESET_LITE_COMMAND


def test_executor_reset_hard_invokes_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _make_completed(returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    lim = RestartLimiter(tmp_path / "log.log")
    ex = WatchdogActionExecutor(limiter=lim)
    ex.execute(_status("RESET_HARD", status="INVALID", age_s=None), now=_NOW)
    assert calls[0] == DEFAULT_RESET_HARD_COMMAND


def test_executor_custom_command_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _make_completed(returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    lim = RestartLimiter(tmp_path / "log.log")
    ex = WatchdogActionExecutor(
        commands={
            "RESET_LITE": ["systemctl", "--user", "restart", "automation.service"],
            "RESET_HARD": ["pkill", "-KILL", "-f", "x"],
        },
        limiter=lim,
    )
    ex.execute(_status("RESET_LITE", status="STALE", age_s=20.0), now=_NOW)
    assert calls[0] == [
        "systemctl", "--user", "restart", "automation.service",
    ]


def test_executor_records_restart_to_limiter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _make_completed(returncode=0),
    )
    log = tmp_path / "log.log"
    lim = RestartLimiter(log, max_restarts=5, window_s=300)
    ex = WatchdogActionExecutor(limiter=lim)
    ex.execute(_status("RESET_LITE", status="STALE", age_s=20.0), now=_NOW)
    assert log.is_file()
    assert "RESET_LITE" in log.read_text()


def test_executor_rate_limited_blocks_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After hitting the ceiling, the executor returns ACTION_BLOCKED
    without spawning any subprocess."""
    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _make_completed(returncode=0)
    monkeypatch.setattr(subprocess, "run", _fake_run)
    log = tmp_path / "log.log"
    # Pre-populate at the ceiling.
    lim = RestartLimiter(log, max_restarts=3, window_s=300)
    for i in range(3):
        lim.record("RESET_LITE", now=_NOW - _dt.timedelta(seconds=i + 1))
    ex = WatchdogActionExecutor(limiter=lim)
    r = ex.execute(_status("RESET_LITE", status="STALE", age_s=20.0), now=_NOW)
    assert r.blocked is True
    assert r.attempted is False
    assert r.primary_exit_code is None
    assert calls == []  # subprocess was NOT spawned
    assert "rate-limited" in (r.notes or "")


def test_executor_relaunch_invoked_after_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def _fake_run(argv, **kwargs):
        calls.append(list(argv))
        return _make_completed(returncode=0)
    monkeypatch.setattr(subprocess, "run", _fake_run)
    lim = RestartLimiter(tmp_path / "log.log")
    ex = WatchdogActionExecutor(
        limiter=lim,
        relaunch_command=["systemctl", "--user", "start", "automation"],
    )
    r = ex.execute(_status("RESET_LITE", status="STALE", age_s=20.0), now=_NOW)
    assert r.relaunched is True
    assert r.relaunch_exit_code == 0
    assert len(calls) == 2
    assert calls[1] == ["systemctl", "--user", "start", "automation"]


def test_executor_subprocess_filenotfound_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(argv, **kwargs):
        raise FileNotFoundError("pkill: not found")
    monkeypatch.setattr(subprocess, "run", _fake_run)
    lim = RestartLimiter(tmp_path / "log.log")
    ex = WatchdogActionExecutor(limiter=lim)
    r = ex.execute(_status("RESET_LITE", status="STALE", age_s=20.0), now=_NOW)
    assert r.attempted is True
    assert r.primary_exit_code is None
    assert "command not found" in (r.notes or "")


def test_executor_subprocess_timeout_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 5))
    monkeypatch.setattr(subprocess, "run", _fake_run)
    lim = RestartLimiter(tmp_path / "log.log")
    ex = WatchdogActionExecutor(limiter=lim, subprocess_timeout_s=2.0)
    r = ex.execute(_status("RESET_HARD", status="INVALID"), now=_NOW)
    assert r.attempted is True
    assert r.primary_exit_code is None
    assert "timed out" in (r.notes or "")


def test_executor_non_zero_exit_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pkill returns 1 when no process matched — recorded as attempted+exit 1."""
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _make_completed(returncode=1, stderr=b"no process found"),
    )
    lim = RestartLimiter(tmp_path / "log.log")
    ex = WatchdogActionExecutor(limiter=lim)
    r = ex.execute(_status("RESET_LITE", status="STALE", age_s=20.0), now=_NOW)
    assert r.attempted is True
    assert r.primary_exit_code == 1
    assert r.primary_stderr == "no process found"


def test_executor_uses_subprocess_kwargs_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify shell=False, check=False, capture_output=True, timeout passed."""
    captured_kwargs: dict[str, Any] = {}

    def _fake_run(argv, **kwargs):
        captured_kwargs.update(kwargs)
        return _make_completed(returncode=0)
    monkeypatch.setattr(subprocess, "run", _fake_run)
    lim = RestartLimiter(tmp_path / "log.log")
    ex = WatchdogActionExecutor(limiter=lim, subprocess_timeout_s=3.0)
    ex.execute(_status("RESET_LITE", status="STALE", age_s=20.0), now=_NOW)
    assert captured_kwargs.get("shell") is False
    assert captured_kwargs.get("check") is False
    assert captured_kwargs.get("capture_output") is True
    assert captured_kwargs.get("timeout") == 3.0


def test_executor_long_stderr_truncated_at_1kb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    huge = b"x" * 4096
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _make_completed(returncode=0, stderr=huge),
    )
    lim = RestartLimiter(tmp_path / "log.log")
    ex = WatchdogActionExecutor(limiter=lim)
    r = ex.execute(_status("RESET_LITE", status="STALE", age_s=20.0), now=_NOW)
    assert r.primary_stderr is not None
    assert len(r.primary_stderr) <= 1025  # 1024 + ellipsis


def test_executor_recent_count_reflects_state_after_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: _make_completed(returncode=0),
    )
    log = tmp_path / "log.log"
    lim = RestartLimiter(log, max_restarts=5, window_s=300)
    # Pre-populate with 1 restart.
    lim.record("RESET_LITE", now=_NOW - _dt.timedelta(seconds=10))
    ex = WatchdogActionExecutor(limiter=lim)
    r = ex.execute(_status("RESET_LITE", status="STALE", age_s=20.0), now=_NOW)
    # After the execute, log has 2 entries within the window.
    assert r.recent_restart_count == 2
