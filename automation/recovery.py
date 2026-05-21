"""Best-effort recovery from predictable orchestrator faults.

Phase 7 recovery is intentionally narrow:

1. **Force the orchestrator back to `IDLE`.** If a tick raised
   mid-FSM (or the watchdog flagged a timeout while the
   orchestrator's state was `SEARCHING` / `ACTING` / `VALIDATING`),
   the FSM is in a state from which `tick()` cannot be re-entered.
   Recovery walks the FSM via the centralized `_transition`
   chokepoint (mid-FSM → `FAILED`) and then calls the public
   `reset()` to get to `IDLE`.

2. **Re-check ADB device connectivity.** Calls `adb.get_state()`.
   If the device is no longer in the `device` state (or the ADB
   call itself errors), the recovery snapshot marks SENSE and ACT
   as `not_ok`. The framework cannot do more here without root,
   reboots, or `kill-server` — all of which are explicitly out of
   Phase 7 scope.

Both steps are bounded:

- One attempt only per `recover()` call. No exponential backoff,
  no retry loops.
- No threads, no daemon, no signals.
- Every step is wrapped in a `try/except`. The recovery method
  ALWAYS returns a `RuntimeHealth` snapshot; it never raises
  `RecoveryError` to the caller. (The exception is reserved for
  future, opt-in raising semantics — Phase 7 keeps it as a typed
  contract only.)

The watchdog is the canonical caller. Operators or future
phase work can also invoke `RecoveryManager.recover(...)`
directly as part of a higher-level recovery cascade.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import TYPE_CHECKING

from .errors import ADBError
from .runtime_health import RuntimeHealth
from .state import State

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .adb import ADB
    from .logger import StructuredLogger
    from .orchestrator import Orchestrator

_LOG = logging.getLogger(__name__)


class RecoveryManager:
    """Best-effort, one-shot recovery for predictable faults.

    Constructor params:

    - `orchestrator` : the `Orchestrator` instance under supervision.
    - `adb`          : the `ADB` wrapper. Used for the device-state
                       re-check (`adb.get_state()`).
    - `logger`       : optional `StructuredLogger`. If supplied,
                       recovery emits one `errors.jsonl` record
                       per `recover()` call (best-effort; failures
                       are swallowed).
    - `correlation_id_factory`: optional. The watchdog typically
                       passes the active tick's correlation id so
                       the error record cross-references the
                       failed tick.

    Recovery does NOT:
    - reboot the device;
    - kill any apps;
    - require root or ADB elevation;
    - restart the ADB server (`kill-server` / `start-server` is a
      Phase 8 candidate per `SYSTEM-ROADMAP.md §11.1`'s
      `RESET_HARD` state; explicitly out of v1.0 Phase 7).
    """

    def __init__(
        self,
        orchestrator: "Orchestrator",
        adb: "ADB",
        *,
        logger: "StructuredLogger | None" = None,
    ) -> None:
        self.orchestrator: "Orchestrator" = orchestrator
        self.adb: "ADB" = adb
        self.logger: "StructuredLogger | None" = logger

    # ---- public API --------------------------------------------------

    def recover(
        self,
        error: Exception,
        *,
        correlation_id: str | None = None,
    ) -> RuntimeHealth:
        """Attempt one round of recovery. Always returns a `RuntimeHealth`.

        Steps:

        1. Try to bring the orchestrator's FSM back to `IDLE`. If
           the current state is not `IDLE`, transition through
           `FAILED` (if not already there) and call `reset()`.
        2. Re-check ADB device connectivity via `adb.get_state()`.
           If anything other than `"device"` is returned (or any
           exception is raised), SENSE and ACT are marked unhealthy.

        Failures inside `recover()` are logged at WARN and the
        relevant subsystem is marked `_ok=False`. The method does
        not raise.

        Returns a snapshot of post-recovery health.
        """
        ts = _dt.datetime.now(tz=_dt.timezone.utc)
        original_error_summary = f"{type(error).__name__}: {error}"

        # Step 1: orchestrator → IDLE -------------------------------
        orchestrator_ok = True
        reset_note: str | None = None
        try:
            self._reset_orchestrator()
        except Exception as exc:  # noqa: BLE001 — best-effort
            orchestrator_ok = False
            reset_note = f"reset failed: {type(exc).__name__}: {exc}"
            _LOG.warning("recovery: orchestrator reset failed: %s", exc)

        # Step 2: ADB re-check --------------------------------------
        sensor_ok = True
        actuator_ok = True
        matcher_ok = True  # matcher is CPU-bound; recovery cannot affect it
        adb_note: str | None = None
        try:
            state = self.adb.get_state().strip()
            if state != "device":
                sensor_ok = False
                actuator_ok = False
                adb_note = f"adb get-state returned {state!r}"
        except ADBError as exc:
            sensor_ok = False
            actuator_ok = False
            adb_note = f"adb get-state failed: {exc}"
        except Exception as exc:  # noqa: BLE001 — pessimistic
            sensor_ok = False
            actuator_ok = False
            adb_note = f"adb get-state errored: {type(exc).__name__}: {exc}"

        # Compose last_error: prefer the original cause, append any
        # recovery-step notes.
        last_error_parts: list[str] = [original_error_summary]
        if reset_note:
            last_error_parts.append(reset_note)
        if adb_note:
            last_error_parts.append(adb_note)
        last_error = " | ".join(last_error_parts)

        # `degraded` is forced True because recovery is only invoked
        # when something already went wrong. Even if every subsystem
        # is `_ok=True` after the recovery steps, the caller passed
        # us an `error` — `last_error` is non-empty — and
        # `RuntimeHealth` enforces the coupling.
        degraded = True

        health = RuntimeHealth(
            sensor_ok=sensor_ok,
            matcher_ok=matcher_ok,
            actuator_ok=actuator_ok,
            orchestrator_ok=orchestrator_ok,
            last_error=last_error,
            degraded=degraded,
            ts=ts,
        )

        # Best-effort structured-log emission. Failure here MUST NOT
        # propagate — telemetry faults during recovery would create
        # a recursive crash.
        if self.logger is not None:
            try:
                fsm_state = self.orchestrator.state.value
            except Exception:  # noqa: BLE001
                fsm_state = "UNKNOWN"
            try:
                self.logger.log_error(
                    correlation_id=correlation_id or "recovery_no_correlation",
                    error_type=type(error).__name__,
                    message=str(error),
                    state=fsm_state,
                    ts=ts,
                    extra={
                        "phase": "recovery",
                        "reset_note": reset_note,
                        "adb_note": adb_note,
                        "health": dict(health.to_debug_dict()),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "recovery: telemetry log failed (swallowed): %s", exc,
                )

        _LOG.info(
            "recovery: %s  (original error: %s)",
            health.summary(), original_error_summary,
        )
        return health

    # ---- internals ---------------------------------------------------

    def _reset_orchestrator(self) -> None:
        """Force the orchestrator's FSM back to `IDLE`.

        Allowed transitions (per `automation/state.py`):
        - `SEARCHING`  → `FAILED`
        - `ACTING`     → `FAILED`
        - `VALIDATING` → `FAILED`
        - `FAILED`     → `IDLE`

        `IDLE → FAILED` is NOT allowed; if the orchestrator is in
        `IDLE` (i.e. nothing to recover), this is a no-op.

        Uses the orchestrator's private `_transition` chokepoint
        (not a redesign — we are calling existing centralized
        behaviour on an existing allowed edge).
        """
        state = self.orchestrator.state
        if state is State.IDLE:
            return
        if state is not State.FAILED:
            # mid-FSM → FAILED is always allowed in the Phase 5 table.
            self.orchestrator._transition(
                State.FAILED, reason="recovery: force FAILED",
            )
        # FAILED → IDLE via the public method.
        self.orchestrator.reset()


__all__ = ["RecoveryManager"]
