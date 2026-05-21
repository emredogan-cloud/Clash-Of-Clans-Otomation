"""CoCTrophyBot — one trophy-drop loop composed from the v1.0 framework.

The bot walks the 9-step `CoCState` FSM in `run_once()`:

    launch CoC (com.supercell.clashofclans)
    → HOME       — find + tap Attack button
    → ATTACK     — find + tap Find Match button
    → FIND_MATCH — wait for the battle UI to appear (no tap)
    → WAIT_VILLAGE — brief settle
    → DROP_ARMY  — deploy troops (fixed-coord pattern)
    → WAIT_BATTLE — wait 170 s on `time.monotonic()`
    → END_BATTLE — find + tap surrender
    → CONFIRM    — find + tap surrender-confirm
    → RETURN_HOME — find + tap return-home
    → COMPLETE

Framework composition:

- **Sensor** — captures used at every step.
- **Matcher** — every template detection.
- **Actuator** — every tap (find-and-tap steps + deployment).
- **Orchestrator** — wraps each "find + tap + verify gone" step
  (5 of them). One fresh Orchestrator per step, carrying that
  step's `Template`. The orchestrator's SEARCH→ACT→VALIDATE
  contract subsumes "tap a button and confirm the screen
  transitioned past it".
- **Watchdog** — the L1 supervisor wraps each Orchestrator,
  catching exceptions, post-hoc-flagging budget overruns, and
  optionally invoking recovery. The bot's outer poll loop
  retries the watchdog until success or the per-step deadline.

What the bot does NOT do (per the Phase prompt's prohibitions):

- No launcher icon taps. CoC is launched via explicit package
  intent through `am start -n com.supercell.clashofclans/...`.
- No OCR, no ML, no segmentation. Literal template matching only.
- No game intelligence. The deployment pattern is fixed-coord;
  troops drop wherever they land.
- No retries after missing-template / step-timeout — that's a
  FAILED outcome, surfaced via `CoCBotResult.success = False`.
  No "blind tap spam", no launcher wandering.
- No infinite loop. One `run_once()` is one trophy-drop attempt;
  the caller decides cadence.
"""
from __future__ import annotations

import datetime as _dt
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from automation.errors import ADBError, AutomationError
from automation.orchestrator import Orchestrator
from automation.state import State as _FrameworkState
from automation.watchdog import Watchdog

from .states import ALLOWED_TRANSITIONS, CoCState, is_allowed
from .templates import TemplatePack

if TYPE_CHECKING:  # pragma: no cover — typing only
    from automation.actuator import Actuator
    from automation.adb import ADB
    from automation.matcher import Matcher
    from automation.sensor import Sensor

_LOG = logging.getLogger(__name__)


# CoC package + launcher activity. monkey is the safest portable
# launcher: `monkey -p <pkg> -c android.intent.category.LAUNCHER 1`
# launches the package's default LAUNCHER activity and returns
# without binding to a particular activity name (which may vary
# by build).
COC_PACKAGE: str = "com.supercell.clashofclans"
LAUNCH_COMMAND: tuple[str, ...] = (
    "monkey", "-p", COC_PACKAGE,
    "-c", "android.intent.category.LAUNCHER", "1",
)

# Defaults — all tunable via the bot's constructor kwargs.
DEFAULT_BATTLE_WAIT_S: float = 170.0
DEFAULT_LAUNCH_WAIT_S: float = 8.0
DEFAULT_PER_STEP_TIMEOUT_S: float = 60.0
DEFAULT_POLL_INTERVAL_S: float = 1.5

