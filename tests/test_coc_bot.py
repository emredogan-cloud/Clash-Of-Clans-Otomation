"""CoCTrophyBot tests — composes framework mocks.

Strategy: mock Sensor / Matcher / Actuator / ADB at the FRAMEWORK
boundary. The bot constructs `Orchestrator(sensor, matcher,
actuator, template)` and `Watchdog(orch)` for its find-and-tap
steps — those are *real* framework instances; only the lowest
layers are mocked. This validates the bot's composition end to
end without a device.
"""
from __future__ import annotations

import datetime as _dt
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import pytest

from automation.action_result import ActionResult
from automation.errors import ADBError
from automation.frame import Frame
from automation.match_result import MatchResult
from automation.template import Template
from coc.bot import (
    DEPLOY_TAP_SEQUENCE_REF,
    CoCBotResult,
    CoCTrophyBot,
)
from coc.states import CoCState
from coc.templates import EXPECTED_NAMES, TEMPLATE_SPECS, TemplatePack


_UTC = _dt.timezone.utc
_NOW = _dt.datetime(2026, 5, 21, 18, 0, 0, tzinfo=_UTC)

# CoC bot defaults / shorter test budgets so the suite stays fast.
_FAST_BOT_KWARGS = dict(
    battle_wait_s=0.05,
    launch_wait_s=0.01,
    per_step_timeout_s=2.0,
    poll_interval_s=0.05,
)


# ============================================================================
# Helpers — synthetic templates / frames
# ============================================================================


def _make_template(name: str, *, size: int = 32) -> Template:
    """Construct a real Template with a high-entropy 4-quadrant patch."""
    img = np.full((size, size), 30, dtype=np.uint8)
    h, w = size // 2, size // 2
    img[:h, :w] = 50
    img[:h, w:] = 120
    img[h:, :w] = 180
    img[h:, w:] = 230
    return Template(
        name=name, image_gray=img,
        width=size, height=size,
        threshold=0.9, roi=None,
    )


def _make_pack() -> TemplatePack:
    """A full TemplatePack with synthetic templates for all 6 names."""
    return TemplatePack(
        templates={spec.name: _make_template(spec.name)
                   for spec in TEMPLATE_SPECS},
    )


def _partial_pack(missing: set[str]) -> TemplatePack:
    """A pack missing the named templates (for fail-safe tests)."""
    return TemplatePack(
        templates={spec.name: _make_template(spec.name)
                   for spec in TEMPLATE_SPECS
                   if spec.name not in missing},
    )


def _make_frame() -> Frame:
    img = np.zeros((1920, 1080, 3), dtype=np.uint8)
    return Frame(
        image_bgr=img, width=1080, height=1920,
        source_mode="raw", capture_latency_ms=10.0,
        capture_ts=_NOW, native_width=1080, native_height=2408,
    )


def _hit(*, template_name: str = "demo") -> MatchResult:
    return MatchResult(
        found=True, confidence=0.99,
        template_name=template_name, search_mode="full_gray",
        capture_latency_ms=10.0, match_latency_ms=1.0,
        x=500, y=900, width=32, height=32,
    )


def _miss(*, template_name: str = "demo") -> MatchResult:
    return MatchResult(
        found=False, confidence=0.10,
        template_name=template_name, search_mode="full_gray",
        capture_latency_ms=10.0, match_latency_ms=1.0,
    )


def _action_ok() -> ActionResult:
    return ActionResult(
        success=True, action_type="tap", latency_ms=5.0,
        device_x=540, device_y=1200, ts=_NOW,
    )


def _action_fail() -> ActionResult:
    return ActionResult(
        success=False, action_type="tap", latency_ms=5.0,
        device_x=540, device_y=1200, ts=_NOW,
    )


# ============================================================================
# Mock framework layers
# ============================================================================


@dataclass
class _MockSensor:
    calls: int = 0

    def capture(self) -> Frame:
        self.calls += 1
        return _make_frame()


