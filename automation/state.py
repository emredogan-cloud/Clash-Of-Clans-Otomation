"""Phase-5 framework state enum + allowed-transitions table.

This is the *framework* state machine — pure SENSE → THINK → ACT
plumbing. Game / screen / scene states are out of scope; those belong
to a higher layer that does not exist in v1.0.

The five states are:

- `IDLE`        — between ticks. The only state from which `tick()`
                  may be called. The state machine is also in `IDLE`
                  after a successful validation.
- `SEARCHING`   — within a tick: a frame has been captured and the
                  matcher has been invoked. Exits to `ACTING` on a
                  HIT, to `FAILED` on a MISS.
- `ACTING`     — within a tick: the matcher found the template; the
                  actuator is about to (or has just) issued the input
                  event. Exits to `VALIDATING` on a successful ADB
                  invocation, to `FAILED` on an ADB failure.
- `VALIDATING` — within a tick: the action was issued. The orchestrator
                  re-captures + re-matches to confirm the template is
                  no longer present. Exits to `IDLE` on confirmation,
                  to `FAILED` after the single retry is also unsuccessful.
- `FAILED`     — terminal-until-reset. The orchestrator refuses to
                  run another tick from this state. The caller must
                  call `reset()` (an explicit `FAILED → IDLE`
                  transition) before invoking `tick()` again.

The transitions table below is the single source of truth. Any code
path that wants to move between states routes through
`Orchestrator._transition`, which consults this table and raises
`InvalidTransitionError` on disallowed moves.

There are deliberately no hidden transitions:

- No "auto-reset" inside `tick()`. A caller landing in `FAILED` must
  acknowledge it via `reset()`.
- No recovery cascade (`RESET_LITE` / `RESET_HARD` / `RECONNECTING`).
  Those belong to Phase 5+ orchestration work that is explicitly
  out of scope per the Phase 5 prompt.
- No timeouts. Phase 6+ adds per-state timeouts; the v1.0 Phase 5
  orchestrator runs one tick to completion or raises.

The allowed-transitions table can also be exported for documentation
(Mermaid / DOT) by a future tool; Phase 5 does not ship the exporter.
"""
from __future__ import annotations

import enum
from types import MappingProxyType
from typing import Mapping


@enum.unique
class State(enum.Enum):
    """Phase-5 framework states. Exactly five members.

    The string values are the canonical names used in logs, JSON
    artifacts (`TickResult.to_debug_dict`), and `metadata.json`
    sidecars. They are NOT translated; they are wire-stable in v1.0.
    """

    IDLE = "IDLE"
    SEARCHING = "SEARCHING"
    ACTING = "ACTING"
    VALIDATING = "VALIDATING"
    FAILED = "FAILED"


# Allowed transitions. Read this as "from `key`, the only legal next
# states are `value`". `MappingProxyType` makes the table immutable at
# runtime — callers cannot mutate it.
_ALLOWED_TRANSITIONS: dict[State, frozenset[State]] = {
    State.IDLE:       frozenset({State.SEARCHING}),
    State.SEARCHING:  frozenset({State.ACTING, State.FAILED}),
    State.ACTING:     frozenset({State.VALIDATING, State.FAILED}),
    State.VALIDATING: frozenset({State.IDLE, State.FAILED}),
    State.FAILED:     frozenset({State.IDLE}),  # only via reset()
}

ALLOWED_TRANSITIONS: Mapping[State, frozenset[State]] = MappingProxyType(
    _ALLOWED_TRANSITIONS
)


def is_allowed(from_state: State, to_state: State) -> bool:
    """Return True iff `from_state → to_state` is an allowed transition.

    Both arguments must be `State` instances; passing a string or any
    other type returns `False` (the caller is expected to type-check
    upstream, but this function is defensive).
    """
    if not isinstance(from_state, State) or not isinstance(to_state, State):
        return False
    return to_state in ALLOWED_TRANSITIONS[from_state]


def allowed_next(from_state: State) -> frozenset[State]:
    """Return the set of allowed next states for `from_state`.

    Raises `TypeError` if `from_state` is not a `State`.
    """
    if not isinstance(from_state, State):
        raise TypeError(
            f"from_state must be State, got {type(from_state).__name__}"
        )
    return ALLOWED_TRANSITIONS[from_state]


__all__ = ["State", "ALLOWED_TRANSITIONS", "is_allowed", "allowed_next"]
