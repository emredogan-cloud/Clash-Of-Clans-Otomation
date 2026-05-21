"""Correlation-ID generation for single-tick traceability.

Every tick gets exactly one correlation id, threaded through:

- structured log lines (`ticks.jsonl`, `errors.jsonl`);
- metrics observations (carried in the per-tick observation tuple,
  not the persisted aggregate — metrics file holds *distributions*,
  not individual ticks);
- the orchestrator's per-tick artifact directory name AND
  `metadata.json` payload.

Format: `tick_<YYYYMMDDTHHMMSS>_<6 hex chars>`.

- The timestamp is UTC, encoded with `strftime("%Y%m%dT%H%M%S")`.
  Sortable lexicographically inside a date — which is the property
  operators need when correlating ticks across logs + artifacts +
  metrics.
- The 6-char hex tail is sampled from `random.SystemRandom` (the
  OS CSPRNG). 6 hex digits is ~24 bits of entropy; collisions
  inside a single second require ~2¹² ticks, well above the v1.0
  tick rate ceiling (1 Hz). Sub-second collisions are
  observed-against in the test suite.
- Filesystem-safe by construction: `[A-Za-z0-9_T]` only. Safe
  across all POSIX and Windows filesystems.
- No UUID4: 36-byte UUIDs in directory names + log lines bloat
  artifacts and make grep noisy. A 6-char hex suffix is the right
  signal-to-noise tradeoff for v1.0 single-device throughput.

The module exposes one public function — `new_id()` — plus a
typed `CorrelationId` (`NewType[str]`) for callsite documentation.
"""
from __future__ import annotations

import datetime as _dt
import random
from typing import NewType

CorrelationId = NewType("CorrelationId", str)

_HEX_CHARS = "0123456789abcdef"
_HEX_TAIL_LEN: int = 6  # ~24 bits of entropy

# `SystemRandom` is independent of `random.seed()` calls elsewhere in
# the process. Using it instead of `secrets.token_hex` keeps the surface
# tiny and avoids one import; functionally equivalent for this use case.
_SYS_RNG = random.SystemRandom()


def new_id(*, now: _dt.datetime | None = None) -> CorrelationId:
    """Return a fresh correlation id for one tick.

    Format: `tick_<YYYYMMDDTHHMMSS>_<6 hex chars>`. The timestamp is
    UTC. `now` is overridable for deterministic tests; default is
    `datetime.now(tz=UTC)`.

    The function does not memoize — every call returns a distinct id.
    Two calls within the same wall-clock second produce
    ids differing only in the 6-char hex tail.
    """
    ts = now if now is not None else _dt.datetime.now(tz=_dt.timezone.utc)
    if ts.tzinfo is None:
        raise ValueError("new_id() requires a timezone-aware datetime")
    prefix = ts.strftime("%Y%m%dT%H%M%S")
    tail = "".join(_SYS_RNG.choice(_HEX_CHARS) for _ in range(_HEX_TAIL_LEN))
    return CorrelationId(f"tick_{prefix}_{tail}")


def is_valid(candidate: str) -> bool:
    """Return True iff `candidate` matches the `new_id()` format exactly.

    Used by tests, and by tools that consume artifact directory names
    to extract the correlation id. Strict by design — partial or
    malformed ids return False rather than parse leniently.
    """
    if not isinstance(candidate, str):
        return False
    parts = candidate.split("_")
    # ["tick", "<YYYYMMDDTHHMMSS>", "<6 hex>"]
    if len(parts) != 3:
        return False
    if parts[0] != "tick":
        return False
    ts = parts[1]
    if len(ts) != 15 or ts[8] != "T":
        return False
    if not (ts[:8].isdigit() and ts[9:].isdigit()):
        return False
    tail = parts[2]
    if len(tail) != _HEX_TAIL_LEN:
        return False
    return all(c in _HEX_CHARS for c in tail)


__all__ = ["CorrelationId", "new_id", "is_valid"]
