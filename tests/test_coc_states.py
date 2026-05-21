"""CoC FSM tests — enum, allowed-transitions table, reachability."""
from __future__ import annotations

import pytest

from coc.states import (
    ALLOWED_TRANSITIONS,
    CoCState,
    allowed_next,
    is_allowed,
    is_terminal,
)


# ---- enum --------------------------------------------------------------------


def test_state_has_exactly_eleven_members() -> None:
    assert {s.name for s in CoCState} == {
        "HOME", "ATTACK", "FIND_MATCH", "WAIT_VILLAGE", "DROP_ARMY",
        "WAIT_BATTLE", "END_BATTLE", "CONFIRM", "RETURN_HOME",
        "COMPLETE", "FAILED",
    }


def test_state_values_match_names() -> None:
    for s in CoCState:
        assert s.value == s.name


def test_values_are_unique() -> None:
    values = [s.value for s in CoCState]
    assert len(values) == len(set(values))


# ---- allowed-transitions table ----------------------------------------------


def test_table_covers_every_state() -> None:
    assert set(ALLOWED_TRANSITIONS.keys()) == set(CoCState)


def test_table_is_immutable() -> None:
    """`MappingProxyType` view; mutations rejected at runtime."""
    with pytest.raises(TypeError):
        ALLOWED_TRANSITIONS[CoCState.HOME] = frozenset({CoCState.FAILED})  # type: ignore[index]


def test_linear_happy_path_intact() -> None:
    assert CoCState.ATTACK in ALLOWED_TRANSITIONS[CoCState.HOME]
    assert CoCState.FIND_MATCH in ALLOWED_TRANSITIONS[CoCState.ATTACK]
    assert CoCState.WAIT_VILLAGE in ALLOWED_TRANSITIONS[CoCState.FIND_MATCH]
    assert CoCState.DROP_ARMY in ALLOWED_TRANSITIONS[CoCState.WAIT_VILLAGE]
    assert CoCState.WAIT_BATTLE in ALLOWED_TRANSITIONS[CoCState.DROP_ARMY]
    assert CoCState.END_BATTLE in ALLOWED_TRANSITIONS[CoCState.WAIT_BATTLE]
    assert CoCState.CONFIRM in ALLOWED_TRANSITIONS[CoCState.END_BATTLE]
    assert CoCState.RETURN_HOME in ALLOWED_TRANSITIONS[CoCState.CONFIRM]
    assert CoCState.COMPLETE in ALLOWED_TRANSITIONS[CoCState.RETURN_HOME]


def test_every_non_terminal_can_fail() -> None:
    for state in CoCState:
        if state in (CoCState.COMPLETE, CoCState.FAILED):
            continue
        assert CoCState.FAILED in ALLOWED_TRANSITIONS[state], (
            f"{state.value} cannot reach FAILED"
        )


def test_complete_and_failed_are_terminal() -> None:
    assert ALLOWED_TRANSITIONS[CoCState.COMPLETE] == frozenset()
    assert ALLOWED_TRANSITIONS[CoCState.FAILED] == frozenset()
    assert is_terminal(CoCState.COMPLETE)
    assert is_terminal(CoCState.FAILED)


def test_no_backwards_edges() -> None:
    """The trophy-drop loop is strictly forward; no state allows
    transition back to a prior step."""
    order = [
        CoCState.HOME, CoCState.ATTACK, CoCState.FIND_MATCH,
        CoCState.WAIT_VILLAGE, CoCState.DROP_ARMY,
        CoCState.WAIT_BATTLE, CoCState.END_BATTLE,
        CoCState.CONFIRM, CoCState.RETURN_HOME,
    ]
    rank = {s: i for i, s in enumerate(order)}
    for s in order:
        for nxt in ALLOWED_TRANSITIONS[s]:
            if nxt in (CoCState.COMPLETE, CoCState.FAILED):
                continue
            assert rank[nxt] > rank[s], (
                f"{s.value} can transition backward to {nxt.value}"
            )


# ---- is_allowed --------------------------------------------------------------


