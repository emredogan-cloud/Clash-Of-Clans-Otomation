"""Watchdog tests — supervision, timeout, containment, recovery wiring."""
from __future__ import annotations

import datetime as _dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from automation.errors import (
    ADBError,
    CaptureError,
    CoordinateError,
    InvalidTransitionError,
    MatcherError,
    TimeoutFault,
)
from automation.runtime_health import RuntimeHealth
from automation.state import State
from automation.tick_result import TickResult
from automation.watchdog import (
    ABSOLUTE_BUDGET_MS,
    DEFAULT_TIMEOUT_BUDGETS_MS,
    Watchdog,
)


_UTC = _dt.timezone.utc


# ---- mocks -----------------------------------------------------------------


@dataclass
class _MockOrchestrator:
    """Fake `Orchestrator` exposing the surface the Watchdog uses."""

    state: State = State.IDLE
    tick_count: int = 0
    sleep_ms: int = 0
    raise_on_tick: BaseException | None = None
    canned_result: TickResult | None = None

    def tick(self) -> TickResult:
        self.tick_count += 1
        if self.sleep_ms > 0:
            time.sleep(self.sleep_ms / 1000.0)
        if self.raise_on_tick is not None:
            raise self.raise_on_tick
        return self.canned_result or _ok_result()


@dataclass
class _MockRecovery:
    """Fake `RecoveryManager`."""

    recover_calls: list[tuple[Exception, str | None]] = field(default_factory=list)
    health_to_return: RuntimeHealth | None = None
    raise_on_recover: bool = False

    def recover(
        self,
        error: Exception,
        *,
        correlation_id: str | None = None,
    ) -> RuntimeHealth:
        self.recover_calls.append((error, correlation_id))
        if self.raise_on_recover:
            raise RuntimeError("synthetic recovery failure")
        if self.health_to_return is not None:
            return self.health_to_return
        # Default: claim full recovery (subsystems OK, but degraded because
        # last_error is set per the original cause).
        return RuntimeHealth(
            sensor_ok=True, matcher_ok=True, actuator_ok=True,
            orchestrator_ok=True,
            last_error=f"{type(error).__name__}: {error}",
            degraded=True,
            ts=_dt.datetime.now(tz=_UTC),
        )


# ---- helpers ---------------------------------------------------------------


def _ok_result(
    *,
    action_latency_ms: float | None = 60.0,
    tick_latency_ms: float = 1500.0,
) -> TickResult:
    return TickResult(
        state_before=State.IDLE,
        state_after=State.IDLE,
        success=True,
        tick_latency_ms=tick_latency_ms,
        capture_latency_ms=940.0,
        match_latency_ms=50.0,
        action_latency_ms=action_latency_ms,
        ts=_dt.datetime.now(tz=_UTC),
    )


def _miss_result() -> TickResult:
    return TickResult(
        state_before=State.IDLE,
        state_after=State.FAILED,
        success=False,
        tick_latency_ms=1100.0,
        capture_latency_ms=1050.0,
        match_latency_ms=50.0,
        action_latency_ms=None,
        ts=_dt.datetime.now(tz=_UTC),
    )


# ---- construction validation ----------------------------------------------


def test_default_budgets_match_spec() -> None:
    assert DEFAULT_TIMEOUT_BUDGETS_MS == {
        "search_only": 2500,
        "validated": 4000,
        "validated_retry": 5000,
    }


def test_construction_accepts_custom_budgets() -> None:
    orch = _MockOrchestrator()
    wd = Watchdog(orch, timeout_budgets_ms={  # type: ignore[arg-type]
        "search_only": 1000, "validated": 2000, "validated_retry": 3000,
    })
    assert wd.timeout_budgets_ms["search_only"] == 1000


def test_construction_rejects_missing_tier() -> None:
    orch = _MockOrchestrator()
    with pytest.raises(ValueError, match="missing required tier"):
        Watchdog(orch, timeout_budgets_ms={  # type: ignore[arg-type]
            "search_only": 1000, "validated": 2000,
        })


