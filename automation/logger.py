"""Structured JSONL logging — one line per record, append-only.

`StructuredLogger` writes two file streams under `var/logs/`:

- `ticks.jsonl` — one record per `Orchestrator.tick()` invocation.
  Carries the correlation id, FSM transition, success flag, and the
  four latency surfaces (`tick`, `capture`, `match`, `action`),
  plus `retries_used`. Schema is wire-stable; callers parse with
  any JSONL reader.

- `errors.jsonl` — one record per unhandled exception or
  framework-detected error condition surfaced via
  `log_error(...)`. Schema is wire-stable.

Atomic-append guarantees:

- The file is opened in `"ab"` (binary append). On POSIX, writes of
  less than `PIPE_BUF` (4 KiB on Linux) bytes are guaranteed not to
  interleave with concurrent writers (this is `man 2 write`). A
  single record is encoded once, terminated with `\\n`, and written
  in one `write()` call. We size-check at write time and refuse if
  the record exceeds 4 KiB (would indicate a payload bug, not a
  legitimate use case).
- We `flush()` and `fsync()` per write to ensure the record is on
  disk before `log_tick` returns. Phase 6 prioritises durability
  over throughput; the overhead is measured in `phase6-report.md`.

Logging is **best-effort by default**: I/O errors at the write
boundary are logged at WARN via the stdlib `logging` module and
swallowed. Structural errors (payload that cannot be JSON-encoded,
or a malformed schema field) raise `LoggingError` so the caller
notices.

No print debugging. No coloured logs. No external dependencies.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Mapping

from .errors import LoggingError
from .paths import LOGS

_LOG = logging.getLogger(__name__)

LOGS_DIR: Path = LOGS

# Hard ceiling per record. The PIPE_BUF guarantee on Linux is 4 KiB;
# we set the ceiling lower than that to give us headroom for a
# trailing newline and any encoding-expansion edge cases.
MAX_RECORD_BYTES: int = 3500

# Required field sets per record type. Used by `log_tick` /
# `log_error` to assemble payloads with consistent shape; tests
# assert against these sets.
TICK_FIELDS: frozenset[str] = frozenset({
    "correlation_id",
    "ts",
    "state_before",
    "state_after",
    "success",
    "tick_latency_ms",
    "capture_latency_ms",
    "match_latency_ms",
    "action_latency_ms",
    "retries_used",
})

ERROR_FIELDS: frozenset[str] = frozenset({
    "correlation_id",
    "ts",
    "error_type",
    "message",
    "state",
})


class StructuredLogger:
    """Append-only JSONL logger for tick and error events.

    Thread-safe within one process via an internal lock — multiple
    threads can call `log_tick`/`log_error` concurrently without
    interleaved bytes in the output file. Multi-process safety
    relies on the POSIX `PIPE_BUF` guarantee (records ≤ 3500 bytes;
    see module docstring).

    Construct once and reuse for the process lifetime.
    """

    def __init__(
        self,
        logs_dir: Path | None = None,
        *,
        ticks_filename: str = "ticks.jsonl",
        errors_filename: str = "errors.jsonl",
    ) -> None:
        self.logs_dir: Path = logs_dir if logs_dir is not None else LOGS_DIR
        self.ticks_path: Path = self.logs_dir / ticks_filename
        self.errors_path: Path = self.logs_dir / errors_filename
        self._lock = threading.Lock()

    # ---- public API --------------------------------------------------

    def log_tick(
        self,
        *,
        correlation_id: str,
        state_before: str,
        state_after: str,
        success: bool,
        tick_latency_ms: float,
        capture_latency_ms: float,
        match_latency_ms: float,
        action_latency_ms: float | None,
        retries_used: int,
        ts: _dt.datetime | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        """Append one tick record to `ticks.jsonl`.

        Required fields per the Phase 6 prompt. The `extra` mapping
        is optional and merged into the record; reserved-key collisions
        with the required fields raise `LoggingError`.
        """
        payload: dict[str, Any] = {
            "correlation_id": correlation_id,
            "ts": _ts_iso(ts),
            "state_before": state_before,
            "state_after": state_after,
            "success": bool(success),
            "tick_latency_ms": float(tick_latency_ms),
            "capture_latency_ms": float(capture_latency_ms),
            "match_latency_ms": float(match_latency_ms),
            "action_latency_ms": (
                float(action_latency_ms)
                if action_latency_ms is not None
                else None
            ),
            "retries_used": int(retries_used),
        }
        if extra:
            overlap = set(extra.keys()) & TICK_FIELDS
            if overlap:
                raise LoggingError(
                    f"log_tick extra keys collide with required fields: "
                    f"{sorted(overlap)}"
                )
            payload.update(extra)
        self._append_jsonl(self.ticks_path, payload)

    def log_error(
        self,
        *,
        correlation_id: str,
        error_type: str,
        message: str,
        state: str,
        ts: _dt.datetime | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        """Append one error record to `errors.jsonl`.

        `state` is the FSM state at the time of the error. `extra` is
        merged with the same collision rules as `log_tick`.
        """
        payload: dict[str, Any] = {
            "correlation_id": correlation_id,
            "ts": _ts_iso(ts),
            "error_type": error_type,
            "message": message,
            "state": state,
        }
        if extra:
            overlap = set(extra.keys()) & ERROR_FIELDS
            if overlap:
                raise LoggingError(
                    f"log_error extra keys collide with required fields: "
                    f"{sorted(overlap)}"
                )
            payload.update(extra)
        self._append_jsonl(self.errors_path, payload)

    # ---- internals ---------------------------------------------------

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        """Encode + append one JSONL record. Atomic per record."""
        try:
            line = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise LoggingError(
                f"could not JSON-encode {path.name} record: {exc}"
            ) from exc
        raw = (line + "\n").encode("utf-8")
        if len(raw) > MAX_RECORD_BYTES:
            raise LoggingError(
                f"{path.name} record exceeds {MAX_RECORD_BYTES} bytes "
                f"(got {len(raw)}); split or trim the payload"
            )
        with self._lock:
            try:
                self.logs_dir.mkdir(parents=True, exist_ok=True)
                # O_APPEND on POSIX makes the write atomic w.r.t. other
                # appenders; single-write keeps record-level atomicity.
                fd = os.open(
                    path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644
                )
                try:
                    os.write(fd, raw)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                _LOG.warning(
                    "could not append to %s: %s", path, exc,
                )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts_iso(ts: _dt.datetime | None) -> str:
    """Return `ts` (or now) as an ISO 8601 UTC string."""
    if ts is None:
        ts = _dt.datetime.now(tz=_dt.timezone.utc)
    if ts.tzinfo is None:
        raise LoggingError(
            f"timestamp must be timezone-aware, got naive {ts!r}"
        )
    return ts.isoformat()


__all__ = [
    "StructuredLogger",
    "LOGS_DIR",
    "MAX_RECORD_BYTES",
    "TICK_FIELDS",
    "ERROR_FIELDS",
]