@dataclass
class _ScriptedMatcher:
    """Returns scripted MatchResults per (template_name, sequence)."""
    scripts: dict[str, deque] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    default_miss: bool = False

    def queue(self, template_name: str, results: list[MatchResult]) -> None:
        self.scripts[template_name] = deque(results)

    def match(self, frame: Frame, template: Template) -> MatchResult:
        self.calls.append(template.name)
        q = self.scripts.get(template.name)
        if q:
            return q.popleft()
        if self.default_miss:
            return _miss(template_name=template.name)
        return _hit(template_name=template.name)


@dataclass
class _MockActuator:
    tap_calls: list[tuple[int, int, int, int]] = field(default_factory=list)
    fail_on_call: int | None = None  # 1-indexed; None = never fail

    def tap(self, x, y, nw, nh, *, jitter: bool = False) -> ActionResult:
        self.tap_calls.append((int(x), int(y), int(nw), int(nh)))
        if self.fail_on_call is not None and len(self.tap_calls) == self.fail_on_call:
            return _action_fail()
        return _action_ok()


@dataclass
class _MockADB:
    shell_calls: list[list[str]] = field(default_factory=list)
    raise_on_shell: ADBError | None = None

    def shell(self, args, *, timeout=None) -> str:
        self.shell_calls.append(list(args))
        if self.raise_on_shell is not None:
            raise self.raise_on_shell
        return ""

    def get_state(self) -> str:  # pragma: no cover — not used by bot
        return "device"


def _happy_path_matcher() -> _ScriptedMatcher:
    """Queue HIT-then-MISS pairs for the 5 find-and-tap templates,
    plus a single HIT for battle_ui_indicator (wait-for path)."""
    m = _ScriptedMatcher()
    # Each find-and-tap step makes 2 match calls: search HIT + validate MISS.
    # That's the happy path through Orchestrator.tick.
    for name in (
        "home_attack_button", "find_match_button",
        "surrender_button", "surrender_confirm", "return_home_button",
    ):
        m.queue(name, [_hit(template_name=name), _miss(template_name=name)])
    m.queue("battle_ui_indicator", [_hit(template_name="battle_ui_indicator")])
    return m


def _make_bot(
    sensor=None, matcher=None, actuator=None, adb=None,
    templates=None, **kwargs,
) -> CoCTrophyBot:
    return CoCTrophyBot(
        sensor=sensor or _MockSensor(),
        matcher=matcher or _happy_path_matcher(),
        actuator=actuator or _MockActuator(),
        templates=templates or _make_pack(),
        adb=adb or _MockADB(),
        **{**_FAST_BOT_KWARGS, **kwargs},
    )


# ============================================================================
# Happy-path test
# ============================================================================


def test_happy_path_full_loop_completes() -> None:
    sensor = _MockSensor()
    matcher = _happy_path_matcher()
    actuator = _MockActuator()
    adb = _MockADB()
    bot = _make_bot(sensor=sensor, matcher=matcher, actuator=actuator, adb=adb)
    r = bot.run_once()
    assert isinstance(r, CoCBotResult)
    assert r.success is True
    assert r.final_state is CoCState.COMPLETE
    assert r.failure_reason is None
    assert r.failure_step is None
    # The full state walk happened.
    assert r.states_visited == [
        CoCState.HOME, CoCState.ATTACK, CoCState.FIND_MATCH,
        CoCState.WAIT_VILLAGE, CoCState.DROP_ARMY, CoCState.WAIT_BATTLE,
        CoCState.END_BATTLE, CoCState.CONFIRM, CoCState.RETURN_HOME,
        CoCState.COMPLETE,
    ]


def test_happy_path_launches_clash_via_monkey() -> None:
    adb = _MockADB()
    bot = _make_bot(adb=adb)
    bot.run_once()
    assert any("monkey" in args and "com.supercell.clashofclans" in args
               for args in adb.shell_calls), (
        f"monkey launch not found in shell calls: {adb.shell_calls}"
    )


