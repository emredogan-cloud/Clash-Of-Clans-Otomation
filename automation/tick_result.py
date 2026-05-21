"""The `TickResult` container — the single shape carried out of one tick.

Immutable record produced by `Orchestrator.tick()`. Captures:

- the FSM transition span (`state_before` → `state_after`);
- the boolean tick outcome (`success`);
- the four latency surfaces — overall tick wall-clock plus the three
  per-layer breakdowns copied from `Frame.capture_latency_ms`,
  `MatchResult.match_latency_ms`, and `ActionResult.latency_ms`;
- the UTC instant the tick completed.

Layer-latency semantics:

- `capture_latency_ms`: latency of the *initial* (search) capture.
  Validation re-captures are counted into `tick_latency_ms` but are
  not surfaced individually in v1.0 — Phase 6+ instrumentation will
  add per-cycle accounting.
- `match_latency_ms`: latency of the *initial* match (the SEARCH
  match). Same logic as capture.
- `action_latency_ms`: latency of the tap (or None when no action ran,
  e.g. when SEARCHING transitioned straight to FAILED).

`TickResult` is a container only — no FSM logic, no I/O, no side
effects. Construction validates field invariants and freezes the
record (frozen dataclass).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Mapping

from .state import State


@dataclass(frozen=True)
class TickResult:
    """The outcome of one `Orchestrator.tick()` invocation.

    Field semantics:

    - `state_before`         : the FSM state at the start of the tick.
                               Always `State.IDLE` in Phase 5 because
                               `tick()` refuses other entry points.
                               Carried explicitly so the contract is
                               readable in artifacts and so a future
                               Phase-5+ entry from a different state
                               can be added without changing the
                               container shape.
    - `state_after`          : the FSM state at the end of the tick.
                               Either `State.IDLE` (success) or
                               `State.FAILED` (any failure).
    - `success`              : True iff `state_after == State.IDLE` AND
                               the full SEARCH → ACT → VALIDATE
                               cycle completed. `success=False` means
                               the tick landed in `FAILED`.
    - `tick_latency_ms`      : total wall-clock duration of the tick,
                               measured with `perf_counter_ns` around
                               the entry/exit of `tick()`.
    - `capture_latency_ms`   : initial-capture latency copied from
                               `Frame.capture_latency_ms`.
    - `match_latency_ms`     : initial-match latency copied from
                               `MatchResult.match_latency_ms`.
    - `action_latency_ms`    : ACT latency copied from
                               `ActionResult.latency_ms`. `None` when
                               no action ran (SEARCH → FAILED).
    - `ts`                   : UTC instant at which the tick completed
                               (timezone-aware datetime).
    """

    state_before: State
    state_after: State
    success: bool
    tick_latency_ms: float
    capture_latency_ms: float
    match_latency_ms: float
    action_latency_ms: float | None
    ts: _dt.datetime

    def __post_init__(self) -> None:
        if not isinstance(self.state_before, State):
            raise TypeError(
                f"TickResult.state_before must be State, "
                f"got {type(self.state_before).__name__}"
            )
        if not isinstance(self.state_after, State):
            raise TypeError(
                f"TickResult.state_after must be State, "
                f"got {type(self.state_after).__name__}"
            )
        if not isinstance(self.success, bool):
            raise TypeError(
                f"TickResult.success must be bool, "
                f"got {type(self.success).__name__}"
            )
        for label, value in (
            ("tick_latency_ms", self.tick_latency_ms),
            ("capture_latency_ms", self.capture_latency_ms),
            ("match_latency_ms", self.match_latency_ms),
        ):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise TypeError(
                    f"TickResult.{label} must be a number, got {type(value).__name__}"
                )
            if value < 0:
                raise ValueError(
                    f"TickResult.{label} must be >= 0, got {value}"
                )
        if self.action_latency_ms is not None:
            if (
                not isinstance(self.action_latency_ms, (int, float))
                or isinstance(self.action_latency_ms, bool)
            ):
                raise TypeError(
                    f"TickResult.action_latency_ms must be number or None, "
                    f"got {type(self.action_latency_ms).__name__}"
                )
            if self.action_latency_ms < 0:
                raise ValueError(
                    f"TickResult.action_latency_ms must be >= 0, "
                    f"got {self.action_latency_ms}"
                )

        # success ↔ state_after == IDLE.
        if self.success and self.state_after is not State.IDLE:
            raise ValueError(
                f"success=True requires state_after=IDLE, "
                f"got state_after={self.state_after.value}"
            )
        if not self.success and self.state_after is not State.FAILED:
            raise ValueError(
                f"success=False requires state_after=FAILED, "
                f"got state_after={self.state_after.value}"
            )

        if not isinstance(self.ts, _dt.datetime):
            raise TypeError(
                f"TickResult.ts must be datetime, got {type(self.ts).__name__}"
            )
        if self.ts.tzinfo is None:
            raise ValueError("TickResult.ts must be timezone-aware (UTC)")

    # ------------------------------------------------------------------

    def to_debug_dict(self) -> Mapping[str, Any]:
        """JSON-serialisable summary suitable for `metadata.json` artifacts."""
        return {
            "state_before": self.state_before.value,
            "state_after": self.state_after.value,
            "success": self.success,
            "tick_latency_ms": float(self.tick_latency_ms),
            "capture_latency_ms": float(self.capture_latency_ms),
            "match_latency_ms": float(self.match_latency_ms),
            "action_latency_ms": (
                float(self.action_latency_ms)
                if self.action_latency_ms is not None
                else None
            ),
            "ts": self.ts.isoformat(),
        }

    def summary(self) -> str:
        """One-line human-readable summary."""
        verdict = "OK" if self.success else "FAIL"
        return (
            f"TickResult({verdict} "
            f"{self.state_before.value}→{self.state_after.value} "
            f"tick={self.tick_latency_ms:.1f} ms "
            f"capture={self.capture_latency_ms:.1f} match={self.match_latency_ms:.2f} "
            f"action="
            + (
                f"{self.action_latency_ms:.1f}"
                if self.action_latency_ms is not None
                else "—"
            )
            + ")"
        )


__all__ = ["TickResult"]