def test_construction_rejects_zero_budget() -> None:
    orch = _MockOrchestrator()
    with pytest.raises(ValueError, match="must be > 0"):
        Watchdog(orch, timeout_budgets_ms={  # type: ignore[arg-type]
            "search_only": 0, "validated": 1, "validated_retry": 1,
        })


def test_construction_rejects_non_int_budget() -> None:
    orch = _MockOrchestrator()
    with pytest.raises(TypeError, match="must be int"):
        Watchdog(orch, timeout_budgets_ms={  # type: ignore[arg-type]
            "search_only": 1.5, "validated": 1, "validated_retry": 1,  # type: ignore[dict-item]
        })


def test_initial_last_health_is_healthy() -> None:
    orch = _MockOrchestrator()
    wd = Watchdog(orch)  # type: ignore[arg-type]
    assert wd.last_health.degraded is False


# ---- normal supervised tick -----------------------------------------------


def test_run_tick_passes_through_orchestrator_result() -> None:
    orch = _MockOrchestrator(canned_result=_ok_result())
    wd = Watchdog(orch)  # type: ignore[arg-type]
    r = wd.run_tick()
    assert r.success is True
    assert orch.tick_count == 1


def test_run_tick_records_healthy_state_when_tick_succeeds() -> None:
    orch = _MockOrchestrator(canned_result=_ok_result())
    wd = Watchdog(orch)  # type: ignore[arg-type]
    wd.run_tick()
    assert wd.last_health.degraded is False


def test_run_tick_does_not_invoke_recovery_on_success() -> None:
    orch = _MockOrchestrator(canned_result=_ok_result())
    rec = _MockRecovery()
    wd = Watchdog(orch, recovery=rec)  # type: ignore[arg-type]
    wd.run_tick()
    assert rec.recover_calls == []


# ---- exception containment -------------------------------------------------


def test_run_tick_catches_capture_error_returns_synthetic_failed() -> None:
    orch = _MockOrchestrator(raise_on_tick=CaptureError("device disconnected"))
    wd = Watchdog(orch)  # type: ignore[arg-type]
    r = wd.run_tick()
    assert isinstance(r, TickResult)
    assert r.success is False
    assert r.state_after is State.FAILED
    assert wd.last_health.degraded is True
    assert wd.last_health.sensor_ok is False


def test_run_tick_catches_actuator_coordinate_error() -> None:
    orch = _MockOrchestrator(raise_on_tick=CoordinateError("bad coord"))
    wd = Watchdog(orch)  # type: ignore[arg-type]
    r = wd.run_tick()
    assert r.success is False
    assert wd.last_health.actuator_ok is False


def test_run_tick_catches_matcher_error() -> None:
    orch = _MockOrchestrator(raise_on_tick=MatcherError("threshold issue"))
    wd = Watchdog(orch)  # type: ignore[arg-type]
    wd.run_tick()
    assert wd.last_health.matcher_ok is False


def test_run_tick_catches_invalid_transition_error() -> None:
    orch = _MockOrchestrator(raise_on_tick=InvalidTransitionError("FSM stuck"))
    wd = Watchdog(orch)  # type: ignore[arg-type]
    wd.run_tick()
    assert wd.last_health.orchestrator_ok is False


def test_run_tick_catches_adb_error_marks_sensor_and_actuator() -> None:
    orch = _MockOrchestrator(raise_on_tick=ADBError("device dropped"))
    wd = Watchdog(orch)  # type: ignore[arg-type]
    wd.run_tick()
    assert wd.last_health.sensor_ok is False
    assert wd.last_health.actuator_ok is False


def test_run_tick_catches_unknown_exception_pessimistically() -> None:
    orch = _MockOrchestrator(raise_on_tick=RuntimeError("mystery"))
    wd = Watchdog(orch)  # type: ignore[arg-type]
    wd.run_tick()
    assert wd.last_health.orchestrator_ok is False


# ---- timeout (post-hoc soft enforcement) -----------------------------------


