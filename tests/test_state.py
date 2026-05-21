"""FSM state enum + allowed-transitions table tests.

These tests are purely declarative — no orchestrator, no mocks.
"""
from __future__ import annotations

import pytest

from automation.state import (
    ALLOWED_TRANSITIONS,
    State,
    allowed_next,
    is_allowed,
)


# ---- enum membership ---------------------------------------------------------


def test_state_has_exactly_five_members() -> None:
    assert {s.name for s in State} == {
        "IDLE", "SEARCHING", "ACTING", "VALIDATING", "FAILED",
    }


def test_state_values_are_canonical_strings() -> None:
    assert State.IDLE.value == "IDLE"
    assert State.SEARCHING.value == "SEARCHING"
    assert State.ACTING.value == "ACTING"
    assert State.VALIDATING.value == "VALIDATING"
    assert State.FAILED.value == "FAILED"


def test_state_values_are_unique() -> None:
    """enum.unique guard."""
    values = [s.value for s in State]
    assert len(values) == len(set(values))


# ---- the allowed-transitions table ------------------------------------------


def test_table_covers_every_state() -> None:
    assert set(ALLOWED_TRANSITIONS.keys()) == set(State)


def test_idle_only_to_searching() -> None:
    assert ALLOWED_TRANSITIONS[State.IDLE] == frozenset({State.SEARCHING})


def test_searching_to_acting_or_failed() -> None:
    assert ALLOWED_TRANSITIONS[State.SEARCHING] == frozenset(
        {State.ACTING, State.FAILED}
    )


def test_acting_to_validating_or_failed() -> None:
    assert ALLOWED_TRANSITIONS[State.ACTING] == frozenset(
        {State.VALIDATING, State.FAILED}
    )


def test_validating_to_idle_or_failed() -> None:
    assert ALLOWED_TRANSITIONS[State.VALIDATING] == frozenset(
        {State.IDLE, State.FAILED}
    )


def test_failed_only_to_idle() -> None:
    """FAILED is non-terminal but only `reset()` (FAILED→IDLE) exits it."""
    assert ALLOWED_TRANSITIONS[State.FAILED] == frozenset({State.IDLE})


def test_table_is_immutable() -> None:
    """MappingProxyType makes the table read-only at runtime."""
    with pytest.raises(TypeError):
        ALLOWED_TRANSITIONS[State.IDLE] = frozenset({State.FAILED})  # type: ignore[index]


# ---- is_allowed --------------------------------------------------------------


@pytest.mark.parametrize("frm,to", [
    (State.IDLE, State.SEARCHING),
    (State.SEARCHING, State.ACTING),
    (State.SEARCHING, State.FAILED),
    (State.ACTING, State.VALIDATING),
    (State.ACTING, State.FAILED),
    (State.VALIDATING, State.IDLE),
    (State.VALIDATING, State.FAILED),
    (State.FAILED, State.IDLE),
])
def test_is_allowed_for_valid_transitions(frm: State, to: State) -> None:
    assert is_allowed(frm, to) is True


@pytest.mark.parametrize("frm,to", [
    (State.IDLE, State.ACTING),
    (State.IDLE, State.FAILED),
    (State.IDLE, State.IDLE),  # no self-loop
    (State.SEARCHING, State.IDLE),
    (State.SEARCHING, State.VALIDATING),
    (State.ACTING, State.IDLE),
    (State.ACTING, State.SEARCHING),
    (State.VALIDATING, State.ACTING),
    (State.VALIDATING, State.SEARCHING),
    (State.FAILED, State.SEARCHING),
    (State.FAILED, State.ACTING),
    (State.FAILED, State.VALIDATING),
    (State.FAILED, State.FAILED),
])
def test_is_allowed_for_invalid_transitions(frm: State, to: State) -> None:
    assert is_allowed(frm, to) is False


def test_is_allowed_rejects_non_state_inputs() -> None:
    assert is_allowed("IDLE", State.SEARCHING) is False  # type: ignore[arg-type]
    assert is_allowed(State.IDLE, "SEARCHING") is False  # type: ignore[arg-type]
    assert is_allowed(None, None) is False  # type: ignore[arg-type]


# ---- allowed_next ------------------------------------------------------------


def test_allowed_next_returns_frozen_set() -> None:
    out = allowed_next(State.SEARCHING)
    assert isinstance(out, frozenset)
    assert out == {State.ACTING, State.FAILED}


def test_allowed_next_rejects_non_state() -> None:
    with pytest.raises(TypeError, match="must be State"):
        allowed_next("IDLE")  # type: ignore[arg-type]


# ---- reachability ------------------------------------------------------------


def test_every_state_is_reachable_from_idle() -> None:
    """BFS from IDLE through the allowed table touches every state."""
    seen: set[State] = set()
    pending = [State.IDLE]
    while pending:
        s = pending.pop()
        if s in seen:
            continue
        seen.add(s)
        for nxt in ALLOWED_TRANSITIONS[s]:
            pending.append(nxt)
    assert seen == set(State)


def test_no_state_is_a_dead_end() -> None:
    """Every state has at least one outgoing transition."""
    for s in State:
        assert len(ALLOWED_TRANSITIONS[s]) >= 1, f"{s.value} is a dead-end"
