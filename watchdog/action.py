"""L2 action layer — consume `WatchdogStatus.recommendation` and act.

Phase 8B closes the action half of ADR-11 / ADR-11a's L2
watchdog. The Phase 8A observation half (`watchdog.watchdog`)
produces wire-stable recommendation tokens — `"none"`,
`"RESET_LITE"`, `"RESET_HARD"`. This module turns those tokens
into one bounded subprocess invocation per call:

- `"none"`               → no-op (no subprocess spawned).
- `"RESET_LITE"`         → `pkill -TERM -f <pattern>` (default
                            pattern: `"automation"`).
- `"RESET_HARD"`         → `pkill -KILL -f <pattern>`.

A `relaunch_command` (optional) is invoked AFTER the kill on
either restart path. Default: none (operator's responsibility
to wire a substrate-specific launcher — `systemctl --user start
automation.service`, a custom shell command, etc.).

The executor is **best-effort and bounded**:

- One subprocess per call.
- Strict timeouts on `subprocess.run`.
- `shell=False` always (no `shell=True` anywhere).
- No root assumptions; `pkill` runs as the invoking user.
- A `RestartLimiter` enforces a sliding-window restart-rate
  ceiling. When breached, the executor returns
  `RestartActionResult(blocked=True, ...)` and does NOT spawn
  any subprocess.

Restart-rate ceiling:

- Default: 3 restarts per 5-minute sliding window
  (`max_restarts=3, window_s=300`).
- State persisted to `var/run/watchdog-restarts.log` —
  one ISO 8601 UTC timestamp + recommendation token per line.
- The limiter reads the log on every `is_allowed()` call and
  counts entries within the current window. State is therefore
  durable across process restarts of the *executor*
  itself — the operator can crash and restart the watchdog and
  the ceiling continues to be enforced.

Phase 8B prohibitions (still in force):

- No daemon, no background thread, no infinite loop, no signal
  handler.
- No `os.kill` directly; only `subprocess.run(["pkill", ...])`
  via the OS's pkill (which is itself a small bounded utility).
- No `shell=True`.
- No `systemd` Python bindings; the user-mode systemd unit
  consuming this module is operator-provided.
- No reboot. No `am force-stop`. No `adb kill-server`.
- No retries-with-backoff: one attempt per `execute()` call.
  The caller decides the cadence.

Schemas: `RestartActionResult` is the only return type. Its
fields are wire-stable for any future dashboard / log consumer.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, TYPE_CHECKING

# The action module is stdlib-only by contract. The single
# `automation.errors` import is permitted (that module is itself
# stdlib-only) so we keep the typed-exception hierarchy unified.
from automation.errors import WatchdogActionError

if TYPE_CHECKING:  # pragma: no cover — typing only
    from .watchdog import WatchdogStatus

_LOG = logging.getLogger(__name__)


# Default subprocess command lists. The Phase 8B prompt mandates
# these defaults: `pkill -TERM -f automation` for RESET_LITE,
# `pkill -KILL -f automation` for RESET_HARD. The operator can
# override the *pattern* (and the entire commands dict) via the
# `WatchdogActionExecutor` constructor.
DEFAULT_RESET_LITE_COMMAND: list[str] = ["pkill", "-TERM", "-f", "automation"]
DEFAULT_RESET_HARD_COMMAND: list[str] = ["pkill", "-KILL", "-f", "automation"]

# Default restart-rate ceiling (sliding window).
DEFAULT_MAX_RESTARTS: int = 3
DEFAULT_WINDOW_S: float = 300.0  # 5 minutes

# Default subprocess timeout. `pkill` returns in milliseconds in
# normal operation; a slow exit indicates an unhealthy host.
DEFAULT_SUBPROCESS_TIMEOUT_S: float = 5.0

# Log location.
DEFAULT_RESTART_LOG_PATH: Path = Path("var/run/watchdog-restarts.log")


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RestartActionResult:
    """The outcome of one `WatchdogActionExecutor.execute()` call.

    Field semantics:

    - `action_type`           : one of `"none"`, `"RESET_LITE"`,
                                `"RESET_HARD"`. Mirrors the
                                recommendation that was consumed.
    - `attempted`             : True iff at least one subprocess
                                was spawned. False for the `"none"`
                                no-op AND for the ACTION_BLOCKED
                                rate-limited path.
    - `blocked`               : True iff the rate limiter blocked
                                this attempt. When True, `attempted`
                                is False and no subprocess was run.
    - `primary_exit_code`     : exit code of the primary command
                                (the pkill). `None` if no subprocess
                                ran or if the call timed out.
    - `primary_stderr`        : trimmed stderr (≤ 1 KB) of the
                                primary command. Useful for
                                operator debugging.
    - `relaunched`            : True iff a relaunch_command was
                                configured AND was invoked.
    - `relaunch_exit_code`    : exit code of the relaunch command.
                                `None` if no relaunch ran.
    - `recent_restart_count`  : count of restart events recorded
                                within the limiter's window AS OF
                                the time of this call (after this
                                call's own log entry, if one was
                                recorded).
    - `notes`                 : free-text diagnostic. Used to
                                explain unusual paths
                                (e.g., "pkill not found", "timeout").
    - `ts`                    : UTC instant of the call.
    """

    action_type: str
    attempted: bool
    blocked: bool
    primary_exit_code: int | None
    primary_stderr: str | None
    relaunched: bool
    relaunch_exit_code: int | None
    recent_restart_count: int
    notes: str | None
    ts: _dt.datetime

    _ALLOWED_ACTIONS = frozenset({"none", "RESET_LITE", "RESET_HARD"})

    def __post_init__(self) -> None:
        if self.action_type not in RestartActionResult._ALLOWED_ACTIONS:
            raise WatchdogActionError(
                f"action_type must be one of "
                f"{sorted(RestartActionResult._ALLOWED_ACTIONS)}, "
                f"got {self.action_type!r}"
            )
        for label, value in (
            ("attempted", self.attempted),
            ("blocked", self.blocked),
            ("relaunched", self.relaunched),
        ):
            if not isinstance(value, bool):
                raise TypeError(
                    f"RestartActionResult.{label} must be bool, "
                    f"got {type(value).__name__}"
                )
        # blocked ⇒ attempted=False (rate limit fired before any subprocess).
        if self.blocked and self.attempted:
            raise WatchdogActionError(
                "RestartActionResult.blocked=True is incompatible with "
                "attempted=True"
            )
        if (
            not isinstance(self.recent_restart_count, int)
            or isinstance(self.recent_restart_count, bool)
        ):
            raise TypeError(
                f"recent_restart_count must be int, got "
                f"{type(self.recent_restart_count).__name__}"
            )
        if self.recent_restart_count < 0:
            raise WatchdogActionError(
                f"recent_restart_count must be >= 0, "
                f"got {self.recent_restart_count}"
            )
        if not isinstance(self.ts, _dt.datetime):
            raise TypeError(
                f"RestartActionResult.ts must be datetime, "
                f"got {type(self.ts).__name__}"
            )
        if self.ts.tzinfo is None:
            raise WatchdogActionError(
                "RestartActionResult.ts must be timezone-aware (UTC)"
            )

    def to_debug_dict(self) -> Mapping[str, Any]:
        return {
            "action_type": self.action_type,
            "attempted": self.attempted,
            "blocked": self.blocked,
            "primary_exit_code": self.primary_exit_code,
            "primary_stderr": self.primary_stderr,
            "relaunched": self.relaunched,
            "relaunch_exit_code": self.relaunch_exit_code,
            "recent_restart_count": self.recent_restart_count,
            "notes": self.notes,
            "ts": self.ts.isoformat(),
        }

    def summary(self) -> str:
        flag = "BLOCKED" if self.blocked else (
            "ATTEMPTED" if self.attempted else "noop"
        )
        return (
            f"RestartActionResult({self.action_type} {flag} "
            f"primary_exit={self.primary_exit_code} "
            f"relaunched={self.relaunched} "
            f"recent={self.recent_restart_count})"
        )


# ---------------------------------------------------------------------------
# Restart-rate limiter
# ---------------------------------------------------------------------------


# Line format: `<ISO 8601 UTC> <recommendation>\n`. Read by counting
# matching lines within the window. The regex is permissive enough to
# survive line-edge corruption (a half-written line is skipped).
_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\S+)\s+(?P<kind>RESET_LITE|RESET_HARD)\s*$"
)


class RestartLimiter:
    """Sliding-window restart-rate ceiling.

    The limiter persists its state to a JSONL-ish log file —
    one record per restart, ISO 8601 UTC timestamp + recommendation
    token per line. State is durable across executor process
    restarts.

    Constructor params:

    - `log_path`     : path to the restart log. Default:
                       `var/run/watchdog-restarts.log`. Parent
                       directory is created lazily on first record.
    - `max_restarts` : ceiling, inclusive. Default `3`.
    - `window_s`     : sliding window in seconds. Default `300`
                       (5 minutes).
    """

    def __init__(
        self,
        log_path: Path | None = None,
        *,
        max_restarts: int = DEFAULT_MAX_RESTARTS,
        window_s: float = DEFAULT_WINDOW_S,
    ) -> None:
        if log_path is None:
            log_path = DEFAULT_RESTART_LOG_PATH
        if not isinstance(log_path, Path):
            raise WatchdogActionError(
                f"log_path must be Path, got {type(log_path).__name__}"
            )
        if not isinstance(max_restarts, int) or isinstance(max_restarts, bool):
            raise WatchdogActionError(
                f"max_restarts must be int, got {type(max_restarts).__name__}"
            )
        if max_restarts <= 0:
            raise WatchdogActionError(
                f"max_restarts must be > 0, got {max_restarts}"
            )
        if (
            isinstance(window_s, bool)
            or not isinstance(window_s, (int, float))
        ):
            raise WatchdogActionError(
                f"window_s must be a number, got {type(window_s).__name__}"
            )
        if window_s <= 0:
            raise WatchdogActionError(
                f"window_s must be > 0, got {window_s}"
            )
        self.log_path: Path = log_path
        self.max_restarts: int = max_restarts
        self.window_s: float = float(window_s)

    # ---- public ------------------------------------------------------

    def is_allowed(
        self, *, now: _dt.datetime | None = None,
    ) -> tuple[bool, int]:
        """Return `(allowed, recent_count)`.

        - `recent_count` is the number of restart events in
          `(now - window_s, now]` according to the on-disk log.
        - `allowed` is `recent_count < max_restarts`.

        Routine I/O errors when reading the log → treat as
        empty (worst case the operator gets one extra restart;
        better than refusing recovery on a transient FS hiccup).
        """
        if now is None:
            now = _dt.datetime.now(tz=_dt.timezone.utc)
        recent = self._read_recent(now=now)
        recent_count = len(recent)
        return (recent_count < self.max_restarts), recent_count

    def record(
        self,
        kind: str,
        *,
        now: _dt.datetime | None = None,
    ) -> None:
        """Append one restart event to the log.

        `kind` must be `"RESET_LITE"` or `"RESET_HARD"`.

        Routine I/O failures are logged at WARN and swallowed.
        The next call may see fewer entries than expected; the
        worst-case effect is letting one extra restart through.
        """
        if kind not in {"RESET_LITE", "RESET_HARD"}:
            raise WatchdogActionError(
                f"kind must be RESET_LITE or RESET_HARD, got {kind!r}"
            )
        if now is None:
            now = _dt.datetime.now(tz=_dt.timezone.utc)
        if now.tzinfo is None:
            raise WatchdogActionError("now must be timezone-aware (UTC)")
        line = f"{now.isoformat()} {kind}\n"
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            # O_APPEND on POSIX is atomic for writes < PIPE_BUF.
            # The line is well under 4 KB. We use plain "a" mode.
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
        except OSError as exc:
            _LOG.warning(
                "RestartLimiter: could not append to %s: %s",
                self.log_path, exc,
            )

    # ---- internals ---------------------------------------------------

    def _read_recent(self, *, now: _dt.datetime) -> list[_dt.datetime]:
        """Return restart timestamps in the current window."""
        if not self.log_path.is_file():
            return []
        try:
            text = self.log_path.read_text(encoding="utf-8")
        except OSError as exc:
            _LOG.warning(
                "RestartLimiter: could not read %s: %s; assuming empty",
                self.log_path, exc,
            )
            return []
        cutoff = now - _dt.timedelta(seconds=self.window_s)
        out: list[_dt.datetime] = []
        for line in text.splitlines():
            m = _LOG_LINE_RE.match(line)
            if m is None:
                continue
            try:
                ts = _dt.datetime.fromisoformat(m.group("ts"))
            except ValueError:
                continue
            if ts.tzinfo is None:
                # Naive ts in the log — should not happen but be
                # defensive.
                continue
            if ts > cutoff:
                out.append(ts)
        return out


# ---------------------------------------------------------------------------
# Action executor
# ---------------------------------------------------------------------------


class WatchdogActionExecutor:
    """Consume a `WatchdogStatus` recommendation; perform one action.

    Constructor params:

    - `commands`         : dict mapping `"RESET_LITE"` and
                           `"RESET_HARD"` to subprocess argv lists.
                           Defaults to the v1.0 pkill commands above.
                           The operator can override either entry
                           (or both) to point at a different process
                           pattern, a different signal, or a
                           substrate-specific stop command (e.g.,
                           `["systemctl", "--user", "stop",
                           "automation.service"]`).
    - `relaunch_command` : optional argv list invoked AFTER a
                           successful pkill. Default `None`. The
                           operator can wire `["systemctl",
                           "--user", "start", "automation.service"]`
                           or a custom `[".venv/bin/python", "-m",
                           "automation.cli", "run", ...]`.
    - `limiter`          : optional `RestartLimiter`. Default is a
                           freshly-constructed limiter with the
                           module defaults.
    - `subprocess_timeout_s` : timeout for each subprocess call.
                           Default `5.0`.
    """

    def __init__(
        self,
        *,
        commands: dict[str, list[str]] | None = None,
        relaunch_command: list[str] | None = None,
        limiter: RestartLimiter | None = None,
        subprocess_timeout_s: float = DEFAULT_SUBPROCESS_TIMEOUT_S,
    ) -> None:
        if commands is None:
            commands = {
                "RESET_LITE": list(DEFAULT_RESET_LITE_COMMAND),
                "RESET_HARD": list(DEFAULT_RESET_HARD_COMMAND),
            }
        # Validate the commands dict.
        for kind in ("RESET_LITE", "RESET_HARD"):
            if kind not in commands:
                raise WatchdogActionError(
                    f"commands must include {kind!r}, got keys "
                    f"{sorted(commands.keys())}"
                )
            argv = commands[kind]
            if not isinstance(argv, list) or not argv:
                raise WatchdogActionError(
                    f"commands[{kind!r}] must be a non-empty list, "
                    f"got {argv!r}"
                )
            if not all(isinstance(a, str) for a in argv):
                raise WatchdogActionError(
                    f"commands[{kind!r}] must contain only strings, "
                    f"got {argv!r}"
                )
        if relaunch_command is not None:
            if (
                not isinstance(relaunch_command, list)
                or not relaunch_command
                or not all(isinstance(a, str) for a in relaunch_command)
            ):
                raise WatchdogActionError(
                    f"relaunch_command must be a non-empty list of strings, "
                    f"got {relaunch_command!r}"
                )
        if (
            isinstance(subprocess_timeout_s, bool)
            or not isinstance(subprocess_timeout_s, (int, float))
        ):
            raise WatchdogActionError(
                f"subprocess_timeout_s must be a number, got "
                f"{type(subprocess_timeout_s).__name__}"
            )
        if subprocess_timeout_s <= 0:
            raise WatchdogActionError(
                f"subprocess_timeout_s must be > 0, got "
                f"{subprocess_timeout_s}"
            )

        self.commands: dict[str, list[str]] = {
            k: list(v) for k, v in commands.items()
        }
        self.relaunch_command: list[str] | None = (
            list(relaunch_command) if relaunch_command is not None else None
        )
        self.limiter: RestartLimiter = (
            limiter if limiter is not None else RestartLimiter()
        )
        self.subprocess_timeout_s: float = float(subprocess_timeout_s)

    # ---- public ------------------------------------------------------

    def execute(
        self,
        status: "WatchdogStatus",
        *,
        now: _dt.datetime | None = None,
    ) -> RestartActionResult:
        """Consume `status.recommendation`. Perform at most one action.

        Returns a `RestartActionResult` describing what happened —
        no-op, rate-limited, or attempted (with subprocess exit
        codes and optional relaunch outcome). Never raises on
        routine subprocess failures.

        Caller-bug exceptions (`status` missing the
        `recommendation` attribute, or an unknown recommendation
        token) raise `WatchdogActionError`.
        """
        if now is None:
            now = _dt.datetime.now(tz=_dt.timezone.utc)

        # Duck-typed access to the recommendation. We don't import
        # WatchdogStatus to keep the type-only relationship; runtime
        # consumers pass any object with a `recommendation` string
        # attribute.
        try:
            recommendation = getattr(status, "recommendation")
        except AttributeError:
            raise WatchdogActionError(
                f"status object lacks .recommendation "
                f"(type {type(status).__name__})"
            )
        if not isinstance(recommendation, str):
            raise WatchdogActionError(
                f"status.recommendation must be str, "
                f"got {type(recommendation).__name__}"
            )

        # --- "none" recommendation: no-op ----------------------------
        if recommendation == "none":
            allowed, recent = self.limiter.is_allowed(now=now)
            # Don't touch the limiter or spawn anything.
            return RestartActionResult(
                action_type="none",
                attempted=False,
                blocked=False,
                primary_exit_code=None,
                primary_stderr=None,
                relaunched=False,
                relaunch_exit_code=None,
                recent_restart_count=recent,
                notes=None,
                ts=now,
            )

        if recommendation not in {"RESET_LITE", "RESET_HARD"}:
            raise WatchdogActionError(
                f"unknown recommendation: {recommendation!r}"
            )

        # --- rate-limit gate -----------------------------------------
        allowed, recent_before = self.limiter.is_allowed(now=now)
        if not allowed:
            _LOG.warning(
                "WatchdogActionExecutor: BLOCKED %s — %d restarts in "
                "%ds window (limit %d)",
                recommendation, recent_before, int(self.limiter.window_s),
                self.limiter.max_restarts,
            )
            return RestartActionResult(
                action_type=recommendation,
                attempted=False,
                blocked=True,
                primary_exit_code=None,
                primary_stderr=None,
                relaunched=False,
                relaunch_exit_code=None,
                recent_restart_count=recent_before,
                notes=(
                    f"rate-limited: {recent_before} restarts in last "
                    f"{int(self.limiter.window_s)}s ≥ "
                    f"max={self.limiter.max_restarts}"
                ),
                ts=now,
            )

        # --- attempt the primary command -----------------------------
        argv = list(self.commands[recommendation])
        primary_exit, primary_stderr, primary_note = self._invoke(argv)

        # --- record the restart event BEFORE relaunch, so a relaunch
        # that itself terminates this process leaves a complete log.
        self.limiter.record(recommendation, now=now)
        _, recent_after = self.limiter.is_allowed(now=now)

        # --- attempt optional relaunch -------------------------------
        relaunched = False
        relaunch_exit: int | None = None
        relaunch_note: str | None = None
        if self.relaunch_command is not None:
            relaunched = True
            relaunch_exit, _, relaunch_note = self._invoke(
                list(self.relaunch_command)
            )

        # Compose notes. Two sources possible (primary + relaunch).
        note_parts: list[str] = []
        if primary_note:
            note_parts.append(f"primary: {primary_note}")
        if relaunch_note:
            note_parts.append(f"relaunch: {relaunch_note}")
        notes = " | ".join(note_parts) if note_parts else None

        return RestartActionResult(
            action_type=recommendation,
            attempted=True,
            blocked=False,
            primary_exit_code=primary_exit,
            primary_stderr=primary_stderr,
            relaunched=relaunched,
            relaunch_exit_code=relaunch_exit,
            recent_restart_count=recent_after,
            notes=notes,
            ts=now,
        )

    # ---- internals ---------------------------------------------------

    def _invoke(
        self, argv: list[str],
    ) -> tuple[int | None, str | None, str | None]:
        """Run one subprocess. Return `(exit_code, stderr_excerpt, note)`.

        `exit_code` is `None` when the subprocess raises (file not
        found, timeout, etc.). `note` is a free-text diagnostic
        suitable for the result's `notes` field; `None` on the
        happy path.
        """
        try:
            cp = subprocess.run(
                argv,
                shell=False,
                check=False,
                capture_output=True,
                timeout=self.subprocess_timeout_s,
            )
        except FileNotFoundError as exc:
            _LOG.warning("subprocess %s missing: %s", argv[0], exc)
            return None, None, f"command not found: {argv[0]}"
        except subprocess.TimeoutExpired as exc:
            _LOG.warning(
                "subprocess %s timed out after %ss",
                argv[0], self.subprocess_timeout_s,
            )
            return None, None, (
                f"timed out after {self.subprocess_timeout_s}s"
            )
        except OSError as exc:
            _LOG.warning("subprocess %s OSError: %s", argv[0], exc)
            return None, None, f"OSError: {exc}"
        # cp.returncode is always int. Trim stderr to ≤ 1 KB.
        stderr = cp.stderr.decode("utf-8", errors="replace") if cp.stderr else ""
        if len(stderr) > 1024:
            stderr = stderr[:1024] + "…"
        return int(cp.returncode), (stderr or None), None


__all__ = [
    "RestartActionResult",
    "RestartLimiter",
    "WatchdogActionExecutor",
    "DEFAULT_RESET_LITE_COMMAND",
    "DEFAULT_RESET_HARD_COMMAND",
    "DEFAULT_MAX_RESTARTS",
    "DEFAULT_WINDOW_S",
    "DEFAULT_SUBPROCESS_TIMEOUT_S",
    "DEFAULT_RESTART_LOG_PATH",
]