def test_happy_path_deploy_taps_match_the_pattern() -> None:
    actuator = _MockActuator()
    bot = _make_bot(actuator=actuator)
    bot.run_once()
    # Find the deployment phase among all tap calls. The bot
    # taps a button per find-and-tap step (5 buttons) + the
    # 4 deployment taps. So 9 tap calls total on the happy path.
    assert len(actuator.tap_calls) == 5 + len(DEPLOY_TAP_SEQUENCE_REF)
    # The deployment taps are the 3rd–6th calls (counting from 1):
    #   tap 1: home_attack_button
    #   tap 2: find_match_button
    #   tap 3-6: deployment taps
    #   tap 7-9: surrender, confirm, return_home
    # Each tap_calls entry is (x_ref, y_ref, native_w, native_h).
    # The deployment taps' (x_ref, y_ref) must match the pattern.
    deploy_taps = actuator.tap_calls[2:2 + len(DEPLOY_TAP_SEQUENCE_REF)]
    assert [(x, y) for x, y, _, _ in deploy_taps] == list(DEPLOY_TAP_SEQUENCE_REF)


def test_happy_path_state_property_reflects_final_state() -> None:
    bot = _make_bot()
    bot.run_once()
    assert bot.state is CoCState.COMPLETE


# ============================================================================
# Missing template → fail safe
# ============================================================================


def test_missing_template_at_first_step_fails_safe() -> None:
    """If home_attack_button is missing, the bot must NOT tap
    anywhere; it returns FAILED with a clear reason."""
    pack = _partial_pack(missing={"home_attack_button"})
    actuator = _MockActuator()
    bot = _make_bot(templates=pack, actuator=actuator)
    r = bot.run_once()
    assert r.success is False
    assert r.final_state is CoCState.FAILED
    assert r.failure_step is CoCState.HOME
    assert "home_attack_button" in (r.failure_reason or "")
    # No taps issued (other than monkey launch).
    assert actuator.tap_calls == []


def test_missing_template_at_middle_step_fails_safe() -> None:
    """Template pack missing battle_ui_indicator → the bot reaches
    FIND_MATCH then dies cleanly, no deployment taps."""
    pack = _partial_pack(missing={"battle_ui_indicator"})
    actuator = _MockActuator()
    bot = _make_bot(templates=pack, actuator=actuator)
    r = bot.run_once()
    assert r.success is False
    assert r.final_state is CoCState.FAILED
    assert r.failure_step is CoCState.FIND_MATCH
    # home_attack + find_match buttons were tapped (2 taps).
    # No deployment taps (4 of those) were issued.
    assert len(actuator.tap_calls) == 2


# ============================================================================
# Wait paths
# ============================================================================


def test_wait_for_template_returns_when_template_appears() -> None:
    """battle_ui_indicator appears on the 2nd poll."""
    matcher = _happy_path_matcher()
    # Override: first the wait sees MISS, then HIT.
    matcher.queue(
        "battle_ui_indicator",
        [_miss(template_name="battle_ui_indicator"),
         _hit(template_name="battle_ui_indicator")],
    )
    bot = _make_bot(matcher=matcher)
    r = bot.run_once()
    assert r.success is True


def test_wait_for_template_times_out_when_never_appears() -> None:
    matcher = _happy_path_matcher()
    # Always miss for battle_ui_indicator → step times out.
    matcher.queue(
        "battle_ui_indicator",
        [_miss(template_name="battle_ui_indicator")] * 100,
    )
    bot = _make_bot(matcher=matcher, per_step_timeout_s=0.3)
    r = bot.run_once()
    assert r.success is False
    assert r.failure_step is CoCState.FIND_MATCH
    assert "battle_ui_indicator" in (r.failure_reason or "")


def test_battle_wait_uses_monotonic_not_busy_loop() -> None:
    """The 170-second wait must use time.sleep, not a busy loop.
    We pass battle_wait_s = 0.3 and assert the call returns ~0.3s,
    not instantly (which would prove busy-spin) and not 170 s."""
    bot = _make_bot(battle_wait_s=0.3)
    t0 = time.monotonic()
    bot.run_once()
    elapsed = time.monotonic() - t0
    # Lower bound: at least the configured battle wait.
    assert elapsed >= 0.3
    # Upper bound: bounded so we know we didn't busy-spin or accidentally
    # use the default 170 s. Generous to allow for slow CI.
    assert elapsed < 10.0