# Deployment pattern (v1: single fixed pattern). Coordinates are
# in the v1.0 reference resolution (1080×1920). The actuator
# denormalizes to the device's native pixels.
#
# The pattern is a 4-tap sequence: one tap on the bottom-left
# troop slot, then three taps in a lower-left band of the screen.
# This is *deliberately* simple — the v1.0 bot is for trophy
# *dropping*, not winning. The taps may or may not deploy actual
# troops depending on the operator's army composition; whatever
# is deployed will start the battle clock. The 170 s wait then
# carries us to the surrender step.
DEPLOY_TAP_SEQUENCE_REF: tuple[tuple[int, int], ...] = (
    (140, 1810),   # troop slot #1 in the bottom troop bar
    (300, 1500),   # deploy near bottom-left edge
    (200, 1450),   # deploy a bit higher / left
    (260, 1540),   # one more deploy point
)


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CoCBotResult:
    """Outcome of one `CoCTrophyBot.run_once()` call.

    The bot never raises out of `run_once`; every fault is
    captured in this result. `success=True` iff the FSM reached
    `COMPLETE`; `success=False` iff it reached `FAILED` for any
    reason (`failure_reason` is filled in).
    """

    success: bool
    final_state: CoCState
    states_visited: list[CoCState]
    failure_reason: str | None
    failure_step: CoCState | None
    elapsed_s: float
    ts: _dt.datetime

    def to_debug_dict(self) -> Mapping[str, Any]:
        return {
            "success": self.success,
            "final_state": self.final_state.value,
            "states_visited": [s.value for s in self.states_visited],
            "failure_reason": self.failure_reason,
            "failure_step": (
                self.failure_step.value if self.failure_step is not None
                else None
            ),
            "elapsed_s": float(self.elapsed_s),
            "ts": self.ts.isoformat(),
        }

    def summary(self) -> str:
        flag = "OK" if self.success else "FAIL"
        visited = " → ".join(s.value for s in self.states_visited)
        tail = ""
        if self.failure_reason:
            tail = f"  (reason: {self.failure_reason})"
        return f"CoCBotResult({flag} {visited} elapsed={self.elapsed_s:.1f}s){tail}"


# ---------------------------------------------------------------------------
# Internal step error
# ---------------------------------------------------------------------------


class CoCStepError(AutomationError):
    """A step inside `run_once` failed. Caught and surfaced as
    `CoCBotResult(success=False)`. Not raised to the caller."""


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------


