"""External L2 watchdog — heartbeat observer + escalation policy.

`ExternalWatchdog.check()` reads `heartbeat.json`, classifies its
freshness, and returns a `WatchdogStatus` carrying a
*recommendation* (data only — no side effects).

Process-boundary contract (Phase 8A prompt):

- Stdlib only. No imports from `automation/*` — not even error
  classes that would create a cycle. The only `automation`
  reference is `ExternalWatchdogError` from `automation.errors`,
  which is itself stdlib-only.
- No threads. No daemon. No signal handlers. No `kill -9`.
  No reboot. No `systemd` dependency.
- `check()` is one synchronous call. The caller decides the
  cadence (poll loop / cron / systemd timer — out of scope).
- The recommendation is *data*. The caller decides what (if
  anything) to do with it. Phase 8A explicitly does not act on
  the recommendation.

Status taxonomy:

| Status   | Trigger                                              | Recommendation |
|----------|------------------------------------------------------|----------------|
| HEALTHY  | heartbeat exists, parses, has valid schema, age ≤ T  | none           |
| STALE    | heartbeat exists, parses, valid schema, age > T      | RESET_LITE     |
| MISSING  | heartbeat file does not exist                        | RESET_LITE     |
| INVALID  | heartbeat exists but malformed JSON / bad schema     | RESET_HARD     |

The L1 → L2 division (ADR-11): L1 (Phase 7 `automation.watchdog`)
recovers from soft, *in-process* faults. L2 (this module)
observes the process from outside. When L2 says `RESET_HARD`,
that means the framework is in a state L1 cannot itself describe
(no valid heartbeat, no parseable health snapshot). The caller —
a future Phase 8B run-loop, a systemd unit, an operator script —
is responsible for translating the recommendation into action.

Optional artifacts: when `WATCHDOG_L2_DEBUG=1`, each `check()`
writes a `metadata.json` under `var/artifacts/external_watchdog/`
(atomic). Off by default; the L2 observer is silent in steady
state and noisy only when debugged.
"""
from __future__ import annotations

import datetime as _dt
import enum
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

# The L2 watchdog package is stdlib-only by contract. The single
# `automation.errors` import is permitted because that module is
# itself stdlib-only (only re-exports class hierarchy). No
# orchestrator / sensor / matcher / actuator imports here.
from automation.errors import ExternalWatchdogError

_LOG = logging.getLogger(__name__)

# Default freshness threshold. Heartbeats older than this are
# `STALE`. ADR-11 recommends "stale beyond a configured
# threshold"; 15 s is the SYSTEM-ROADMAP §11.1 heartbeat-staleness
# pre-Phase-0 estimate. The operator can override.
DEFAULT_STALE_AFTER_S: float = 15.0

# Schema version we understand. If the heartbeat carries a higher
# major version, the watchdog returns INVALID — the operator must
# upgrade the watchdog binary alongside the framework.
SUPPORTED_HEARTBEAT_SCHEMA_VERSION: int = 1

# Required top-level fields in the heartbeat payload.
_REQUIRED_FIELDS: frozenset[str] = frozenset({
    "ts",
    "correlation_id",
    "degraded",
    "health",
    "pid",
    "schema_version",
})

# Recommendation tokens — wire-stable strings consumed by future
# Phase 8B / operator scripts.
RECOMMENDATION_NONE: str = "none"
RECOMMENDATION_RESET_LITE: str = "RESET_LITE"
RECOMMENDATION_RESET_HARD: str = "RESET_HARD"

# Artifact location.
ARTIFACTS_DIR: Path = Path("var/artifacts/external_watchdog")


@enum.unique
class WatchdogStatusKind(enum.Enum):
    """The four mutually-exclusive freshness verdicts."""

    HEALTHY = "HEALTHY"
    STALE = "STALE"
    MISSING = "MISSING"
    INVALID = "INVALID"


