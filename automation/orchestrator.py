"""Phase-5 orchestration layer — `Orchestrator` composes SENSE/THINK/ACT.

This module realises the Phase-5 scope: a *minimal* hand-rolled FSM
(per ADR-08) that drives one tick from `IDLE` through SEARCHING →
ACTING → VALIDATING and lands in either `IDLE` (success) or
`FAILED`. Five states; explicit transitions only; single-template,
single-tick, single-retry semantics.

Out of scope (per the Phase 5 prompt, deferred to Phase 6+):

- watchdog, heartbeat;
- recovery cascade (`RESET_LITE` / `RESET_HARD` / `RECONNECTING`);
- asyncio / loop / `run()`;
- per-state timeouts (Phase 6 instrumentation owns these);
- persistent retry counters across ticks;
- screen registry / Script / multi-template strategy;
- telemetry (logs are at DEBUG via stdlib `logging`).

Validation cycle:

After a successful ADB invocation the orchestrator re-captures and
re-matches the same template. If the template is *still found*, one
extra capture+match is attempted. If the template is found a second
time, the tick lands in `FAILED`. The retry budget is per tick and
not persisted.

Debug artifacts: when `ORCH_DEBUG=1` (or `Orchestrator(debug=True)`),
each tick writes a per-invocation directory under
`var/artifacts/orchestrator/<ts>_<verdict>_<uuid>/` with a
`metadata.json` sidecar. Atomic `tmp` → rename; no screenshots —
the lower layers own frame / template / heatmap artifacts.
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
from .errors import InvalidTransitionError
from .metrics import derive_tier
from .paths import ARTIFACTS
from .state import ALLOWED_TRANSITIONS, State
from .tick_result import TickResult

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .action_result import ActionResult
    from .actuator import Actuator
    from .frame import Frame
    from .logger import StructuredLogger
    from .match_result import MatchResult
    from .matcher import Matcher
    from .metrics import MetricsCollector
    from .sensor import Sensor
    from .template import Template
    from typing import Callable

_LOG = logging.getLogger(__name__)

ARTIFACTS_DIR: Path = ARTIFACTS / "orchestrator"

# The single validation retry budget. Per the Phase 5 prompt: one
# retry only, per tick, no exponential backoff, no persistence.
VALIDATION_RETRY_BUDGET: int = 1


def _parse_bool_env(var: str, default: bool = False) -> bool:
    raw = os.environ.get(var)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


class Orchestrator:
    """Single-tick orchestrator composing Sensor + Matcher + Actuator.

    Public surface:

    - `tick() -> TickResult` — run exactly one SENSE → THINK → ACT →
      VALIDATE cycle. Requires `state == IDLE` at entry; otherwise
      raises `InvalidTransitionError`.
    - `reset() -> None` — explicit `FAILED → IDLE` transition. The
      only way to leave `FAILED`. Raises `InvalidTransitionError`
      if called outside `FAILED`.
    - `state` (read-only property) — the current FSM state.

    Constructor params:

    - `sensor`         : a `Sensor` instance.
    - `matcher`        : a `Matcher` instance.
    - `actuator`       : an `Actuator` instance.
    - `template`       : the single `Template` to search/validate against.
    - `logger`         : optional `StructuredLogger`. If supplied,
                         every tick emits one record to `ticks.jsonl`,
                         and any framework-level errors raised during
                         the tick path emit one record to
                         `errors.jsonl`. Phase 6 instrumentation; not
                         required.
    - `metrics`        : optional `MetricsCollector`. If supplied,
                         every tick / action / match is observed
                         (counter + per-tier histogram). Phase 6
                         instrumentation; not required.
    - `correlation_id_factory` : optional zero-arg callable returning
                         a `CorrelationId` for the next tick.
                         Defaults to `correlation.new_id`. Override
                         in tests for deterministic ids.
    - `debug`          : write per-tick metadata to
                         `var/artifacts/orchestrator/`. If `None`,
                         consults `ORCH_DEBUG` env var at construction
                         time only (ADR-13: no runtime config mutation).

    Threading: the orchestrator is single-threaded. Phase 5 does not
    introduce asyncio; the underlying sensor / matcher / actuator are
    invoked synchronously. Phase 6+ can wrap `tick()` in a coroutine
    when the loop arrives.

    Instrumentation philosophy (Phase 6, ADR-12):

    - The instrumentation *wraps*, not redesigns, the FSM. Every
      transition still routes through `_transition`; that method
      remains the chokepoint.
    - All three observability sinks (logger, metrics, artifacts)
      are *optional*. The orchestrator works correctly without any
      of them — the Phase 5 behaviour is preserved exactly.
    - Errors raised inside the instrumentation path are logged at
      WARN and swallowed; instrumentation cannot break the tick.
    """

    def __init__(
        self,
        sensor: "Sensor",
        matcher: "Matcher",
        actuator: "Actuator",
        template: "Template",
        *,
        logger: "StructuredLogger | None" = None,
        metrics: "MetricsCollector | None" = None,
        correlation_id_factory: "Callable[[], CorrelationId] | None" = None,
        debug: bool | None = None,
    ) -> None:
        self.sensor: "Sensor" = sensor
        self.matcher: "Matcher" = matcher
        self.actuator: "Actuator" = actuator
        self.template: "Template" = template
        self._state: State = State.IDLE
        self.logger: "StructuredLogger | None" = logger
        self.metrics: "MetricsCollector | None" = metrics
        self._correlation_id_factory: "Callable[[], CorrelationId]" = (
            correlation_id_factory
            if correlation_id_factory is not None
            else new_correlation_id
        )
        self._current_correlation_id: "CorrelationId | None" = None
        self.debug: bool = (
            debug if debug is not None else _parse_bool_env("ORCH_DEBUG")
        )

    # ---- read-only state surface -------------------------------------

    @property
    def state(self) -> State:
        """The FSM's current state. Read-only."""
        return self._state

    # ---- explicit transitions ----------------------------------------

    def reset(self) -> None:
        """Move FAILED → IDLE. Raises if called from any other state.

        This is the ONLY way to leave FAILED. The orchestrator does not
        auto-reset inside `tick()` — landing in FAILED forces the
        caller to acknowledge the prior failure before continuing.
        """
        if self._state is not State.FAILED:
            raise InvalidTransitionError(
                f"reset() requires state=FAILED (only FAILED → IDLE is allowed), "
                f"got state={self._state.value}"
            )
        self._transition(State.IDLE, reason="reset()")

    # ---- the tick ----------------------------------------------------

    def tick(self) -> TickResult:
        """Run exactly one SENSE → THINK → ACT → VALIDATE cycle.

        Pre-condition: `self.state == IDLE`. The method raises
        `InvalidTransitionError` otherwise (no auto-recovery from
        `FAILED`).

        The five FSM states are visited in this order on the happy
        path: `IDLE → SEARCHING → ACTING → VALIDATING → IDLE`. Each
        non-happy exit drops the FSM into `FAILED`:

        - MISS at SEARCHING: `SEARCHING → FAILED`.
        - ADB failure at ACTING: `ACTING → FAILED`.
        - Template still present after the action + 1 retry:
          `VALIDATING → FAILED`.

        Always returns a `TickResult`. Raises only:

        - `InvalidTransitionError` if called outside `IDLE`.
        - Any exception bubbled up from `sensor.capture()`,
          `matcher.match()`, or `actuator.tap()` — those are
          subsystem-level faults that v1.0 Phase 5 does not
          translate; future phases may wrap into `OrchestratorError`.
        """
        if self._state is not State.IDLE:
            raise InvalidTransitionError(
                f"tick() requires state=IDLE, got state={self._state.value}; "
                f"call reset() first"
            )
        state_before = self._state
        # One correlation id per tick. Held on the instance so the
        # artifact writer and any instrumentation hook downstream can
        # read it without threading it through every internal helper.
        self._current_correlation_id = self._correlation_id_factory()
        t_start = time.perf_counter_ns()

        # IDLE → SEARCHING ---------------------------------------------
        self._transition(State.SEARCHING, reason="tick() entry")

        # 1. capture
        search_frame: "Frame" = self.sensor.capture()

        # 2. match
        search_match: "MatchResult" = self.matcher.match(
            search_frame, self.template
        )

        capture_latency_ms = float(search_frame.capture_latency_ms)
        match_latency_ms = float(search_match.match_latency_ms)

        # Branch 1: MISS at SEARCH -------------------------------------
        if not search_match.found:
            self._transition(State.FAILED, reason="search miss")
            return self._finalize(
                state_before=state_before,
                t_start=t_start,
                capture_latency_ms=capture_latency_ms,
                match_latency_ms=match_latency_ms,
                action_latency_ms=None,
                action_result=None,
                search_match=search_match,
                validation_match=None,
                retries_used=0,
            )

        # SEARCHING → ACTING -------------------------------------------
        self._transition(State.ACTING, reason="search hit")

        center = search_match.center()
        # Validated by MatchResult: when found=True, center() returns a
        # non-None tuple. The `assert` documents the invariant.
        assert center is not None
        x_ref, y_ref = center

        # 3. act
        action_result: "ActionResult" = self.actuator.tap(
            x_ref,
            y_ref,
            search_frame.native_width,
            search_frame.native_height,
        )
        action_latency_ms = float(action_result.latency_ms)

        # Branch 2: ADB failure at ACT ---------------------------------
        if not action_result.success:
            self._transition(State.FAILED, reason="action failed")
            return self._finalize(
                state_before=state_before,
                t_start=t_start,
                capture_latency_ms=capture_latency_ms,
                match_latency_ms=match_latency_ms,
                action_latency_ms=action_latency_ms,
                action_result=action_result,
                search_match=search_match,
                validation_match=None,
                retries_used=0,
            )

        # ACTING → VALIDATING ------------------------------------------
        self._transition(State.VALIDATING, reason="action sent")

        # 4. validate (with one allowed retry)
        retries_used = 0
        validation_match: "MatchResult | None" = None
        while True:
            validation_match = self._validate_cycle()
            if not validation_match.found:
                break  # template gone → success
            if retries_used >= VALIDATION_RETRY_BUDGET:
                break  # retry exhausted → will fall through to FAIL
            retries_used += 1
            _LOG.debug(
                "validation retry %d: template still present (conf=%.3f)",
                retries_used, validation_match.confidence,
            )

        # Branch 3a: validation success --------------------------------
        if not validation_match.found:
            self._transition(State.IDLE, reason="validation passed")
            return self._finalize(
                state_before=state_before,
                t_start=t_start,
                capture_latency_ms=capture_latency_ms,
                match_latency_ms=match_latency_ms,
                action_latency_ms=action_latency_ms,
                action_result=action_result,
                search_match=search_match,
                validation_match=validation_match,
                retries_used=retries_used,
            )

        # Branch 3b: validation failed ---------------------------------
        self._transition(State.FAILED, reason="validation failed")
        return self._finalize(
            state_before=state_before,
            t_start=t_start,
            capture_latency_ms=capture_latency_ms,
            match_latency_ms=match_latency_ms,
            action_latency_ms=action_latency_ms,
            action_result=action_result,
            search_match=search_match,
            validation_match=validation_match,
            retries_used=retries_used,
        )

    # ---- internals ---------------------------------------------------

    def _validate_cycle(self) -> "MatchResult":
        """Run one capture + match cycle to verify the template is gone.

        Returns the `MatchResult`. The caller decides (a) whether to
        retry, (b) the FSM transition. `_validate_cycle` is a thin
        wrapper around `Sensor.capture` + `Matcher.match` named
        specifically so the validation surface is visible in tracebacks
        and easy to mock in tests.
        """
        frame = self.sensor.capture()
        return self.matcher.match(frame, self.template)

    def _transition(self, to_state: State, *, reason: str) -> None:
        """Move the FSM to `to_state`. Raises if disallowed.

        Centralizing transitions through this method ensures the
        allowed-transitions table is the single source of truth — no
        bypass via direct `self._state = ...` assignment.
        """
        if to_state not in ALLOWED_TRANSITIONS[self._state]:
            raise InvalidTransitionError(
                f"illegal FSM transition {self._state.value} → {to_state.value} "
                f"(reason: {reason}); "
                f"allowed next states from {self._state.value}: "
                f"{sorted(s.value for s in ALLOWED_TRANSITIONS[self._state])}"
            )
        _LOG.debug(
            "state transition %s → %s (reason: %s)",
            self._state.value, to_state.value, reason,
        )
        self._state = to_state

    def _finalize(
        self,
        *,
        state_before: State,
        t_start: int,
        capture_latency_ms: float,
        match_latency_ms: float,
        action_latency_ms: float | None,
        action_result: "ActionResult | None",
        search_match: "MatchResult",
        validation_match: "MatchResult | None",
        retries_used: int,
    ) -> TickResult:
        """Build the final `TickResult` and optionally persist artifacts.

        Called from every tick exit branch. The tick latency is
        measured here (after all FSM work is done; before artifact I/O
        so the latency reflects the tick proper, not the diagnostic
        overhead).
        """
        t_end = time.perf_counter_ns()
        tick_latency_ms = (t_end - t_start) / 1e6
        ts = _dt.datetime.now(tz=_dt.timezone.utc)
        success = (self._state is State.IDLE)
        result = TickResult(
            state_before=state_before,
            state_after=self._state,
            success=success,
            tick_latency_ms=tick_latency_ms,
            capture_latency_ms=capture_latency_ms,
            match_latency_ms=match_latency_ms,
            action_latency_ms=action_latency_ms,
            ts=ts,
        )
        # Tier derivation drives metrics and artifact naming. See
        # `metrics.derive_tier` for the rule; the orchestrator owns
        # the observables (action_ran / validation_ran / retries_used).
        tier = derive_tier(
            action_ran=action_result is not None,
            validation_ran=validation_match is not None,
            retries_used=retries_used,
        )
        correlation_id = self._current_correlation_id
        # `correlation_id` is guaranteed non-None here because tick()
        # generates it before entering SEARCHING and _finalize is only
        # reached from inside tick().
        assert correlation_id is not None

        _LOG.debug(
            "tick complete: %s tick=%.2f ms (capture=%.2f match=%.2f action=%s) "
            "retries=%d tier=%s correlation_id=%s",
            result.summary(),
            tick_latency_ms, capture_latency_ms, match_latency_ms,
            f"{action_latency_ms:.2f}" if action_latency_ms is not None else "—",
            retries_used, tier, correlation_id,
        )

        # Structured log — best-effort.
        if self.logger is not None:
            try:
                self.logger.log_tick(
                    correlation_id=correlation_id,
                    state_before=result.state_before.value,
                    state_after=result.state_after.value,
                    success=result.success,
                    tick_latency_ms=result.tick_latency_ms,
                    capture_latency_ms=result.capture_latency_ms,
                    match_latency_ms=result.match_latency_ms,
                    action_latency_ms=result.action_latency_ms,
                    retries_used=retries_used,
                    ts=result.ts,
                    extra={"tier": tier, "template": self.template.name},
                )
            except Exception as exc:  # noqa: BLE001 — best-effort sink
                _LOG.warning("structured log failed: %s", exc)

        # Metrics — best-effort.
        if self.metrics is not None:
            try:
                self.metrics.observe_tick(
                    latency_ms=result.tick_latency_ms,
                    tier=tier,
                    success=result.success,
                    retries_used=retries_used,
                )
                self.metrics.observe_match(latency_ms=result.match_latency_ms)
                if action_result is not None:
                    self.metrics.observe_action(
                        action_type=action_result.action_type,
                        latency_ms=action_result.latency_ms,
                    )
            except Exception as exc:  # noqa: BLE001 — best-effort sink
                _LOG.warning("metrics observation failed: %s", exc)

        if self.debug:
            self._write_artifacts(
                result=result,
                search_match=search_match,
                action_result=action_result,
                validation_match=validation_match,
                retries_used=retries_used,
                correlation_id=correlation_id,
                tier=tier,
            )
        return result

    # ---- debug artifacts ---------------------------------------------

    def _write_artifacts(
        self,
        *,
        result: TickResult,
        search_match: "MatchResult",
        action_result: "ActionResult | None",
        validation_match: "MatchResult | None",
        retries_used: int,
        correlation_id: CorrelationId,
        tier: str,
    ) -> None:
        """Write `metadata.json` for the tick. Best-effort; never raises.

        Schema (one file per tick, atomic write):

            {
              "correlation_id": str,
              "tier": "search_only" | "validated" | "validated_retry",
              "tick": {
                "state_before": "IDLE",
                "state_after":  "IDLE" | "FAILED",
                "success": bool,
                "tick_latency_ms": float,
                "capture_latency_ms": float,
                "match_latency_ms": float,
                "action_latency_ms": float | null,
                "ts": "ISO 8601"
              },
              "template": {"name": str, ...},
              "search_match":     <MatchResult.to_debug_dict>,
              "action_result":    <ActionResult.to_debug_dict> | null,
              "validation_match": <MatchResult.to_debug_dict> | null,
              "retries_used": int
            }

        The directory name embeds the correlation id so artifacts are
        cross-referenceable with `var/logs/ticks.jsonl` by name alone.
        """
        try:
            ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
            verdict = "ok" if result.success else "fail"
            cap_dir = ARTIFACTS_DIR / f"{correlation_id}_{verdict}_{tier}"
            cap_dir.mkdir(parents=True, exist_ok=True)

            metadata: dict[str, Any] = {
                "correlation_id": correlation_id,
                "tier": tier,
                "tick": dict(result.to_debug_dict()),
                "template": {
                    "name": self.template.name,
                    "width": int(self.template.width),
                    "height": int(self.template.height),
                    "threshold": float(self.template.threshold),
                },
                "search_match": dict(search_match.to_debug_dict()),
                "action_result": (
                    dict(action_result.to_debug_dict())
                    if action_result is not None
                    else None
                ),
                "validation_match": (
                    dict(validation_match.to_debug_dict())
                    if validation_match is not None
                    else None
                ),
                "retries_used": int(retries_used),
            }
            _atomic_write_bytes(
                cap_dir / "metadata.json",
                (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
            _LOG.debug("wrote orchestrator artifacts to %s", cap_dir)
        except (OSError, ValueError) as exc:
            _LOG.warning("could not write orchestrator artifacts: %s", exc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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
    "Orchestrator",
    "ARTIFACTS_DIR",
    "VALIDATION_RETRY_BUDGET",
]
