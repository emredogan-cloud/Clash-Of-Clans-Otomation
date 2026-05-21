"""Runtime liveness beacon — atomic JSON heartbeat file.

`HeartbeatWriter.beat(...)` writes a fresh `heartbeat.json` to a
caller-provided path. The contents are the v1.0 L2 supervision
contract:

    {
      "ts":             "<ISO 8601 UTC>",
      "correlation_id": "<tick_YYYYMMDDTHHMMSS_xxxxxx>",
      "degraded":       <bool>,
      "health":         <runtime_health.to_debug_dict()>,
      "pid":            <int — process id of the framework runtime>
    }

The writer is **caller-driven**. It does not spawn a thread, it
does not run a loop, it does not register a signal handler. The
framework (typically the Phase 7 in-process `Watchdog`, or a
future Phase 8B run-loop) calls `beat(...)` after each supervised
tick — or whenever liveness should be re-asserted. The Phase 8A
prompt prohibits daemons and threads.

Atomic semantics: a temp file (in the same directory) is written
with `os.fsync()` and then `os.replace()`'d over the destination.
Concurrent readers (the external watchdog) always see either the
old complete file or the new complete file; never partial bytes.
This is the POSIX `man 2 rename` guarantee.

Best-effort I/O: a routine disk hiccup (transient permission
denial, ENOSPC) is logged at WARN and swallowed; the framework
cannot crash on a missed heartbeat. The narrowed `HeartbeatError`
class is reserved for *structural* faults — payload that cannot
be JSON-encoded, or a runtime_health argument that doesn't expose
`to_debug_dict()`. Those are caller bugs and must surface.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

# This module is part of the L2 watchdog package and is stdlib-only by
# constraint. The exception class is imported by absolute path from
# the framework's error hierarchy *only* because the
# `automation.errors` module is itself stdlib-only (no third-party
# deps). No `automation.runtime_health`, `automation.orchestrator`,
# etc. import is allowed here — the runtime_health value is
# duck-typed via the `_HealthLike` Protocol below.
from automation.errors import HeartbeatError

_LOG = logging.getLogger(__name__)

# Schema version. Bumped if a future Phase makes a breaking change
# to the heartbeat payload. The external watchdog reads this and
# refuses to interpret unknown major versions.
HEARTBEAT_SCHEMA_VERSION: int = 1

# Required top-level fields. Used by the writer to assemble the
# payload AND by the external watchdog to validate it.
REQUIRED_FIELDS: frozenset[str] = frozenset({
    "ts",
    "correlation_id",
    "degraded",
    "health",
    "pid",
    "schema_version",
})


@runtime_checkable
class _HealthLike(Protocol):
    """Duck-typed interface for the runtime-health argument.

    The framework's `automation.runtime_health.RuntimeHealth`
    satisfies this; so does any other object that exposes a
    `to_debug_dict()` method returning a JSON-encodable mapping.
    The protocol exists so this module does NOT need to import
    `automation.runtime_health` — preserving the process boundary
    the prompt mandates.
    """

    def to_debug_dict(self) -> Mapping[str, Any]: ...  # pragma: no cover


class HeartbeatWriter:
    """Atomic JSON heartbeat writer.

    Constructor params:

    - `path`: absolute or relative `Path` for `heartbeat.json`. The
      parent directory is created on first `beat()` if absent.

    Threading: not thread-safe by design. The framework is
    single-threaded (per ADR-07); one `HeartbeatWriter` per
    process is sufficient. If a future caller needs concurrency
    they can add an external `threading.Lock`.
    """

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise TypeError(
                f"HeartbeatWriter.path must be Path, got {type(path).__name__}"
            )
        self.path: Path = path

    # ---- public API --------------------------------------------------

    def beat(
        self,
        correlation_id: str,
        runtime_health: _HealthLike,
        *,
        ts: _dt.datetime | None = None,
    ) -> None:
        """Write one heartbeat record atomically.

        Required args:

        - `correlation_id`: the correlation id of the most recent
          supervised tick. Any non-empty string. Cross-referenceable
          with `var/logs/ticks.jsonl` and the orchestrator's
          artifact directories.
        - `runtime_health`: an object exposing `to_debug_dict()`
          returning a JSON-encodable mapping. Typically a
          `RuntimeHealth` instance; duck-typed for boundary
          isolation.

        Optional:

        - `ts`: override the timestamp (tests pass a fixed value).
          Defaults to `datetime.now(tz=UTC)`.

        Behavior:

        - `HeartbeatError` is raised on structural caller faults:
          a non-string `correlation_id`, a runtime_health without
          `to_debug_dict()`, a payload that cannot be JSON-encoded,
          or a naive `ts`.
        - Routine I/O failures (e.g. transient ENOSPC) are logged
          at WARN and swallowed. The next `beat()` call will try
          again; in the meantime the heartbeat goes stale and the
          external watchdog escalates per its policy.
        """
        if not isinstance(correlation_id, str) or not correlation_id:
            raise HeartbeatError(
                f"correlation_id must be a non-empty string, "
                f"got {correlation_id!r}"
            )
        if not isinstance(runtime_health, _HealthLike):
            raise HeartbeatError(
                "runtime_health must implement to_debug_dict() "
                f"(got {type(runtime_health).__name__})"
            )
        if ts is None:
            ts = _dt.datetime.now(tz=_dt.timezone.utc)
        elif not isinstance(ts, _dt.datetime):
            raise HeartbeatError(
                f"ts must be datetime, got {type(ts).__name__}"
            )
        elif ts.tzinfo is None:
            raise HeartbeatError("ts must be timezone-aware (UTC)")

        try:
            health_dict = dict(runtime_health.to_debug_dict())
        except Exception as exc:  # noqa: BLE001 — caller provided object
            raise HeartbeatError(
                f"runtime_health.to_debug_dict() raised "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        degraded = bool(health_dict.get("degraded", False))

        payload: dict[str, Any] = {
            "schema_version": HEARTBEAT_SCHEMA_VERSION,
            "ts": ts.isoformat(),
            "correlation_id": correlation_id,
            "degraded": degraded,
            "health": health_dict,
            "pid": os.getpid(),
        }
        try:
            blob = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        except (TypeError, ValueError) as exc:
            raise HeartbeatError(
                f"heartbeat payload not JSON-encodable: {exc}"
            ) from exc

        try:
            _atomic_write_text(self.path, blob)
        except OSError as exc:
            # Routine I/O fault → log and swallow. A genuine
            # write-side fault makes the external watchdog see a
            # stale beacon, which is the correct behaviour.
            _LOG.warning(
                "heartbeat: atomic write to %s failed (%s); "
                "next beat will retry", self.path, exc,
            )

    # ---- introspection -----------------------------------------------

    def last_written_ts(self) -> _dt.datetime | None:
        """Return the on-disk heartbeat's mtime as a tz-aware UTC dt.

        Convenience for callers that want to assert "the writer
        actually wrote" without parsing the JSON. Returns `None`
        if the file does not exist.
        """
        if not self.path.is_file():
            return None
        return _dt.datetime.fromtimestamp(
            self.path.stat().st_mtime, tz=_dt.timezone.utc,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically (tmp + fsync + rename).

    Distinct from `automation.metrics._atomic_write_text` only in
    that we deliberately don't import from `automation/*` to keep
    the watchdog package stdlib-only and process-boundary clean.
    Functionally identical.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # On POSIX, os.replace is atomic across rename. Use it
        # rather than shutil.move (which falls back to copy2 across
        # filesystems and is not atomic in that fallback).
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


__all__ = [
    "HeartbeatWriter",
    "HEARTBEAT_SCHEMA_VERSION",
    "REQUIRED_FIELDS",
]