@pytest.mark.parametrize("frm,to", [
    (CoCState.HOME, CoCState.ATTACK),
    (CoCState.ATTACK, CoCState.FIND_MATCH),
    (CoCState.FIND_MATCH, CoCState.WAIT_VILLAGE),
    (CoCState.WAIT_VILLAGE, CoCState.DROP_ARMY),
    (CoCState.DROP_ARMY, CoCState.WAIT_BATTLE),
    (CoCState.WAIT_BATTLE, CoCState.END_BATTLE),
    (CoCState.END_BATTLE, CoCState.CONFIRM),
    (CoCState.CONFIRM, CoCState.RETURN_HOME),
    (CoCState.RETURN_HOME, CoCState.COMPLETE),
    (CoCState.HOME, CoCState.FAILED),
    (CoCState.FIND_MATCH, CoCState.FAILED),
    (CoCState.WAIT_BATTLE, CoCState.FAILED),
])
def test_is_allowed_for_valid_transitions(frm, to) -> None:
    assert is_allowed(frm, to) is True


@pytest.mark.parametrize("frm,to", [
    (CoCState.HOME, CoCState.FIND_MATCH),       # skip ATTACK
    (CoCState.ATTACK, CoCState.HOME),           # backward
    (CoCState.DROP_ARMY, CoCState.HOME),        # backward
    (CoCState.COMPLETE, CoCState.HOME),         # terminal
    (CoCState.FAILED, CoCState.HOME),           # terminal
    (CoCState.HOME, CoCState.HOME),             # self-loop disallowed
])
def test_is_allowed_for_invalid_transitions(frm, to) -> None:
    assert is_allowed(frm, to) is False


def test_is_allowed_rejects_non_state_inputs() -> None:
    assert is_allowed("HOME", CoCState.ATTACK) is False  # type: ignore[arg-type]
    assert is_allowed(CoCState.HOME, "ATTACK") is False  # type: ignore[arg-type]
    assert is_allowed(None, None) is False  # type: ignore[arg-type]


# ---- allowed_next / is_terminal ---------------------------------------------


def test_allowed_next_returns_frozenset() -> None:
    out = allowed_next(CoCState.WAIT_VILLAGE)
    assert isinstance(out, frozenset)
    assert out == {CoCState.DROP_ARMY, CoCState.FAILED}


def test_allowed_next_rejects_non_state() -> None:
    with pytest.raises(TypeError, match="must be CoCState"):
        allowed_next("HOME")  # type: ignore[arg-type]


def test_is_terminal_rejects_non_state() -> None:
    with pytest.raises(TypeError, match="must be CoCState"):
        is_terminal("HOME")  # type: ignore[arg-type]


def test_non_terminal_states() -> None:
    for s in CoCState:
        if s in (CoCState.COMPLETE, CoCState.FAILED):
            continue
        assert not is_terminal(s)


# ---- reachability ------------------------------------------------------------


def test_complete_reachable_from_home() -> None:
    """BFS from HOME along forward edges reaches COMPLETE."""
    seen: set[CoCState] = set()
    pending = [CoCState.HOME]
    while pending:
        s = pending.pop()
        if s in seen:
            continue
        seen.add(s)
        for nxt in ALLOWED_TRANSITIONS[s]:
            if nxt is CoCState.FAILED:
                continue  # exclude failure path for happy reachability
            pending.append(nxt)
    assert CoCState.COMPLETE in seen


def test_all_states_visited_on_happy_path() -> None:
    """The 9 working states + COMPLETE are all visited by the
    HOME → … → COMPLETE walk."""
    expected = {
        CoCState.HOME, CoCState.ATTACK, CoCState.FIND_MATCH,
        CoCState.WAIT_VILLAGE, CoCState.DROP_ARMY,
        CoCState.WAIT_BATTLE, CoCState.END_BATTLE,
        CoCState.CONFIRM, CoCState.RETURN_HOME,
        CoCState.COMPLETE,
    }
    seen: set[CoCState] = set()
    pending = [CoCState.HOME]
    while pending:
        s = pending.pop()
        if s in seen:
            continue
        seen.add(s)
        for nxt in ALLOWED_TRANSITIONS[s]:
            if nxt is CoCState.FAILED:
                continue
            pending.append(nxt)
    assert seen == expected
