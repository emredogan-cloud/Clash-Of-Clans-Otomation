"""Clash of Clans trophy-drop bot — v1.0 domain layer.

Built on top of the v1.0 framework (Phase 0 → Phase 8B). The
framework is not modified. This package only provides:

- `coc.states`     — CoC-specific FSM enum + allowed-transitions
                     table. Separate from the framework's
                     `automation.state` (which is the SENSE/THINK/
                     ACT inner FSM); this one is the gameplay-step
                     FSM.
- `coc.templates`  — declarative template pack + loader. PNGs
                     live under `templates/`; the operator
                     captures and crops them via
                     `scripts/coc_template_capture.py`.
- `coc.bot`        — `CoCTrophyBot` composing Sensor + Matcher +
                     Actuator + Orchestrator + Watchdog into one
                     trophy-drop loop.

Scope (v1.0 trophy drop):

    HOME → ATTACK → FIND_MATCH → WAIT_VILLAGE → DROP_ARMY →
    WAIT_BATTLE (170 s) → END_BATTLE → CONFIRM → RETURN_HOME →
    COMPLETE

No game intelligence, no OCR, no ML, no strategy, no farming.
Single deployment pattern (fixed-coord troop taps). Fail-safe
on every error class — no random taps, no launcher wandering.
The CoC app is launched via explicit package intent
(`com.supercell.clashofclans`), never via launcher icons.
"""
__all__ = ["states", "templates", "bot"]
