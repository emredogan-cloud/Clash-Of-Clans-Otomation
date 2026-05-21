#!/usr/bin/env python3
"""Phase 4 live validation harness.

Throwaway script that exercises the Actuator against the connected
device. Runs 20 iterations of each of {tap, swipe, long_press} in a
safe region of the screen and reports median / p95 / stdev for the
ADB-shell-side latency reported on each `ActionResult`.

Safe region:
- "Safe" = the lower-middle region of the home screen / launcher /
  whatever the device currently displays. We deliberately avoid the
  status bar (top), navigation bar (bottom), and the corner gestures
  that could trigger system actions.
- The reference-space anchor used is `(540, 1500)` — center column,
  ~78% down the reference frame (lower-middle).
- Swipes are vertical, short, and stay in the same safe band so
  they cannot trigger a back-gesture (left/right edges) or pull
  down notifications (top).

Usage:
    .venv/bin/python -m scripts.phase4_live_validation

Output:
- Console table with per-action latency stats.
- An aggregated JSON sidecar at
  `bench/results/phase4_live_validation.json` (atomic write).
- Per-action artifacts under `var/artifacts/actuator/` because the
  script runs the Actuator with `debug=True`.

Exit code 0 on success, non-zero if any action fails (ActionResult.success=False)
or if a CoordinateError raises.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

# Make `automation` importable when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation.actuator import Actuator
from automation.adb import ADB
from automation.denormalize import Denormalizer


SAFE_TAP_REF: tuple[int, int] = (540, 1500)   # lower-middle, safe.
SAFE_SWIPE_FROM_REF: tuple[int, int] = (540, 1400)
SAFE_SWIPE_TO_REF: tuple[int, int] = (540, 1100)  # short upward swipe
SAFE_LONG_PRESS_REF: tuple[int, int] = (540, 1500)

ITERATIONS: int = 20


def _stats(values: list[float]) -> dict[str, float]:
    """Compute median, p95, stdev, min, max, mean for a numeric series."""
    sv = sorted(values)
    n = len(sv)
    median = statistics.median(sv)
    p95_idx = min(n - 1, int(round(0.95 * (n - 1))))
    p95 = sv[p95_idx]
    return {
        "n": n,
        "mean": statistics.fmean(values),
        "median": median,
        "p95": p95,
        "stdev": statistics.stdev(values) if n > 1 else 0.0,
        "min": min(values),
        "max": max(values),
    }


def _print_table(label: str, stats: dict[str, float], success_count: int) -> None:
    print(
        f"  {label:>12}  n={stats['n']:>3}  "
        f"success={success_count}/{stats['n']}  "
        f"mean={stats['mean']:7.2f} ms  "
        f"median={stats['median']:7.2f} ms  "
        f"p95={stats['p95']:7.2f} ms  "
        f"stdev={stats['stdev']:6.2f} ms  "
        f"min={stats['min']:6.2f}  max={stats['max']:6.2f}"
    )


def _query_native_dims(adb: ADB) -> tuple[int, int]:
    """Return native (width, height) via `adb shell wm size`."""
    out = adb.shell(["wm", "size"]).strip()
    # Example: "Physical size: 1080x2408"
    # Or: "Physical size: 1080x2408\nOverride size: 1080x2160"
    for line in out.splitlines():
        if "size:" in line.lower():
            piece = line.split(":", 1)[1].strip()
            if "x" in piece:
                w, h = piece.split("x")
                return int(w), int(h)
    raise RuntimeError(f"could not parse wm size output: {out!r}")


def main() -> int:
    adb = ADB()
    native_w, native_h = _query_native_dims(adb)
    print(f"Device native: {native_w}x{native_h}")
    print(f"Reference:     1080x1920 (ADR-04)")
    print(f"Iterations per action: {ITERATIONS}")
    print()

    actuator = Actuator(
        adb,
        denormalizer=Denormalizer((1080, 1920)),
        seed=1234,
        debug=True,
    )

    all_stats: dict[str, dict[str, float | int]] = {}
    all_failures: dict[str, int] = {}

    # Warmup: one of each, discarded.
    actuator.tap(*SAFE_TAP_REF, native_w, native_h)
    actuator.swipe(*SAFE_SWIPE_FROM_REF, *SAFE_SWIPE_TO_REF,
                   native_w, native_h, duration_ms=300)
    actuator.long_press(*SAFE_LONG_PRESS_REF, native_w, native_h, duration_ms=600)
    time.sleep(0.4)

    # tap
    print("Running tap iterations...")
    lat_taps: list[float] = []
    succ_taps = 0
    for i in range(ITERATIONS):
        r = actuator.tap(*SAFE_TAP_REF, native_w, native_h)
        if r.success:
            succ_taps += 1
        lat_taps.append(r.latency_ms)
        time.sleep(0.15)  # minimal pacing so events don't queue
    s = _stats(lat_taps)
    all_stats["tap"] = s
    all_failures["tap"] = ITERATIONS - succ_taps
    _print_table("tap", s, succ_taps)

    # swipe
    print("Running swipe iterations...")
    lat_swipes: list[float] = []
    succ_swipes = 0
    for i in range(ITERATIONS):
        r = actuator.swipe(
            *SAFE_SWIPE_FROM_REF, *SAFE_SWIPE_TO_REF,
            native_w, native_h, duration_ms=300,
        )
        if r.success:
            succ_swipes += 1
        lat_swipes.append(r.latency_ms)
        time.sleep(0.25)  # let momentum settle
    s = _stats(lat_swipes)
    all_stats["swipe"] = s
    all_failures["swipe"] = ITERATIONS - succ_swipes
    _print_table("swipe", s, succ_swipes)

    # long_press
    print("Running long_press iterations...")
    lat_lp: list[float] = []
    succ_lp = 0
    for i in range(ITERATIONS):
        r = actuator.long_press(
            *SAFE_LONG_PRESS_REF, native_w, native_h, duration_ms=600,
        )
        if r.success:
            succ_lp += 1
        lat_lp.append(r.latency_ms)
        time.sleep(0.25)
    s = _stats(lat_lp)
    all_stats["long_press"] = s
    all_failures["long_press"] = ITERATIONS - succ_lp
    _print_table("long_press", s, succ_lp)

    # Sidecar JSON for the report.
    out_path = Path(__file__).resolve().parent.parent / "bench" / "results" / "phase4_live_validation.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "device_native": [native_w, native_h],
        "reference": [1080, 1920],
        "iterations": ITERATIONS,
        "anchors": {
            "tap_ref": list(SAFE_TAP_REF),
            "swipe_from_ref": list(SAFE_SWIPE_FROM_REF),
            "swipe_to_ref": list(SAFE_SWIPE_TO_REF),
            "long_press_ref": list(SAFE_LONG_PRESS_REF),
        },
        "results": all_stats,
        "failures": all_failures,
    }
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(out_path)
    print(f"\nSummary JSON written to {out_path}")

    total_failures = sum(all_failures.values())
    if total_failures > 0:
        print(f"\nWARN: {total_failures} action(s) reported ADB failure.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