# ============================================================================
# Actuator failures → fail safe
# ============================================================================


def test_actuator_tap_failure_propagates_as_FAILED() -> None:
    """If the actuator returns success=False (e.g., ADB issued
    a non-zero exit), the bot must NOT continue tapping; it
    returns FAILED."""
    # Fail on the 1st tap (the home_attack_button tap).
    actuator = _MockActuator(fail_on_call=1)
    matcher = _happy_path_matcher()
    bot = _make_bot(matcher=matcher, actuator=actuator)
    r = bot.run_once()
    assert r.success is False
    assert r.final_state is CoCState.FAILED


def test_deployment_tap_failure_propagates_as_FAILED() -> None:
    """If a deployment tap fails (e.g., 3rd of 4), the bot
    aborts the deployment and FAILED."""
    matcher = _happy_path_matcher()
    # The 1st two taps are find-and-tap buttons (home_attack, find_match).
    # Deployment taps are #3-6. Fail the 5th tap (deployment #3).
    actuator = _MockActuator(fail_on_call=5)
    bot = _make_bot(matcher=matcher, actuator=actuator)
    r = bot.run_once()
    assert r.success is False
    assert r.failure_step is CoCState.DROP_ARMY


# ============================================================================
# Launch failure
# ============================================================================


def test_adb_launch_failure_fails_safe() -> None:
    adb = _MockADB(raise_on_shell=ADBError("device offline"))
    actuator = _MockActuator()
    bot = _make_bot(adb=adb, actuator=actuator)
    r = bot.run_once()
    assert r.success is False
    assert r.failure_step is CoCState.HOME
    assert "launch" in (r.failure_reason or "")
    # No bot-issued taps because launch failed before any step ran.
    assert actuator.tap_calls == []


# ============================================================================
# Return-home path coverage
# ============================================================================


def test_return_home_step_is_the_last_find_and_tap() -> None:
    """The bot's last find-and-tap is return_home_button."""
    matcher = _happy_path_matcher()
    bot = _make_bot(matcher=matcher)
    bot.run_once()
    # The matcher should have been called for each template in order.
    seen = [c for c in matcher.calls]
    # The return_home_button must be the LAST template name seen.
    assert seen.count("return_home_button") >= 1
    assert seen[-1] == "return_home_button"


def test_state_progression_with_no_skip() -> None:
    """Every state in the linear path is visited in order."""
    bot = _make_bot()
    r = bot.run_once()
    expected_order = [
        CoCState.HOME, CoCState.ATTACK, CoCState.FIND_MATCH,
        CoCState.WAIT_VILLAGE, CoCState.DROP_ARMY, CoCState.WAIT_BATTLE,
        CoCState.END_BATTLE, CoCState.CONFIRM, CoCState.RETURN_HOME,
        CoCState.COMPLETE,
    ]
    assert r.states_visited == expected_order


# ============================================================================
# Result container hygiene
# ============================================================================


def test_result_to_debug_dict_is_json_safe() -> None:
    import json
    bot = _make_bot()
    r = bot.run_once()
    blob = r.to_debug_dict()
    decoded = json.loads(json.dumps(blob))
    assert decoded["success"] is True
    assert decoded["final_state"] == "COMPLETE"


def test_result_summary_includes_state_walk() -> None:
    bot = _make_bot()
    r = bot.run_once()
    summary = r.summary()
    assert "OK" in summary
    assert "HOME" in summary
    assert "COMPLETE" in summary


def test_result_is_immutable() -> None:
    bot = _make_bot()
    r = bot.run_once()
    with pytest.raises(Exception):
        r.success = False  # type: ignore[misc]


# ============================================================================
# Template pack expected names
# ============================================================================


def test_expected_names_match_specs() -> None:
    assert EXPECTED_NAMES == {spec.name for spec in TEMPLATE_SPECS}


def test_template_pack_has_all_6_templates() -> None:
    pack = _make_pack()
    assert len(pack) == 6
    assert set(pack.names()) == EXPECTED_NAMES
