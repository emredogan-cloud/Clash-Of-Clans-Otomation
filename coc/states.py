"""CoC trophy-drop FSM — gameplay-step state machine.

This is a *gameplay* state machine, distinct from the framework's
`automation.state` (which is the SENSE/THINK/ACT inner FSM of one
orchestrator tick). The 11 states here represent discrete steps
of one trophy-drop loop on Clash of Clans:

    HOME (initial) → ATTACK → FIND_MATCH → WAIT_VILLAGE →
    DROP_ARMY → WAIT_BATTLE → END_BATTLE → CONFIRM →
    RETURN_HOME → COMPLETE (terminal)

`FAILED` is the terminal state for any error class (missing
template, step timeout, actuator failure). v1.0 has no
recovery cascade — the bot is fail-safe, not auto-recovering.

The allowed-transitions table is enforced by `CoCTrophyBot`'s
`_transition` chokepoint, similar to the framework's
`Orchestrator._transition`.
"""
from __future__ import annotations

import enum
from types import MappingProxyType
from typing import Mapping


@enum.unique
class CoCState(enum.Enum):
    """One step in the trophy-drop gameplay loop."""

    HOME = "HOME"                # at the village; expect Attack button
    ATTACK = "ATTACK"            # tapped Attack; expect Find Match button
    FIND_MATCH = "FIND_MATCH"    # tapped Find Match; waiting for match
    WAIT_VILLAGE = "WAIT_VILLAGE"  # match found; enemy village loaded
    DROP_ARMY = "DROP_ARMY"      # in battle; deploying troops
    WAIT_BATTLE = "WAIT_BATTLE"  # waiting out the battle clock
    END_BATTLE = "END_BATTLE"    # tap surrender / end-battle button
    CONFIRM = "CONFIRM"          # tap confirm-end dialog
    RETURN_HOME = "RETURN_HOME"  # tap return-home button on result
    COMPLETE = "COMPLETE"        # one loop done; terminal
    FAILED = "FAILED"            # error; terminal


_ALLOWED: dict[CoCState, frozenset[CoCState]] = {
    CoCState.HOME:         frozenset({CoCState.ATTACK, CoCState.FAILED}),
    CoCState.ATTACK:       frozenset({CoCState.FIND_MATCH, CoCState.FAILED}),
    CoCState.FIND_MATCH:   frozenset({CoCState.WAIT_VILLAGE, CoCState.FAILED}),
    CoCState.WAIT_VILLAGE: frozenset({CoCState.DROP_ARMY, CoCState.FAILED}),
    CoCState.DROP_ARMY:    frozenset({CoCState.WAIT_BATTLE, CoCState.FAILED}),
    CoCState.WAIT_BATTLE:  frozenset({CoCState.END_BATTLE, CoCState.FAILED}),
    CoCState.END_BATTLE:   frozenset({CoCState.CONFIRM, CoCState.FAILED}),
    CoCState.CONFIRM:      frozenset({CoCState.RETURN_HOME, CoCState.FAILED}),
    CoCState.RETURN_HOME:  frozenset({CoCState.COMPLETE, CoCState.FAILED}),
    CoCState.COMPLETE:     frozenset(),  # terminal
    CoCState.FAILED:       frozenset(),  # terminal
}

ALLOWED_TRANSITIONS: Mapping[CoCState, frozenset[CoCState]] = MappingProxyType(
    _ALLOWED
)


def is_allowed(from_state: CoCState, to_state: CoCState) -> bool:
    """True iff `from_state → to_state` is a valid transition.

    Returns False (not raises) when either argument is not a
    `CoCState`. The caller is expected to type-check upstream;
    this is a defensive convenience.
    """
    if not isinstance(from_state, CoCState) or not isinstance(to_state, CoCState):
        return False
    return to_state in ALLOWED_TRANSITIONS[from_state]


def is_terminal(state: CoCState) -> bool:
    """True iff the state has no outgoing transitions."""
    if not isinstance(state, CoCState):
        raise TypeError(
            f"state must be CoCState, got {type(state).__name__}"
        )
    return len(ALLOWED_TRANSITIONS[state]) == 0


def allowed_next(from_state: CoCState) -> frozenset[CoCState]:
    """Set of states reachable from `from_state` in one transition."""
    if not isinstance(from_state, CoCState):
        raise TypeError(
            f"from_state must be CoCState, got {type(from_state).__name__}"
        )
    return ALLOWED_TRANSITIONS[from_state]


__all__ = [
    "CoCState",
    "ALLOWED_TRANSITIONS",
    "is_allowed",
    "is_terminal",
    "allowed_next",
]
