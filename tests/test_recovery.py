"""RecoveryManager tests — orchestrator reset + ADB re-check + best-effort."""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

import pytest

from automation.errors import ADBError, InvalidTransitionError
from automation.recovery import RecoveryManager
from automation.runtime_health import RuntimeHealth
from automation.state import ALLOWED_TRANSITIONS, State


# ---- mocks -----------------------------------------------------------------


@dataclass
class _MockOrchestrator:
    """Standalone fake exposing `state`, `reset()`, and `_transition()`."""

    state: State = State.IDLE
    transitions: list[tuple[State, str]] = field(default_factory=list)
    reset_calls: int = 0
    raise_on_reset: bool = False

    # Match the orchestrator's centralized chokepoint signature.
    def _transition(self, to_state: State, *, reason: str) -> None:
        if to_state not in ALLOWED_TRANSITIONS[self.state]:
            raise InvalidTransitionError(
                f"illegal {self.state.value} → {to_state.value}"
            )
        self.transitions.append((to_state, reason))
        self.state = to_state

    def reset(self) -> None:
        self.reset_calls += 1
        if self.raise_on_reset:
            raise RuntimeError("synthetic reset failure")
        if self.state is not State.FAILED:
            raise InvalidTransitionError("reset requires FAILED")
        self._transition(State.IDLE, reason="reset")


@dataclass
class _MockADB:
    """Standalone fake of `ADB.get_state()`."""

    state_value: str = "device"
    raise_adb_error: bool = False
    raise_generic: bool = False

    def get_state(self) -> str:
        if self.raise_adb_error:
            raise ADBError("synthetic adb failure")
        if self.raise_generic:
            raise RuntimeError("synthetic non-adb error")
        return self.state_value


