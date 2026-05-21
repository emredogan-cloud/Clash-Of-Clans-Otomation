"""Pure-Python metrics collector with JSON persistence.

Phase 6's metrics surface is intentionally narrow:

- **Counters** — monotonic non-negative integers; bumped by
  `observe_tick`, `observe_action`, `observe_match`. Persisted as a
  flat `int` dict.
- **Histograms** — fixed-bucket count distributions. Bucket
  boundaries are the cumulative upper edges (inclusive). Sample
  `s` lands in bucket `i` iff
  `buckets[i-1] < s ≤ buckets[i]` (with `buckets[-1] = +∞` as the
  overflow bucket). This matches Prometheus histogram semantics.

The collector does **not** depend on Prometheus. The persisted
format is a JSON file (`var/metrics/metrics.json`) that downstream
tools (replay CLI, dashboards, soak post-mortems) can parse
trivially.

The collector does **not** spin a background thread or daemon.
The caller decides when to invoke `persist()`. Each `observe_*`
call is fast (constant time; no I/O) so the caller is expected to
call `persist()` at convenient cadence (end-of-tick, periodic,
on-demand).

Bucket layouts are **mandated by `PHASE-MASTER-PROMPTS.md` Phase 6**
(amended in Phase 5.5 to acknowledge validated-tick reality):

- Tick duration (ms): `50, 100, 200, 400, 800, 1600, 3200, 6400`
  — 3200 ms catches the validated+retry p95 (~2956 ms per
  `phase5-report.md` §4); 6400 ms is fault headroom.
- Tap latency (ms): `10, 25, 50, 100, 200, 500` — Phase 4
  measured tap median 58.8 ms; the 50/100 buckets are the working
  band.
- Match latency (ms): `1, 2, 5, 10, 25, 50, 100` — Phase 3
  measured ROI grayscale ~2 ms, full-frame grayscale ~50 ms.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

from .errors import MetricsError
from .paths import METRICS

_LOG = logging.getLogger(__name__)

METRICS_DIR: Path = METRICS

# ----- mandated bucket layouts (all milliseconds) ----------------------------

TICK_BUCKETS_MS: tuple[float, ...] = (50, 100, 200, 400, 800, 1600, 3200, 6400)
TAP_BUCKETS_MS: tuple[float, ...] = (10, 25, 50, 100, 200, 500)
MATCH_BUCKETS_MS: tuple[float, ...] = (1, 2, 5, 10, 25, 50, 100)

# Tier labels for the per-tier tick histogram.
TICK_TIERS: tuple[str, ...] = ("search_only", "validated", "validated_retry")

# Counter names per the Phase 6 prompt.
COUNTER_NAMES: tuple[str, ...] = (
    "ticks_total",
    "ticks_success",
    "ticks_failed",
    "retries_total",
    "validation_ticks",
    "actions_total",
    "matches_total",
)


def _validate_buckets(buckets: tuple[float, ...]) -> None:
    if not buckets:
        raise MetricsError("histogram must have at least one bucket")
    last = -float("inf")
    for b in buckets:
        if not isinstance(b, (int, float)) or isinstance(b, bool):
            raise MetricsError(
                f"bucket boundary must be a number, got {type(b).__name__}"
            )
        if b <= last:
            raise MetricsError(
                f"bucket boundaries must be strictly increasing, got {buckets}"
            )
        last = b


class _Histogram:
    """Fixed-bucket histogram. Per-bucket counts + sum + total.

    Internal-only; users go through `MetricsCollector.observe_*`.
    """

    __slots__ = ("buckets", "counts", "sum_ms", "count")

    def __init__(self, buckets: tuple[float, ...]) -> None:
        _validate_buckets(buckets)
        self.buckets: tuple[float, ...] = buckets
        # One extra bucket for the overflow (sample > last boundary).
        self.counts: list[int] = [0] * (len(buckets) + 1)
        self.sum_ms: float = 0.0
        self.count: int = 0

    def observe(self, sample_ms: float) -> None:
        if not isinstance(sample_ms, (int, float)) or isinstance(sample_ms, bool):
            raise MetricsError(
                f"observation must be a number, got {type(sample_ms).__name__}"
            )
        if sample_ms < 0:
            raise MetricsError(f"observation must be >= 0, got {sample_ms}")
        for i, edge in enumerate(self.buckets):
            if sample_ms <= edge:
                self.counts[i] += 1
                break
        else:
            self.counts[-1] += 1  # overflow
        self.sum_ms += float(sample_ms)
        self.count += 1

    def snapshot(self) -> dict[str, Any]:
        # Bucket labels as Prometheus-style "le=<edge>" plus "+Inf"
        # for the overflow. Persistable as a plain dict.
        labels: list[str] = [f"le={edge}" for edge in self.buckets]
        labels.append("le=+Inf")
        return {
            "buckets_ms": list(self.buckets),
            "counts": list(self.counts),
            "labels": labels,
            "sum_ms": self.sum_ms,
            "count": self.count,
        }


class MetricsCollector:
    """Counters + histograms with on-demand JSON persistence.

    Thread-safe within one process via an internal lock — all
    `observe_*` and `persist()` calls hold the lock briefly.

    Construct once and reuse for the process lifetime.
    """

    def __init__(self, metrics_dir: Path | None = None) -> None:
        self.metrics_dir: Path = (
            metrics_dir if metrics_dir is not None else METRICS_DIR
        )
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {name: 0 for name in COUNTER_NAMES}
        # Per-tier tick histograms. A "search_only" tick goes into the
        # search_only histogram only; a "validated" tick into the
        # validated histogram only; same for "validated_retry".
        self._tick_histograms: dict[str, _Histogram] = {
            tier: _Histogram(TICK_BUCKETS_MS) for tier in TICK_TIERS
        }
        # Per-action-type histogram (currently only "tap" is exercised
        # in v1.0; swipe/long_press tracked under the same bucket
        # layout but in separate keyed histograms).
        self._action_histograms: dict[str, _Histogram] = {}
        self._match_histogram: _Histogram = _Histogram(MATCH_BUCKETS_MS)

    # ---- public API --------------------------------------------------

    def observe_tick(
        self,
        *,
        latency_ms: float,
        tier: str,
        success: bool,
        retries_used: int,
    ) -> None:
        """Record one tick.

        Tier MUST be one of `TICK_TIERS` ("search_only", "validated",
        "validated_retry"). Counters bumped:

        - `ticks_total` += 1
        - `ticks_success` += 1 if `success` else `ticks_failed` += 1
        - `retries_total` += `retries_used`
        - `validation_ticks` += 1 if tier in {"validated", "validated_retry"}
        """
        if tier not in TICK_TIERS:
            raise MetricsError(
                f"tier must be one of {TICK_TIERS}, got {tier!r}"
            )
        if not isinstance(retries_used, int) or isinstance(retries_used, bool):
            raise MetricsError(
                f"retries_used must be int, got {type(retries_used).__name__}"
            )
        if retries_used < 0:
            raise MetricsError(
                f"retries_used must be >= 0, got {retries_used}"
            )
        with self._lock:
            self._tick_histograms[tier].observe(latency_ms)
            self._counters["ticks_total"] += 1
            if success:
                self._counters["ticks_success"] += 1
            else:
                self._counters["ticks_failed"] += 1
            self._counters["retries_total"] += retries_used
            if tier in ("validated", "validated_retry"):
                self._counters["validation_ticks"] += 1

    def observe_action(
        self,
        *,
        action_type: str,
        latency_ms: float,
    ) -> None:
        """Record one action invocation.

        The histogram is keyed by `action_type` ("tap" / "swipe" /
        "long_press"). Counter `actions_total` bumped.
        """
        if not isinstance(action_type, str) or not action_type:
            raise MetricsError(
                f"action_type must be a non-empty string, got {action_type!r}"
            )
        with self._lock:
            hist = self._action_histograms.setdefault(
                action_type, _Histogram(TAP_BUCKETS_MS)
            )
            hist.observe(latency_ms)
            self._counters["actions_total"] += 1

    def observe_match(self, *, latency_ms: float) -> None:
        """Record one match invocation. Counter `matches_total` bumped."""
        with self._lock:
            self._match_histogram.observe(latency_ms)
            self._counters["matches_total"] += 1

    # ---- snapshots / persistence -------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot of the current metric state.

        Stable schema:

            {
              "counters": {<name>: <int>, ...},
              "tick_histograms": {
                "search_only":     {...},
                "validated":       {...},
                "validated_retry": {...}
              },
              "action_histograms": {"<action_type>": {...}, ...},
              "match_histogram": {...}
            }
        """
        with self._lock:
            return {
                "counters": dict(self._counters),
                "tick_histograms": {
                    tier: hist.snapshot()
                    for tier, hist in self._tick_histograms.items()
                },
                "action_histograms": {
                    name: hist.snapshot()
                    for name, hist in self._action_histograms.items()
                },
                "match_histogram": self._match_histogram.snapshot(),
            }

    def persist(self, *, filename: str = "metrics.json") -> Path:
        """Atomically write the current snapshot to JSON.

        Returns the path written. Atomic semantics: snapshot is
        encoded once, written to a `.tmp` sibling, fsynced, then
        renamed over the target. Concurrent readers always see a
        complete file.
        """
        snap = self.snapshot()
        try:
            blob = json.dumps(snap, indent=2, sort_keys=True) + "\n"
        except (TypeError, ValueError) as exc:
            raise MetricsError(
                f"could not JSON-encode metrics snapshot: {exc}"
            ) from exc
        try:
            self.metrics_dir.mkdir(parents=True, exist_ok=True)
            target = self.metrics_dir / filename
            _atomic_write_text(target, blob)
            return target
        except OSError as exc:
            raise MetricsError(f"persisting metrics failed: {exc}") from exc

    def counters_view(self) -> Mapping[str, int]:
        """Read-only view of the counter map. Useful for tests / replay."""
        with self._lock:
            return dict(self._counters)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically (tmp + fsync + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        shutil.move(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def derive_tier(
    *,
    action_ran: bool,
    validation_ran: bool,
    retries_used: int,
) -> str:
    """Derive the tick tier from FSM-path observables.

    The Phase 6 prompt mandates three tiers:

    - `"search_only"`  — no validation cycle executed. Includes both
                         search-miss ticks (no action) and action-
                         fail ticks (action ran but FAILED before
                         VALIDATING).
    - `"validated"`    — exactly one validation cycle executed
                         (`retries_used == 0`).
    - `"validated_retry"` — two validation cycles executed
                         (`retries_used == 1`).

    The orchestrator passes `action_ran` and `validation_ran` (=
    `validation_match is not None` in the Phase 5 implementation)
    plus `retries_used` from its `_finalize` path.
    """
    if not validation_ran:
        return "search_only"
    return "validated_retry" if retries_used > 0 else "validated"


__all__ = [
    "MetricsCollector",
    "TICK_BUCKETS_MS",
    "TAP_BUCKETS_MS",
    "MATCH_BUCKETS_MS",
    "TICK_TIERS",
    "COUNTER_NAMES",
    "METRICS_DIR",
    "derive_tier",
]