def _parse_bool_env(var: str, default: bool = False) -> bool:
    raw = os.environ.get(var)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class WatchdogStatus:
    """Immutable container — the output of `ExternalWatchdog.check`.

    Field semantics:

    - `status`         : one of `WatchdogStatusKind` (str-valued).
                         The wire-stable verdict.
    - `age_s`          : heartbeat age in seconds (now − heartbeat.ts).
                         `None` for `MISSING` (no heartbeat to age)
                         and for `INVALID` when the ts cannot be
                         parsed. Otherwise a non-negative float.
    - `recommendation` : one of `"none"`, `"RESET_LITE"`, or
                         `"RESET_HARD"`. Data only — the caller
                         decides what to do.
    - `ts`             : UTC instant at which the check ran
                         (timezone-aware datetime).
    """

    status: str
    age_s: float | None
    recommendation: str
    ts: _dt.datetime

    def __post_init__(self) -> None:
        valid_statuses = {k.value for k in WatchdogStatusKind}
        if self.status not in valid_statuses:
            raise ValueError(
                f"WatchdogStatus.status must be one of {sorted(valid_statuses)}, "
                f"got {self.status!r}"
            )
        valid_recos = {
            RECOMMENDATION_NONE,
            RECOMMENDATION_RESET_LITE,
            RECOMMENDATION_RESET_HARD,
        }
        if self.recommendation not in valid_recos:
            raise ValueError(
                f"WatchdogStatus.recommendation must be one of "
                f"{sorted(valid_recos)}, got {self.recommendation!r}"
            )
        if self.age_s is not None:
            if isinstance(self.age_s, bool) or not isinstance(
                self.age_s, (int, float)
            ):
                raise TypeError(
                    f"WatchdogStatus.age_s must be float or None, "
                    f"got {type(self.age_s).__name__}"
                )
            if self.age_s < 0:
                raise ValueError(
                    f"WatchdogStatus.age_s must be >= 0, got {self.age_s}"
                )
        if not isinstance(self.ts, _dt.datetime):
            raise TypeError(
                f"WatchdogStatus.ts must be datetime, got {type(self.ts).__name__}"
            )
        if self.ts.tzinfo is None:
            raise ValueError("WatchdogStatus.ts must be timezone-aware (UTC)")

    # ------------------------------------------------------------------

    def to_debug_dict(self) -> Mapping[str, Any]:
        """JSON-safe summary for `metadata.json` artifacts."""
        return {
            "status": self.status,
            "age_s": (
                float(self.age_s) if self.age_s is not None else None
            ),
            "recommendation": self.recommendation,
            "ts": self.ts.isoformat(),
        }

    def summary(self) -> str:
        age = f"{self.age_s:.2f}s" if self.age_s is not None else "—"
        return (
            f"WatchdogStatus({self.status} age={age} "
            f"recommendation={self.recommendation})"
        )