def test_run_tick_flags_timeout_when_budget_exceeded() -> None:
    """A search_only tick that takes longer than the budget is flagged.
    We use a tight 100-ms budget so a mock orchestrator that sleeps 250 ms
    exceeds it."""
    orch = _MockOrchestrator(
        canned_result=_miss_result(),  # action_latency_ms=None → search_only
        sleep_ms=250,
    )
    wd = Watchdog(
        orch,  # type: ignore[arg-type]
        timeout_budgets_ms={
            "search_only": 100, "validated": 4000, "validated_retry": 5000,
        },
    )
    wd.run_tick()
    assert wd.last_health.degraded is True
    assert wd.last_health.orchestrator_ok is False
    assert "TimeoutFault" in (wd.last_health.last_error or "")


def test_run_tick_does_not_flag_timeout_when_within_budget() -> None:
    orch = _MockOrchestrator(
        canned_result=_miss_result(),
        sleep_ms=10,
    )
    wd = Watchdog(orch)  # type: ignore[arg-type]
    wd.run_tick()
    assert wd.last_health.degraded is False


def test_run_tick_uses_validated_retry_budget_when_action_ran() -> None:
    """An action-bearing tick is judged against the validated_retry budget
    (the more lenient one, per the v1.0 simplification)."""
    orch = _MockOrchestrator(
        canned_result=_ok_result(action_latency_ms=60.0),  # action ran
        sleep_ms=200,
    )
    wd = Watchdog(
        orch,  # type: ignore[arg-type]
        timeout_budgets_ms={
            "search_only": 50, "validated": 100, "validated_retry": 1000,
        },
    )
    wd.run_tick()
    # 200 ms < 1000 ms (validated_retry) → no timeout flag.
    assert wd.last_health.degraded is False


def test_run_tick_uses_search_only_budget_when_no_action() -> None:
    orch = _MockOrchestrator(
        canned_result=_miss_result(),
        sleep_ms=200,
    )
    wd = Watchdog(
        orch,  # type: ignore[arg-type]
        timeout_budgets_ms={
            "search_only": 100, "validated": 4000, "validated_retry": 5000,
        },
    )
    wd.run_tick()
    # 200 ms > 100 ms (search_only) → timeout flag.
    assert wd.last_health.orchestrator_ok is False


# ---- recovery wiring -------------------------------------------------------


def test_run_tick_invokes_recovery_on_exception() -> None:
    orch = _MockOrchestrator(raise_on_tick=CaptureError("bad"))
    rec = _MockRecovery()
    wd = Watchdog(orch, recovery=rec)  # type: ignore[arg-type]
    wd.run_tick()
    assert len(rec.recover_calls) == 1
    err, cid = rec.recover_calls[0]
    assert isinstance(err, CaptureError)
    assert cid is not None and cid.startswith("tick_")


def test_run_tick_invokes_recovery_on_timeout() -> None:
    orch = _MockOrchestrator(
        canned_result=_miss_result(),
        sleep_ms=200,
    )
    rec = _MockRecovery()
    wd = Watchdog(
        orch,  # type: ignore[arg-type]
        recovery=rec,
        timeout_budgets_ms={
            "search_only": 50, "validated": 100, "validated_retry": 100,
        },
    )
    wd.run_tick()
    assert len(rec.recover_calls) == 1
    err, _ = rec.recover_calls[0]
    assert isinstance(err, TimeoutFault)


def test_run_tick_publishes_recovery_health_when_recovery_runs() -> None:
    """The post-recovery health snapshot becomes `last_health`."""
    orch = _MockOrchestrator(raise_on_tick=CaptureError("x"))
    custom_health = RuntimeHealth(
        sensor_ok=True, matcher_ok=True, actuator_ok=True, orchestrator_ok=True,
        last_error="CaptureError: x", degraded=True,
        ts=_dt.datetime.now(tz=_UTC),
    )
    rec = _MockRecovery(health_to_return=custom_health)
    wd = Watchdog(orch, recovery=rec)  # type: ignore[arg-type]
    wd.run_tick()
    assert wd.last_health == custom_health


