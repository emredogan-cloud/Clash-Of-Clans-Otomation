"""Tick supervision: timeout detection + fault containment.

`Watchdog.run_tick()` wraps a single `Orchestrator.tick()` call:

1. Snapshot the watchdog's own correlation id (used for the
   per-supervision artifact and any recovery log line).
2. Call `orchestrator.tick()` inside `try/except Exception`. The
   FSM is **not** modified — the orchestrator is supervised in
   place per the Phase 7 prompt's "do NOT redesign orchestrator".
3. Measure elapsed wall-clock time with `perf_counter_ns`.
4. Derive a tier estimate from the returned `TickResult` (or use
   a pessimistic default if the tick raised).
5. Compare elapsed time against the tier's timeout budget; flag a
   `TimeoutFault` if exceeded (post-hoc soft enforcement).
6. Build a `RuntimeHealth` snapshot reflecting subsystem state.
7. If the tick raised or the budget was exceeded, invoke the
   optional `RecoveryManager.recover(...)` once (best-effort).
8. Emit a `WATCHDOG_DEBUG` artifact when enabled.
9. Return a `TickResult` to the caller. On a raised tick the
   watchdog returns a synthetic `IDLE → FAILED` result so the
   caller's downstream code can branch on `.success` uniformly.

Pre-emption model (Phase 7 honest stance):

- Timeouts are **measured post-hoc**, not enforced via SIGALRM /
  threads. The Phase 7 prompt prohibits "daemon", "threads",
  "infinite loop"; cleanly preempting a blocking subprocess call
  inside the supervised tick without one of these is not possible
  on CPython.
- The lower layers already enforce hard upper bounds via
  `subprocess.run(timeout=...)`: `ADB.shell` 10 s, `Sensor`
  capture path 30 s, `Actuator` action 10 s. So a truly hung tick
  is bounded by those numbers; the watchdog's budgets sit safely
  underneath them.
- A genuine Python-level infinite loop in `tick()` would not be
  caught by the watchdog. That is a bug, not a fault, and is
  acknowledged in `phase7-report.md` as a known limitation.

Threading: single-call supervision only. No daemon. The watchdog
holds no mutable state across calls beyond the `last_health`
publish slot.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .correlation import CorrelationId, new_id as new_correlation_id
from .errors import TimeoutFault
from .paths import ARTIFACTS
from .runtime_health import RuntimeHealth
from .state import State
from .tick_result import TickResult

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .orchestrator import Orchestrator
    from .recovery import RecoveryManager

_LOG = logging.getLogger(__name__)

ARTIFACTS_DIR: Path = ARTIFACTS / "watchdog"

# Default tier budgets per the Phase 7 prompt. All ms.
DEFAULT_TIMEOUT_BUDGETS_MS: dict[str, int] = {
    "search_only": 2500,
    "validated": 4000,
    "validated_retry": 5000,
}

# Absolute ceiling used when the tick raised (no tier derivable).
ABSOLUTE_BUDGET_MS: int = 6000


def _parse_bool_env(var: str, default: bool = False) -> bool:
    raw = os.environ.get(var)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


class Watchdog:
    """Supervise a single orchestrator tick.

    Public surface:

    - `run_tick() -> TickResult` — supervise one tick. Always
      returns a `TickResult`; never raises (caught exceptions are
      reflected in the result + `last_health`).
    - `last_health` (read-only property) — the most recent
      `RuntimeHealth` snapshot produced by the watchdog. Defaults
      to a healthy snapshot at construction time.

    Constructor params:

    - `orchestrator`        : the `Orchestrator` instance under
                              supervision.
    - `recovery`            : optional `RecoveryManager`. If supplied,
                              `run_tick()` invokes
                              `recovery.recover(...)` once on any
                              fault. If `None`, no recovery is
                              attempted.
    - `timeout_budgets_ms`  : optional override for the per-tier
                              budgets. Defaults to
                              `DEFAULT_TIMEOUT_BUDGETS_MS`.
    - `debug`               : write per-tick metadata to
                              `var/artifacts/watchdog/`. If `None`,
                              consults `WATCHDOG_DEBUG` env var at
                              construction time only (ADR-13).

    Threading: single-threaded. No daemon. No mutating state
    across instances. The `last_health` publish is the only
    instance state that changes after construction.
    """

    def __init__(
        self,
        orchestrator: "Orchestrator",
        *,
        recovery: "RecoveryManager | None" = None,
        timeout_budgets_ms: dict[str, int] | None = None,
        debug: bool | None = None,
    ) -> None:
        self.orchestrator: "Orchestrator" = orchestrator
        self.recovery: "RecoveryManager | None" = recovery
        self.timeout_budgets_ms: dict[str, int] = (
            dict(timeout_budgets_ms)
            if timeout_budgets_ms is not None
            else dict(DEFAULT_TIMEOUT_BUDGETS_MS)
        )
        # Validate budgets.
        for tier in ("search_only", "validated", "validated_retry"):
            if tier not in self.timeout_budgets_ms:
                raise ValueError(
                    f"timeout_budgets_ms missing required tier {tier!r}"
                )
            b = self.timeout_budgets_ms[tier]
            if not isinstance(b, int) or isinstance(b, bool):
                raise TypeError(
                    f"timeout_budgets_ms[{tier!r}] must be int, "
                    f"got {type(b).__name__}"
                )
            if b <= 0:
                raise ValueError(
                    f"timeout_budgets_ms[{tier!r}] must be > 0, got {b}"
                )

        self.debug: bool = (
            debug if debug is not None else _parse_bool_env("WATCHDOG_DEBUG")
        )
        self._last_health: RuntimeHealth = RuntimeHealth.healthy()

    # ---- public surface ---------------------------------------------

    @property
    def last_health(self) -> RuntimeHealth:
        """Most recent `RuntimeHealth` snapshot. Read-only."""
        return self._last_health

    def run_tick(self) -> TickResult:
        """Supervise one orchestrator tick.

        Always returns a `TickResult`:
        - on a normal tick, the orchestrator's own `TickResult`
          (latency + state flow unchanged);
        - on a raised tick, a synthetic `IDLE → FAILED` result
          carrying the elapsed time;
        - on a budget-exceeded tick, the orchestrator's result
          unchanged, but the watchdog's `last_health` is degraded
          and the WATCHDOG_DEBUG artifact carries `timeout=True`.
        """
        correlation_id = new_correlation_id()
        ts_start = _dt.datetime.now(tz=_dt.timezone.utc)
        t_start = time.perf_counter_ns()

        result: TickResult | None = None
        raised: Exception | None = None
        try:
            result = self.orchestrator.tick()
        except Exception as exc:  # noqa: BLE001 — fault containment
            raised = exc
            _LOG.warning(
                "watchdog[%s]: orchestrator.tick() raised %s: %s",
                correlation_id, type(exc).__name__, exc,
            )

        t_end = time.perf_counter_ns()
        elapsed_ms = (t_end - t_start) / 1e6

        # Derive tier estimate (used for budget selection).
        if result is not None:
            tier = _derive_tier_estimate(result)
        else:
            # No TickResult — use a conservative fallback budget so a
            # raised tick that nonetheless completed quickly is not
            # also flagged as a timeout.
            tier = "search_only"

        budget_ms = self.timeout_budgets_ms.get(tier, ABSOLUTE_BUDGET_MS)
        timeout_exceeded = elapsed_ms > budget_ms

        # Build / pass through the TickResult.
        returned_result = result if result is not None else _synthetic_failed(
            elapsed_ms=elapsed_ms, ts=ts_start,
        )

        # Compose health BEFORE recovery so we record the immediate
        # post-tick state. Recovery, if invoked, will publish its
        # own (possibly different) health snapshot — the watchdog
        # republishes that to `last_health`.
        immediate_health = _compose_health(
            error=raised,
            timeout_exceeded=timeout_exceeded,
            elapsed_ms=elapsed_ms,
            budget_ms=budget_ms,
            ts=ts_start,
        )
        self._last_health = immediate_health

        recovery_attempted = False
        recovery_succeeded: bool | None = None
        recovery_health: RuntimeHealth | None = None
        if (raised is not None or timeout_exceeded) and self.recovery is not None:
            recovery_attempted = True
            recovery_error = raised if raised is not None else TimeoutFault(
                f"tick {elapsed_ms:.2f} ms exceeded {tier} budget {budget_ms} ms"
            )
            try:
                recovery_health = self.recovery.recover(
                    recovery_error, correlation_id=correlation_id,
                )
            except Exception as exc:  # noqa: BLE001
                # Recovery itself failed. Containment is the
                # priority: log and continue. `last_health` keeps
                # the pre-recovery snapshot.
                _LOG.warning(
                    "watchdog[%s]: recovery raised %s: %s "
                    "(swallowed)", correlation_id, type(exc).__name__, exc,
                )
                recovery_succeeded = False
            else:
                # Recovery returned a snapshot. We publish it.
                self._last_health = recovery_health
                # A "successful" recovery is one where the
                # orchestrator is operable again. `degraded` may
                # still be True (last_error carries the original
                # cause), but `orchestrator_ok=True` means the next
                # tick can proceed.
                recovery_succeeded = recovery_health.orchestrator_ok

        # Artifact (best-effort).
        if self.debug:
            self._write_artifact(
                correlation_id=correlation_id,
                ts=ts_start,
                tier=tier,
                budget_ms=budget_ms,
                elapsed_ms=elapsed_ms,
                timeout_exceeded=timeout_exceeded,
                raised=raised,
                immediate_health=immediate_health,
                recovery_attempted=recovery_attempted,
                recovery_succeeded=recovery_succeeded,
                recovery_health=recovery_health,
                returned_result=returned_result,
            )

        return returned_result

    # ---- artifact helper --------------------------------------------

    def _write_artifact(
        self,
        *,
        correlation_id: CorrelationId,
        ts: _dt.datetime,
        tier: str,
        budget_ms: int,
        elapsed_ms: float,
        timeout_exceeded: bool,
        raised: Exception | None,
        immediate_health: RuntimeHealth,
        recovery_attempted: bool,
        recovery_succeeded: bool | None,
        recovery_health: RuntimeHealth | None,
        returned_result: TickResult,
    ) -> None:
        """Write `metadata.json` for one supervised tick. Best-effort."""
        try:
            ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
            verdict = "ok" if returned_result.success else "fail"
            cap_dir = ARTIFACTS_DIR / f"{correlation_id}_{verdict}_{tier}"
            cap_dir.mkdir(parents=True, exist_ok=True)

            metadata: dict[str, Any] = {
                "correlation_id": correlation_id,
                "ts": ts.isoformat(),
                "tier_estimate": tier,
                "budget_ms": int(budget_ms),
                "elapsed_ms": float(elapsed_ms),
                "timeout_exceeded": bool(timeout_exceeded),
                "raised": (
                    {
                        "type": type(raised).__name__,
                        "message": str(raised),
                    }
                    if raised is not None
                    else None
                ),
                "immediate_health": dict(immediate_health.to_debug_dict()),
                "recovery": {
                    "attempted": recovery_attempted,
                    "succeeded": recovery_succeeded,
                    "health": (
                        dict(recovery_health.to_debug_dict())
                        if recovery_health is not None
                        else None
                    ),
                },
                "returned_tick": dict(returned_result.to_debug_dict()),
            }
            _atomic_write_bytes(
                cap_dir / "metadata.json",
                (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode(
                    "utf-8"
                ),
            )
            _LOG.debug("watchdog: wrote artifact to %s", cap_dir)
        except (OSError, ValueError) as exc:
            _LOG.warning("watchdog: could not write artifact: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _derive_tier_estimate(result: TickResult) -> str:
    """Best-effort tier estimate from `TickResult` observables.

    The Phase-5 `TickResult` does not carry `retries_used`, so the
    watchdog cannot distinguish `validated` (1 validate cycle) from
    `validated_retry` (2 validate cycles) without reading the
    orchestrator's artifact. v1.0 simplification:

    - `action_latency_ms is None` → no action ran → `"search_only"`.
    - `action_latency_ms is set`  → action ran → `"validated_retry"`
      (use the more lenient budget; this is the conservative
      choice).

    This means the strict `validated` 4000 ms budget is NOT
    enforced separately in v1.0 Phase 7; an action-bearing tick
    is judged against the 5000 ms `validated_retry` budget.
    Documented in `phase7-report.md` §2. A Phase 8+ enhancement
    that surfaces `retries_used` on `TickResult` would let the
    watchdog enforce the strict tier — out of v1.0 scope.
    """
    if result.action_latency_ms is None:
        return "search_only"
    return "validated_retry"


def _synthetic_failed(
    *, elapsed_ms: float, ts: _dt.datetime,
) -> TickResult:
    """Build a synthetic `IDLE → FAILED` TickResult.

    Used when the supervised tick raised before producing one.
    Carries the measured elapsed time so callers see a non-zero
    latency. All other latency surfaces are zero — the inner
    capture/match/action figures are unknown.
    """
    return TickResult(
        state_before=State.IDLE,
        state_after=State.FAILED,
        success=False,
        tick_latency_ms=elapsed_ms,
        capture_latency_ms=0.0,
        match_latency_ms=0.0,
        action_latency_ms=None,
        ts=ts,
    )


def _compose_health(
    *,
    error: Exception | None,
    timeout_exceeded: bool,
    elapsed_ms: float,
    budget_ms: int,
    ts: _dt.datetime,
) -> RuntimeHealth:
    """Compose a `RuntimeHealth` from the raw supervision observables.

    Heuristic mapping of error type → unhealthy subsystem. Errors
    we don't recognise pessimistically mark `orchestrator_ok=False`.
    """
    if error is None and not timeout_exceeded:
        return RuntimeHealth.healthy(ts=ts)

    last_error: str
    if error is not None:
        last_error = f"{type(error).__name__}: {error}"
    else:
        last_error = (
            f"TimeoutFault: tick {elapsed_ms:.2f} ms exceeded "
            f"budget {budget_ms} ms"
        )

    sensor_ok = True
    matcher_ok = True
    actuator_ok = True
    orchestrator_ok = True

    if error is not None:
        err_name = type(error).__name__
        # Best-effort categorisation by exception name; matches
        # the prefixes used by SENSE / THINK / ACT / orchestrator.
        if any(
            tag in err_name
            for tag in ("Capture", "FrameDecode", "Sensor", "UnsupportedPixelFormat")
        ):
            sensor_ok = False
        elif any(tag in err_name for tag in ("ROI", "Match", "Matcher")):
            matcher_ok = False
        elif any(
            tag in err_name
            for tag in ("Coordinate", "ActionExecution", "Actuator")
        ):
            actuator_ok = False
        elif any(
            tag in err_name
            for tag in ("Orchestrator", "Transition", "ValidationError")
        ):
            orchestrator_ok = False
        elif "ADB" in err_name:
            # ADB errors below the supervised tick path → SENSE/ACT
            # both impacted (the ADB layer feeds both).
            sensor_ok = False
            actuator_ok = False
        else:
            # Unknown exception. Pessimistic: orchestrator is the
            # umbrella; the next tick must be supervised through
            # recovery before proceeding.
            orchestrator_ok = False
    elif timeout_exceeded:
        # Timeout without an exception ≡ "tick was slow". Cannot
        # know which subsystem hung. Mark orchestrator as the
        # umbrella unhealthy subsystem so the caller can branch.
        orchestrator_ok = False

    return RuntimeHealth(
        sensor_ok=sensor_ok,
        matcher_ok=matcher_ok,
        actuator_ok=actuator_ok,
        orchestrator_ok=orchestrator_ok,
        last_error=last_error,
        degraded=True,
        ts=ts,
    )


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically (tmp + fsync + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        shutil.move(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


__all__ = [
    "Watchdog",
    "ARTIFACTS_DIR",
    "DEFAULT_TIMEOUT_BUDGETS_MS",
    "ABSOLUTE_BUDGET_MS",
]
