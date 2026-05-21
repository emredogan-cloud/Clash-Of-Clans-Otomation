"""Immutable per-subsystem health snapshot for the hardening layer.

`RuntimeHealth` is the single shape `RecoveryManager.recover(...)`
and `Watchdog.last_health` produce. It records:

- one boolean per subsystem (sensor / matcher / actuator / orchestrator);
- the last error observed during the supervising tick (or recovery
  attempt), as a stringified summary;
- a `degraded` flag that MUST be `True` whenever any subsystem is
  unhealthy OR `last_error` is set;
- a UTC timestamp.

The class is a container only — no I/O, no logic beyond field
validation. The hardening layer (`watchdog.py`, `recovery.py`)
composes instances of this class.

Why immutable? Health is a snapshot, not a mutable register. The
watchdog publishes snapshots to its `last_health` property after
each `run_tick()`; the recovery manager returns a snapshot from
`recover()`. Mutating either after publication would create races
between supervisor and consumer (Phase 8 soak harness, future
remote health endpoint, etc.). Frozen dataclass + a `healthy()`
helper covers v1.0.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class RuntimeHealth:
    """A snapshot of subsystem health at one instant.

    Field semantics:

    - `sensor_ok`       : True iff the SENSE pipeline is operable.
                          False after a capture failure or ADB
                          read-side fault.
    - `matcher_ok`      : True iff the THINK pipeline is operable.
                          False after a `MatcherError` (rare; the
                          matcher is mostly stateless CPU work).
    - `actuator_ok`     : True iff the ACT pipeline is operable.
                          False after an actuator failure or ADB
                          write-side fault.
    - `orchestrator_ok` : True iff the FSM is in a known-good state
                          (typically `IDLE`). False when the FSM is
                          stuck mid-tick or in `FAILED`.
    - `last_error`      : A short stringified description of the
                          most recent error observed. `None` when
                          everything is healthy.
    - `degraded`        : Coupled to the above: True iff any
                          `_ok` flag is False OR `last_error` is set.
                          Validated in `__post_init__`.
    - `ts`              : UTC instant the snapshot was taken
                          (timezone-aware datetime).
    """

    sensor_ok: bool
    matcher_ok: bool
    actuator_ok: bool
    orchestrator_ok: bool
    last_error: str | None
    degraded: bool
    ts: _dt.datetime

    def __post_init__(self) -> None:
        for label, value in (
            ("sensor_ok", self.sensor_ok),
            ("matcher_ok", self.matcher_ok),
            ("actuator_ok", self.actuator_ok),
            ("orchestrator_ok", self.orchestrator_ok),
            ("degraded", self.degraded),
        ):
            if not isinstance(value, bool):
                raise TypeError(
                    f"RuntimeHealth.{label} must be bool, got "
                    f"{type(value).__name__}"
                )
        if self.last_error is not None and not isinstance(self.last_error, str):
            raise TypeError(
                f"RuntimeHealth.last_error must be str or None, got "
                f"{type(self.last_error).__name__}"
            )
        if not isinstance(self.ts, _dt.datetime):
            raise TypeError(
                f"RuntimeHealth.ts must be datetime, got "
                f"{type(self.ts).__name__}"
            )
        if self.ts.tzinfo is None:
            raise ValueError("RuntimeHealth.ts must be timezone-aware (UTC)")

        any_unhealthy = not all(
            (
                self.sensor_ok,
                self.matcher_ok,
                self.actuator_ok,
                self.orchestrator_ok,
            )
        )
        any_error = self.last_error is not None and self.last_error != ""
        if (any_unhealthy or any_error) and not self.degraded:
            raise ValueError(
                "RuntimeHealth.degraded must be True when any "
                "subsystem is unhealthy or last_error is set"
            )
        if self.degraded and not (any_unhealthy or any_error):
            # Catch the inverse mistake too: claiming degraded with
            # no evidence is misleading for replay / dashboards.
            raise ValueError(
                "RuntimeHealth.degraded=True requires either an "
                "unhealthy subsystem or a non-empty last_error"
            )

    # ------------------------------------------------------------------

    def to_debug_dict(self) -> Mapping[str, Any]:
        """JSON-safe summary suitable for `metadata.json` artifacts."""
        return {
            "sensor_ok": self.sensor_ok,
            "matcher_ok": self.matcher_ok,
            "actuator_ok": self.actuator_ok,
            "orchestrator_ok": self.orchestrator_ok,
            "last_error": self.last_error,
            "degraded": self.degraded,
            "ts": self.ts.isoformat(),
        }

    def summary(self) -> str:
        """One-line human-readable summary."""
        flag = "DEGRADED" if self.degraded else "HEALTHY"
        subs = []
        if not self.sensor_ok:
            subs.append("sensor")
        if not self.matcher_ok:
            subs.append("matcher")
        if not self.actuator_ok:
            subs.append("actuator")
        if not self.orchestrator_ok:
            subs.append("orchestrator")
        impacted = ",".join(subs) if subs else "—"
        err = self.last_error or "—"
        return f"RuntimeHealth({flag} impacted={impacted} last_error={err!r})"

    # ------------------------------------------------------------------

    @classmethod
    def healthy(cls, *, ts: _dt.datetime | None = None) -> "RuntimeHealth":
        """Construct a fully-healthy snapshot at `ts` (default: now, UTC)."""
        if ts is None:
            ts = _dt.datetime.now(tz=_dt.timezone.utc)
        return cls(
            sensor_ok=True,
            matcher_ok=True,
            actuator_ok=True,
            orchestrator_ok=True,
            last_error=None,
            degraded=False,
            ts=ts,
        )


__all__ = ["RuntimeHealth"]