def test_run_tick_swallows_recovery_exception() -> None:
    """Recovery itself failing must not crash the framework."""
    orch = _MockOrchestrator(raise_on_tick=CaptureError("x"))
    rec = _MockRecovery(raise_on_recover=True)
    wd = Watchdog(orch, recovery=rec)  # type: ignore[arg-type]
    r = wd.run_tick()
    # Watchdog still returned a TickResult.
    assert isinstance(r, TickResult)
    # last_health kept the immediate (pre-recovery) snapshot.
    assert wd.last_health.degraded is True


def test_run_tick_recovery_only_one_attempt_per_call() -> None:
    """Exactly one recovery attempt per run_tick — no implicit retry loop."""
    orch = _MockOrchestrator(raise_on_tick=CaptureError("x"))
    rec = _MockRecovery()
    wd = Watchdog(orch, recovery=rec)  # type: ignore[arg-type]
    wd.run_tick()
    assert len(rec.recover_calls) == 1


def test_run_tick_no_recovery_when_recovery_argument_is_none() -> None:
    orch = _MockOrchestrator(raise_on_tick=CaptureError("x"))
    wd = Watchdog(orch, recovery=None)  # type: ignore[arg-type]
    wd.run_tick()
    # No crash — just a degraded health snapshot.
    assert wd.last_health.degraded is True


# ---- correlation id --------------------------------------------------------


def test_each_run_tick_uses_its_own_correlation_id() -> None:
    orch = _MockOrchestrator(raise_on_tick=CaptureError("x"))
    rec = _MockRecovery()
    wd = Watchdog(orch, recovery=rec)  # type: ignore[arg-type]
    wd.run_tick()
    wd.run_tick()
    cids = [cid for _, cid in rec.recover_calls]
    assert cids[0] != cids[1]
    assert all(cid is not None and cid.startswith("tick_") for cid in cids)


# ---- artifact -------------------------------------------------------------


def test_artifact_written_when_debug_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "var" / "artifacts" / "watchdog"
    monkeypatch.setattr("automation.watchdog.ARTIFACTS_DIR", artifacts)
    orch = _MockOrchestrator(canned_result=_ok_result())
    wd = Watchdog(orch, debug=True)  # type: ignore[arg-type]
    wd.run_tick()
    subdirs = list(artifacts.iterdir())
    assert len(subdirs) == 1
    md = json.loads((subdirs[0] / "metadata.json").read_text())
    assert "correlation_id" in md
    assert md["timeout_exceeded"] is False
    assert md["raised"] is None
    assert md["immediate_health"]["degraded"] is False


def test_artifact_records_timeout_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "var" / "artifacts" / "watchdog"
    monkeypatch.setattr("automation.watchdog.ARTIFACTS_DIR", artifacts)
    orch = _MockOrchestrator(
        canned_result=_miss_result(),
        sleep_ms=150,
    )
    rec = _MockRecovery()
    wd = Watchdog(
        orch,  # type: ignore[arg-type]
        recovery=rec,
        debug=True,
        timeout_budgets_ms={
            "search_only": 50, "validated": 100, "validated_retry": 100,
        },
    )
    wd.run_tick()
    md = json.loads(next(iter(artifacts.iterdir())).joinpath("metadata.json").read_text())
    assert md["timeout_exceeded"] is True
    assert md["recovery"]["attempted"] is True
    assert md["recovery"]["succeeded"] is True


def test_artifact_skipped_when_debug_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "var" / "artifacts" / "watchdog"
    monkeypatch.setattr("automation.watchdog.ARTIFACTS_DIR", artifacts)
    orch = _MockOrchestrator(canned_result=_ok_result())
    wd = Watchdog(orch, debug=False)  # type: ignore[arg-type]
    wd.run_tick()
    if artifacts.exists():
        assert not any(artifacts.iterdir())


def test_artifact_env_var_enables_debug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "var" / "artifacts" / "watchdog"
    monkeypatch.setattr("automation.watchdog.ARTIFACTS_DIR", artifacts)
    monkeypatch.setenv("WATCHDOG_DEBUG", "1")
    orch = _MockOrchestrator(canned_result=_ok_result())
    wd = Watchdog(orch)  # type: ignore[arg-type]  # debug defaults from env
    assert wd.debug is True
    wd.run_tick()
    assert any(artifacts.iterdir())