class ExternalWatchdog:
    """Stdlib-only external observer of the framework's heartbeat.

    Constructor params:

    - `heartbeat_path` : path to the framework's `heartbeat.json`.
                         `Path` instance.
    - `stale_after_s`  : freshness threshold in seconds. Heartbeats
                         older than this are `STALE`. Must be > 0.
                         Default `15.0` (ADR-11 / SYSTEM-ROADMAP
                         §3.3 heartbeat-staleness target).
    - `debug`          : write a per-check artifact to
                         `var/artifacts/external_watchdog/` when
                         True. If `None`, consults the
                         `WATCHDOG_L2_DEBUG` env var at construction
                         time only (ADR-13: no runtime mutation).
    - `artifacts_dir`  : optional override for the artifact location.
                         Tests inject `tmp_path`.

    Threading: the watchdog itself is single-call. No internal
    state mutates after construction except the artifact write
    (which is best-effort). One process, one watchdog.
    """

    def __init__(
        self,
        heartbeat_path: Path,
        *,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        debug: bool | None = None,
        artifacts_dir: Path | None = None,
    ) -> None:
        if not isinstance(heartbeat_path, Path):
            raise ExternalWatchdogError(
                f"heartbeat_path must be Path, got "
                f"{type(heartbeat_path).__name__}"
            )
        if isinstance(stale_after_s, bool) or not isinstance(
            stale_after_s, (int, float)
        ):
            raise ExternalWatchdogError(
                f"stale_after_s must be a number, got "
                f"{type(stale_after_s).__name__}"
            )
        if stale_after_s <= 0:
            raise ExternalWatchdogError(
                f"stale_after_s must be > 0, got {stale_after_s}"
            )
        self.heartbeat_path: Path = heartbeat_path
        self.stale_after_s: float = float(stale_after_s)
        self.debug: bool = (
            debug if debug is not None
            else _parse_bool_env("WATCHDOG_L2_DEBUG")
        )
        self.artifacts_dir: Path = (
            artifacts_dir if artifacts_dir is not None else ARTIFACTS_DIR
        )

    # ---- public API --------------------------------------------------

    def check(self, *, now: _dt.datetime | None = None) -> WatchdogStatus:
        """Read the heartbeat once and emit a `WatchdogStatus`.

        Optional `now` is overridable for deterministic tests.
        Returns a freshly-constructed `WatchdogStatus`.

        Never raises on routine I/O / parse / schema faults —
        those are classified as `MISSING` or `INVALID`. Only
        construction-time argument faults raise
        `ExternalWatchdogError`.
        """
        if now is None:
            now = _dt.datetime.now(tz=_dt.timezone.utc)

        status_kind, age_s, recommendation, parse_note = self._classify(
            now=now
        )
        status = WatchdogStatus(
            status=status_kind.value,
            age_s=age_s,
            recommendation=recommendation,
            ts=now,
        )

        if self.debug:
            self._write_artifact(status=status, parse_note=parse_note)

        _LOG.debug(
            "L2 check: %s heartbeat=%s",
            status.summary(), self.heartbeat_path,
        )
        return status

    # ---- internals ---------------------------------------------------

    def _classify(
        self, *, now: _dt.datetime,
    ) -> tuple[WatchdogStatusKind, float | None, str, str | None]:
        """Read + classify. Returns (kind, age_s, recommendation, parse_note)."""
        if not self.heartbeat_path.is_file():
            return (
                WatchdogStatusKind.MISSING,
                None,
                RECOMMENDATION_RESET_LITE,
                "heartbeat file does not exist",
            )

        try:
            raw = self.heartbeat_path.read_text(encoding="utf-8")
        except OSError as exc:
            return (
                WatchdogStatusKind.INVALID,
                None,
                RECOMMENDATION_RESET_HARD,
                f"read failed: {exc}",
            )

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return (
                WatchdogStatusKind.INVALID,
                None,
                RECOMMENDATION_RESET_HARD,
                f"json decode failed: {exc}",
            )

        if not isinstance(payload, dict):
            return (
                WatchdogStatusKind.INVALID,
                None,
                RECOMMENDATION_RESET_HARD,
                f"payload root must be object, got {type(payload).__name__}",
            )

        missing = _REQUIRED_FIELDS - payload.keys()
        if missing:
            return (
                WatchdogStatusKind.INVALID,
                None,
                RECOMMENDATION_RESET_HARD,
                f"missing required fields: {sorted(missing)}",
            )

        sv = payload.get("schema_version")
        if not isinstance(sv, int) or isinstance(sv, bool):
            return (
                WatchdogStatusKind.INVALID,
                None,
                RECOMMENDATION_RESET_HARD,
                f"schema_version must be int, got {type(sv).__name__}",
            )
        if sv != SUPPORTED_HEARTBEAT_SCHEMA_VERSION:
            return (
                WatchdogStatusKind.INVALID,
                None,
                RECOMMENDATION_RESET_HARD,
                f"schema_version {sv} not supported "
                f"(this watchdog handles {SUPPORTED_HEARTBEAT_SCHEMA_VERSION})",
            )

        ts_str = payload.get("ts")
        if not isinstance(ts_str, str):
            return (
                WatchdogStatusKind.INVALID,
                None,
                RECOMMENDATION_RESET_HARD,
                f"ts must be string, got {type(ts_str).__name__}",
            )
        try:
            beat_ts = _dt.datetime.fromisoformat(ts_str)
        except ValueError:
            return (
                WatchdogStatusKind.INVALID,
                None,
                RECOMMENDATION_RESET_HARD,
                f"ts is not ISO 8601: {ts_str!r}",
            )
        if beat_ts.tzinfo is None:
            return (
                WatchdogStatusKind.INVALID,
                None,
                RECOMMENDATION_RESET_HARD,
                f"ts must be timezone-aware: {ts_str!r}",
            )

        age_s = (now - beat_ts).total_seconds()
        if age_s < 0:
            # Clock skew between writer and observer. Treat as fresh
            # but clip the age to zero — a negative age would
            # confuse downstream consumers and break the
            # WatchdogStatus invariant.
            age_s = 0.0

        if age_s > self.stale_after_s:
            return (
                WatchdogStatusKind.STALE,
                float(age_s),
                RECOMMENDATION_RESET_LITE,
                None,
            )

        return (
            WatchdogStatusKind.HEALTHY,
            float(age_s),
            RECOMMENDATION_NONE,
            None,
        )

    # ---- artifact ----------------------------------------------------

    def _write_artifact(
        self,
        *,
        status: WatchdogStatus,
        parse_note: str | None,
    ) -> None:
        """Write `metadata.json` for one check. Best-effort, never raises."""
        try:
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            ts_compact = status.ts.strftime("%Y%m%dT%H%M%S_%f")
            cap_dir = self.artifacts_dir / (
                f"check_{ts_compact}_{status.status.lower()}_{uuid.uuid4().hex[:8]}"
            )
            cap_dir.mkdir(parents=True, exist_ok=True)

            metadata: dict[str, Any] = {
                "status": status.status,
                "recommendation": status.recommendation,
                "heartbeat_age_s": status.age_s,
                "heartbeat_path": str(self.heartbeat_path),
                "stale_after_s": self.stale_after_s,
                "parse_note": parse_note,
                "ts": status.ts.isoformat(),
            }
            _atomic_write_text(
                cap_dir / "metadata.json",
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            )
            _LOG.debug("L2 watchdog: wrote artifact %s", cap_dir)
        except (OSError, ValueError) as exc:
            _LOG.warning("L2 watchdog: could not write artifact: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    """tmp + fsync + replace. Identical contract to heartbeat helper."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


__all__ = [
    "ExternalWatchdog",
    "WatchdogStatus",
    "WatchdogStatusKind",
    "ARTIFACTS_DIR",
    "DEFAULT_STALE_AFTER_S",
    "SUPPORTED_HEARTBEAT_SCHEMA_VERSION",
    "RECOMMENDATION_NONE",
    "RECOMMENDATION_RESET_LITE",
    "RECOMMENDATION_RESET_HARD",
]