class CoCTrophyBot:
    """Single-loop CoC trophy-drop bot.

    Constructor params:

    - `sensor`              : framework `Sensor` instance.
    - `matcher`             : framework `Matcher` instance.
    - `actuator`            : framework `Actuator` instance.
    - `templates`           : `TemplatePack` loaded via
                              `coc.templates.load_template_pack`.
    - `adb`                 : framework `ADB` instance for the
                              `monkey` launch shell call.
    - `battle_wait_s`       : monotonic wait after DROP_ARMY.
                              Default 170 s (Phase prompt).
    - `launch_wait_s`       : sleep after the `monkey` launch
                              to let CoC's splash → home
                              transition settle. Default 8 s.
    - `per_step_timeout_s`  : per-step deadline for find-and-tap
                              and wait-for-template steps.
                              Default 60 s.
    - `poll_interval_s`     : sleep between polling attempts
                              inside a step. Default 1.5 s.

    Threading: single-threaded. One bot instance owns one
    in-progress loop. `run_once()` is the only entry point.
    """

    def __init__(
        self,
        sensor: "Sensor",
        matcher: "Matcher",
        actuator: "Actuator",
        templates: TemplatePack,
        adb: "ADB",
        *,
        battle_wait_s: float = DEFAULT_BATTLE_WAIT_S,
        launch_wait_s: float = DEFAULT_LAUNCH_WAIT_S,
        per_step_timeout_s: float = DEFAULT_PER_STEP_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self.sensor: "Sensor" = sensor
        self.matcher: "Matcher" = matcher
        self.actuator: "Actuator" = actuator
        self.templates: TemplatePack = templates
        self.adb: "ADB" = adb
        self.battle_wait_s: float = float(battle_wait_s)
        self.launch_wait_s: float = float(launch_wait_s)
        self.per_step_timeout_s: float = float(per_step_timeout_s)
        self.poll_interval_s: float = float(poll_interval_s)
        self._state: CoCState = CoCState.HOME
        self._visited: list[CoCState] = [CoCState.HOME]

    # ---- read-only state -------------------------------------------------

    @property
    def state(self) -> CoCState:
        return self._state

    @property
    def states_visited(self) -> list[CoCState]:
        return list(self._visited)

    # ---- public entry point ----------------------------------------------

    def run_once(self) -> CoCBotResult:
        """Execute one full trophy-drop loop.

        Always returns a `CoCBotResult`. Never raises. On any
        step failure the FSM transitions to FAILED, the
        `failure_reason` and `failure_step` are populated, and
        success=False is returned.
        """
        ts_start = _dt.datetime.now(tz=_dt.timezone.utc)
        t0 = time.monotonic()
        failure_reason: str | None = None
        failure_step: CoCState | None = None

        try:
            # 0. Launch CoC by explicit package intent.
            self._launch_clash()

            # 1. HOME → ATTACK
            self._step_find_and_tap("home_attack_button", CoCState.ATTACK)

            # 2. ATTACK → FIND_MATCH
            self._step_find_and_tap("find_match_button", CoCState.FIND_MATCH)

            # 3. FIND_MATCH → WAIT_VILLAGE  (detect-only; no tap)
            self._step_wait_for_template(
                "battle_ui_indicator", CoCState.WAIT_VILLAGE,
            )

            # 4. WAIT_VILLAGE → DROP_ARMY  (1 s settle then transition)
            time.sleep(1.0)
            self._transition(CoCState.DROP_ARMY)

            # 5. DROP_ARMY → WAIT_BATTLE  (fixed-coord deploy)
            self._step_drop_army()
            self._transition(CoCState.WAIT_BATTLE)

            # 6. WAIT_BATTLE → END_BATTLE  (monotonic 170 s wait)
            self._step_wait_battle()
            self._transition(CoCState.END_BATTLE)

            # 7. END_BATTLE → CONFIRM
            self._step_find_and_tap("surrender_button", CoCState.CONFIRM)

            # 8. CONFIRM → RETURN_HOME
            self._step_find_and_tap("surrender_confirm", CoCState.RETURN_HOME)

            # 9. RETURN_HOME → COMPLETE
            self._step_find_and_tap("return_home_button", CoCState.COMPLETE)

        except CoCStepError as exc:
            failure_step = self._state
            try:
                self._transition(CoCState.FAILED)
            except CoCStepError:
                # `_transition` enforces the FSM table; if the
                # current state already disallows FAILED (only
                # COMPLETE and FAILED itself do), preserve the
                # current state — it's already a sensible
                # terminal. Belt-and-braces — should not happen.
                pass
            failure_reason = str(exc)

        elapsed_s = time.monotonic() - t0
        return CoCBotResult(
            success=(self._state is CoCState.COMPLETE),
            final_state=self._state,
            states_visited=list(self._visited),
            failure_reason=failure_reason,
            failure_step=failure_step,
            elapsed_s=elapsed_s,
            ts=ts_start,
        )

    # ---- steps -----------------------------------------------------------

    def _launch_clash(self) -> None:
        """Launch CoC via explicit package intent.

        Uses `monkey -p <pkg> -c android.intent.category.LAUNCHER 1`
        — the portable Android idiom for "launch this package's
        default LAUNCHER activity". monkey returns immediately
        after kicking off the launch; the bot then sleeps
        `launch_wait_s` to let CoC's splash → home transition
        settle before the first SEARCH attempt.

        Raises `CoCStepError` on ADB failure.
        """
        try:
            self.adb.shell(list(LAUNCH_COMMAND), timeout=15.0)
        except ADBError as exc:
            raise CoCStepError(
                f"failed to launch {COC_PACKAGE} via monkey: {exc}"
            ) from exc
        time.sleep(self.launch_wait_s)

    def _step_find_and_tap(
        self, template_name: str, next_state: CoCState,
    ) -> None:
        """Find + tap + verify-gone using a per-step Orchestrator + Watchdog.

        Composes the framework's `Orchestrator` (SEARCH → ACT →
        VALIDATE cycle) wrapped in a `Watchdog` (fault
        containment + timeout flag). Polls `wd.run_tick()` until
        either:

        - `result.success == True` → transition to `next_state`.
        - `time.monotonic()` exceeds the per-step deadline
          → raise `CoCStepError` (caller surfaces as FAILED).
        """
        if template_name not in self.templates:
            raise CoCStepError(
                f"template {template_name!r} not in pack — refusing to act"
            )
        tpl = self.templates.get(template_name)

        orch = Orchestrator(self.sensor, self.matcher, self.actuator, tpl)
        wd = Watchdog(orch)

        deadline = time.monotonic() + self.per_step_timeout_s
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            # Reset between iterations if a prior tick ended in FAILED.
            if orch.state is _FrameworkState.FAILED:
                orch.reset()
            elif orch.state is not _FrameworkState.IDLE:
                # Belt-and-braces — should not happen because the
                # orchestrator either ends in IDLE (success) or
                # FAILED (any non-happy exit).
                raise CoCStepError(
                    f"orchestrator in unexpected state "
                    f"{orch.state.value} mid-step {template_name!r}"
                )
            tick_result = wd.run_tick()
            if tick_result.success:
                _LOG.info(
                    "CoC step OK: %s after %d attempt(s)",
                    template_name, attempts,
                )
                self._transition(next_state)
                return
            _LOG.debug(
                "CoC step still trying: %s attempt=%d", template_name, attempts,
            )
            time.sleep(self.poll_interval_s)

        raise CoCStepError(
            f"step {template_name!r} timed out after "
            f"{self.per_step_timeout_s:.1f} s ({attempts} attempts)"
        )

    def _step_wait_for_template(
        self, template_name: str, next_state: CoCState,
    ) -> None:
        """Wait until a template appears on screen (no tap).

        Polls Sensor + Matcher directly because the framework's
        Orchestrator would ALSO tap on a HIT — and for the
        `battle_ui_indicator` step we explicitly do NOT want to
        tap the matched UI.
        """
        if template_name not in self.templates:
            raise CoCStepError(
                f"template {template_name!r} not in pack — refusing to wait"
            )
        tpl = self.templates.get(template_name)
        deadline = time.monotonic() + self.per_step_timeout_s
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            frame = self.sensor.capture()
            match = self.matcher.match(frame, tpl)
            if match.found:
                _LOG.info(
                    "CoC wait OK: %s after %d attempt(s)",
                    template_name, attempts,
                )
                self._transition(next_state)
                return
            time.sleep(self.poll_interval_s)
        raise CoCStepError(
            f"wait-for-template {template_name!r} timed out after "
            f"{self.per_step_timeout_s:.1f} s ({attempts} attempts)"
        )

    def _step_drop_army(self) -> None:
        """Execute the v1 fixed-coord deployment pattern.

        Uses Sensor once (to acquire `native_width/height` for the
        Actuator's denormalization) then issues the
        `DEPLOY_TAP_SEQUENCE_REF` taps in order. A short pacing
        sleep separates taps so the device's input handler can
        keep up.
        """
        frame = self.sensor.capture()
        nw, nh = frame.native_width, frame.native_height
        for i, (x_ref, y_ref) in enumerate(DEPLOY_TAP_SEQUENCE_REF, start=1):
            result = self.actuator.tap(x_ref, y_ref, nw, nh)
            if not result.success:
                raise CoCStepError(
                    f"deployment tap #{i} at ref ({x_ref},{y_ref}) failed: "
                    f"actuator returned success=False"
                )
            time.sleep(0.4)

    def _step_wait_battle(self) -> None:
        """Monotonic wait for `battle_wait_s` seconds.

        Sleeps in 5-second chunks (or the remainder, whichever is
        smaller) so a future caller could in principle interrupt
        by raising on a wake. v1.0 has no interrupt surface, but
        the chunked sleep makes the code play nicely under
        `KeyboardInterrupt` too.
        """
        target = time.monotonic() + self.battle_wait_s
        while True:
            remaining = target - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 5.0))

    # ---- internal helpers ------------------------------------------------

    def _transition(self, to_state: CoCState) -> None:
        """FSM chokepoint. Raises `CoCStepError` on disallowed move."""
        if not is_allowed(self._state, to_state):
            allowed = sorted(s.value for s in ALLOWED_TRANSITIONS[self._state])
            raise CoCStepError(
                f"illegal CoC FSM transition "
                f"{self._state.value} → {to_state.value}; "
                f"allowed from {self._state.value}: {allowed}"
            )
        _LOG.debug(
            "CoC transition %s → %s", self._state.value, to_state.value,
        )
        self._state = to_state
        self._visited.append(to_state)


__all__ = [
    "CoCTrophyBot",
    "CoCBotResult",
    "CoCStepError",
    "COC_PACKAGE",
    "LAUNCH_COMMAND",
    "DEPLOY_TAP_SEQUENCE_REF",
    "DEFAULT_BATTLE_WAIT_S",
    "DEFAULT_LAUNCH_WAIT_S",
    "DEFAULT_PER_STEP_TIMEOUT_S",
    "DEFAULT_POLL_INTERVAL_S",
]