def test_artifact_write_failure_does_not_break_run_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing artifact write must not propagate."""
    monkeypatch.setattr(
        "automation.watchdog.ARTIFACTS_DIR",
        Path("/proc/forbidden/watchdog"),
    )
    orch = _MockOrchestrator(canned_result=_ok_result())
    wd = Watchdog(orch, debug=True)  # type: ignore[arg-type]
    r = wd.run_tick()
    assert r.success is True


# =============================================================================
# Phase 8B: heartbeat wiring
# =============================================================================


@dataclass
class _CaptureHeartbeat:
    """Fake `HeartbeatWriter` recording every `beat()` call."""

    calls: list[tuple[str, object]] = field(default_factory=list)
    raise_on_beat: bool = False

    def beat(self, correlation_id, runtime_health, *, ts=None):  # type: ignore[no-untyped-def]
        if self.raise_on_beat:
            raise RuntimeError("synthetic beat failure")
        self.calls.append((correlation_id, runtime_health))


def test_phase8b_heartbeat_called_after_successful_tick() -> None:
    orch = _MockOrchestrator(canned_result=_ok_result())
    hb = _CaptureHeartbeat()
    wd = Watchdog(orch, heartbeat=hb)  # type: ignore[arg-type]
    wd.run_tick()
    assert len(hb.calls) == 1
    cid, health = hb.calls[0]
    assert cid.startswith("tick_")
    assert health is wd.last_health


def test_phase8b_heartbeat_called_after_failed_tick() -> None:
    orch = _MockOrchestrator(raise_on_tick=CaptureError("x"))
    hb = _CaptureHeartbeat()
    wd = Watchdog(orch, heartbeat=hb)  # type: ignore[arg-type]
    wd.run_tick()
    # Heartbeat still written so the L2 watchdog sees the degraded
    # health rather than nothing.
    assert len(hb.calls) == 1
    cid, health = hb.calls[0]
    assert cid.startswith("tick_")
    assert health.degraded is True


def test_phase8b_correlation_id_unique_per_tick() -> None:
    orch = _MockOrchestrator(canned_result=_ok_result())
    hb = _CaptureHeartbeat()
    wd = Watchdog(orch, heartbeat=hb)  # type: ignore[arg-type]
    wd.run_tick()
    wd.run_tick()
    wd.run_tick()
    assert len(hb.calls) == 3
    cids = [cid for cid, _ in hb.calls]
    assert len(set(cids)) == 3  # all distinct


def test_phase8b_heartbeat_failure_does_not_break_run_tick() -> None:
    """A failing heartbeat write must not affect the supervised TickResult."""
    orch = _MockOrchestrator(canned_result=_ok_result())
    hb = _CaptureHeartbeat(raise_on_beat=True)
    wd = Watchdog(orch, heartbeat=hb)  # type: ignore[arg-type]
    r = wd.run_tick()
    assert r.success is True


def test_phase8b_no_heartbeat_when_argument_is_none() -> None:
    """If no heartbeat is wired, no auto-write happens — Phase-7 behaviour."""
    orch = _MockOrchestrator(canned_result=_ok_result())
    wd = Watchdog(orch, heartbeat=None)  # type: ignore[arg-type]
    wd.run_tick()
    # Just verifies construction + run with `heartbeat=None` does not raise.


def test_phase8b_heartbeat_receives_post_recovery_health() -> None:
    """When recovery runs, the heartbeat carries the post-recovery health."""
    orch = _MockOrchestrator(raise_on_tick=CaptureError("x"))
    recovery_health = RuntimeHealth(
        sensor_ok=True, matcher_ok=True, actuator_ok=True,
        orchestrator_ok=True, last_error="CaptureError: x",
        degraded=True, ts=_dt.datetime.now(tz=_UTC),
    )
    rec = _MockRecovery(health_to_return=recovery_health)
    hb = _CaptureHeartbeat()
    wd = Watchdog(orch, recovery=rec, heartbeat=hb)  # type: ignore[arg-type]
    wd.run_tick()
    _, health = hb.calls[0]
    assert health is recovery_health  # republished from recovery