@dataclass
class _MockLogger:
    """Standalone fake of `StructuredLogger.log_error`."""

    error_calls: list[dict[str, Any]] = field(default_factory=list)
    raise_on_log: bool = False

    def log_error(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        if self.raise_on_log:
            raise RuntimeError("synthetic log failure")
        self.error_calls.append(kwargs)


# ---- orchestrator reset paths ----------------------------------------------


def test_recover_from_failed_state_returns_to_idle() -> None:
    orch = _MockOrchestrator(state=State.FAILED)
    adb = _MockADB()
    mgr = RecoveryManager(orch, adb)  # type: ignore[arg-type]
    health = mgr.recover(ValueError("test"))
    assert orch.state is State.IDLE
    assert orch.reset_calls == 1
    assert health.orchestrator_ok is True


def test_recover_from_mid_fsm_searching_forces_failed_then_resets() -> None:
    orch = _MockOrchestrator(state=State.SEARCHING)
    adb = _MockADB()
    mgr = RecoveryManager(orch, adb)  # type: ignore[arg-type]
    mgr.recover(ValueError("mid-tick fault"))
    assert orch.state is State.IDLE
    # We see: SEARCHING → FAILED (transition), FAILED → IDLE (reset).
    assert (State.FAILED, "recovery: force FAILED") in orch.transitions
    assert orch.reset_calls == 1


def test_recover_from_mid_fsm_acting_forces_failed_then_resets() -> None:
    orch = _MockOrchestrator(state=State.ACTING)
    adb = _MockADB()
    mgr = RecoveryManager(orch, adb)  # type: ignore[arg-type]
    mgr.recover(ValueError("mid-tick fault"))
    assert orch.state is State.IDLE


def test_recover_from_mid_fsm_validating_forces_failed_then_resets() -> None:
    orch = _MockOrchestrator(state=State.VALIDATING)
    adb = _MockADB()
    mgr = RecoveryManager(orch, adb)  # type: ignore[arg-type]
    mgr.recover(ValueError("mid-validate fault"))
    assert orch.state is State.IDLE


def test_recover_from_idle_is_noop_for_fsm() -> None:
    """If FSM is already IDLE, recovery doesn't touch it."""
    orch = _MockOrchestrator(state=State.IDLE)
    adb = _MockADB()
    mgr = RecoveryManager(orch, adb)  # type: ignore[arg-type]
    mgr.recover(ValueError("non-fsm fault"))
    assert orch.state is State.IDLE
    assert orch.reset_calls == 0
    assert orch.transitions == []


def test_recover_marks_orchestrator_unhealthy_when_reset_raises() -> None:
    orch = _MockOrchestrator(state=State.FAILED, raise_on_reset=True)
    adb = _MockADB()
    mgr = RecoveryManager(orch, adb)  # type: ignore[arg-type]
    health = mgr.recover(ValueError("test"))
    assert health.orchestrator_ok is False
    assert "reset failed" in (health.last_error or "")


# ---- ADB re-check paths ----------------------------------------------------


def test_recover_marks_sensor_actuator_unhealthy_when_adb_errors() -> None:
    orch = _MockOrchestrator(state=State.IDLE)
    adb = _MockADB(raise_adb_error=True)
    mgr = RecoveryManager(orch, adb)  # type: ignore[arg-type]
    health = mgr.recover(ValueError("test"))
    assert health.sensor_ok is False
    assert health.actuator_ok is False
    assert health.matcher_ok is True  # CPU-bound; unaffected
    assert "adb get-state failed" in (health.last_error or "")


def test_recover_marks_sensor_actuator_unhealthy_on_non_device_state() -> None:
    """`adb get-state` returning anything other than 'device' is degraded."""
    orch = _MockOrchestrator(state=State.IDLE)
    adb = _MockADB(state_value="unauthorized")
    mgr = RecoveryManager(orch, adb)  # type: ignore[arg-type]
    health = mgr.recover(ValueError("test"))
    assert health.sensor_ok is False
    assert health.actuator_ok is False
    assert "unauthorized" in (health.last_error or "")


def test_recover_handles_unexpected_adb_exception() -> None:
    """Non-ADBError exceptions from `adb.get_state` are caught pessimistically."""
    orch = _MockOrchestrator(state=State.IDLE)
    adb = _MockADB(raise_generic=True)
    mgr = RecoveryManager(orch, adb)  # type: ignore[arg-type]
    health = mgr.recover(ValueError("test"))
    assert health.sensor_ok is False
    assert health.actuator_ok is False


def test_recover_clean_path_returns_degraded_due_to_original_error() -> None:
    """Even with a clean recovery, degraded=True because the input was an error."""
    orch = _MockOrchestrator(state=State.IDLE)
    adb = _MockADB(state_value="device")
    mgr = RecoveryManager(orch, adb)  # type: ignore[arg-type]
    health = mgr.recover(ValueError("the fault that triggered recovery"))
    # All subsystems back to OK after recovery.
    assert health.sensor_ok and health.matcher_ok
    assert health.actuator_ok and health.orchestrator_ok
    # But degraded=True because last_error carries the original cause.
    assert health.degraded is True
    assert "ValueError" in (health.last_error or "")


# ---- best-effort logging ---------------------------------------------------


def test_recover_emits_log_error_when_logger_supplied() -> None:
    orch = _MockOrchestrator(state=State.FAILED)
    adb = _MockADB()
    logger = _MockLogger()
    mgr = RecoveryManager(orch, adb, logger=logger)  # type: ignore[arg-type]
    mgr.recover(ValueError("oops"), correlation_id="tick_test_abcdef")
    assert len(logger.error_calls) == 1
    call = logger.error_calls[0]
    assert call["correlation_id"] == "tick_test_abcdef"
    assert call["error_type"] == "ValueError"
    assert call["message"] == "oops"
    assert "recovery" in call["extra"].get("phase", "")


def test_recover_swallows_logger_failure() -> None:
    """Telemetry failure during recovery cannot crash the framework."""
    orch = _MockOrchestrator(state=State.FAILED)
    adb = _MockADB()
    logger = _MockLogger(raise_on_log=True)
    mgr = RecoveryManager(orch, adb, logger=logger)  # type: ignore[arg-type]
    # Must not raise:
    health = mgr.recover(ValueError("test"))
    assert isinstance(health, RuntimeHealth)


def test_recover_works_without_logger() -> None:
    """Logger argument is optional."""
    orch = _MockOrchestrator(state=State.FAILED)
    adb = _MockADB()
    mgr = RecoveryManager(orch, adb)  # type: ignore[arg-type]
    health = mgr.recover(ValueError("test"))
    assert isinstance(health, RuntimeHealth)


# ---- return-contract guarantees --------------------------------------------


def test_recover_always_returns_runtime_health() -> None:
    """Even with maximal failure (reset raises AND adb raises), recover() must
    not raise; it returns a RuntimeHealth describing the situation."""
    orch = _MockOrchestrator(state=State.FAILED, raise_on_reset=True)
    adb = _MockADB(raise_adb_error=True)
    mgr = RecoveryManager(orch, adb)  # type: ignore[arg-type]
    health = mgr.recover(ValueError("triple failure"))
    assert isinstance(health, RuntimeHealth)
    assert health.orchestrator_ok is False
    assert health.sensor_ok is False
    assert health.actuator_ok is False
    assert health.degraded is True


def test_recover_preserves_original_error_in_last_error() -> None:
    orch = _MockOrchestrator(state=State.IDLE)
    adb = _MockADB()
    mgr = RecoveryManager(orch, adb)  # type: ignore[arg-type]
    err = ADBError("device pulled mid-tick")
    health = mgr.recover(err)
    assert "ADBError" in (health.last_error or "")
    assert "device pulled mid-tick" in (health.last_error or "")


def test_recover_ts_is_tz_aware_utc() -> None:
    orch = _MockOrchestrator(state=State.IDLE)
    adb = _MockADB()
    mgr = RecoveryManager(orch, adb)  # type: ignore[arg-type]
    health = mgr.recover(ValueError("x"))
    assert health.ts.tzinfo is not None
    assert health.ts.tzinfo.utcoffset(health.ts) == _dt.timedelta(0)
