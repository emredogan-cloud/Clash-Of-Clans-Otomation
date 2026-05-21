#!/usr/bin/env python3
"""Phase 8A live validation — L2 watchdog observation + escalation.

Demonstrates the three reachable verdicts of the external watchdog
against the operator's connected device:

1. **HEALTHY** — run a Phase 7 supervised tick, write a fresh
   heartbeat, immediately call `ExternalWatchdog.check()`. Expect
   `HEALTHY` + `none`.

2. **STALE** — write a heartbeat, configure the watchdog with a
   tight `stale_after_s` (e.g. 1 s), sleep 2 s, call `check()`.
   Expect `STALE` + `RESET_LITE`.

3. **INVALID** — overwrite the heartbeat with malformed JSON,
   call `check()`. Expect `INVALID` + `RESET_HARD`.

Also measures L2 check overhead (NFR: < 1 ms per check).

Crucially, the live demo:
- Modifies NO runtime code paths (no auto-wiring of the heartbeat
  into the Phase 7 Watchdog — that's Phase 8B work).
- Does NOT kill / signal / reboot anything. The external
  watchdog returns recommendations as data; the script reads them
  and prints them.

Usage:
    .venv/bin/python -m scripts.phase8a_live_validation
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import sys
import time
from pathlib import Path

# Make the project root importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from automation.actuator import Actuator
from automation.adb import ADB
from automation.denormalize import Denormalizer
from automation.matcher import Matcher
from automation.orchestrator import Orchestrator
from automation.paths import ARTIFACTS, ensure_runtime_dirs
from automation.recovery import RecoveryManager
from automation.runtime_health import RuntimeHealth
from automation.sensor import Sensor
from automation.template import Template
from automation.watchdog import Watchdog
from watchdog.heartbeat import HeartbeatWriter
from watchdog.watchdog import (
    DEFAULT_STALE_AFTER_S,
    ExternalWatchdog,
)


# Heartbeat location under the runtime tree.
HEARTBEAT_PATH = Path("var/watchdog/heartbeat.json")
L2_ARTIFACTS_DIR = ARTIFACTS / "external_watchdog"


def _start_settings(adb: ADB) -> None:
    """Phase 6.5 inert baseline."""
    adb.shell(["am", "start", "-W", "-a", "android.settings.SETTINGS"])
    time.sleep(0.8)


def _press_home(adb: ADB) -> None:
    adb.shell(["input", "keyevent", "3"])


def _make_random_template() -> Template:
    rng = np.random.default_rng(seed=20260521)
    img = rng.integers(0, 256, size=(96, 96), dtype=np.uint8)
    return Template(
        name="phase8a_random_miss",
        image_gray=img, width=96, height=96, threshold=0.95, roi=None,
    )


def main() -> int:
    ensure_runtime_dirs()
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if HEARTBEAT_PATH.exists():
        HEARTBEAT_PATH.unlink()
    if L2_ARTIFACTS_DIR.exists():
        shutil.rmtree(L2_ARTIFACTS_DIR)

    adb = ADB()
    sensor = Sensor(adb, mode="raw")
    matcher = Matcher()
    actuator = Actuator(
        adb, denormalizer=Denormalizer((1080, 1920)), seed=20260521,
    )
    orch = Orchestrator(sensor, matcher, actuator, _make_random_template())
    recovery = RecoveryManager(orch, adb)
    wd_l1 = Watchdog(orch, recovery=recovery)
    hb = HeartbeatWriter(HEARTBEAT_PATH)
    print(f"heartbeat path: {HEARTBEAT_PATH.resolve()}")
    print(f"default stale_after_s: {DEFAULT_STALE_AFTER_S}")

    _start_settings(adb)

    # ----- scenario 1: HEALTHY ------------------------------------------
    print()
    print("=" * 72)
    print("Scenario 1 — HEALTHY (fresh heartbeat after supervised tick)")
    print("=" * 72)
    r1 = wd_l1.run_tick()
    print(f"  L1 TickResult: {r1.summary()}")
    print(f"  L1 Health:     {wd_l1.last_health.summary()}")
    hb.beat(
        correlation_id="tick_phase8a_scenario1",
        runtime_health=wd_l1.last_health,
    )
    print(f"  heartbeat written: {HEARTBEAT_PATH.is_file()}")

    obs_healthy = ExternalWatchdog(
        HEARTBEAT_PATH, debug=True, artifacts_dir=L2_ARTIFACTS_DIR,
    )
    s_healthy = obs_healthy.check()
    print(f"  L2 verdict: {s_healthy.summary()}")
    assert s_healthy.status == "HEALTHY"

    # ----- scenario 2: forced STALE -------------------------------------
    print()
    print("=" * 72)
    print("Scenario 2 — forced STALE (stale_after_s=1.0, sleep 2 s)")
    print("=" * 72)
    # Re-use the heartbeat from scenario 1; configure a tight threshold.
    obs_stale = ExternalWatchdog(
        HEARTBEAT_PATH, stale_after_s=1.0,
        debug=True, artifacts_dir=L2_ARTIFACTS_DIR,
    )
    print("  sleeping 2 s …")
    time.sleep(2.0)
    s_stale = obs_stale.check()
    print(f"  L2 verdict: {s_stale.summary()}")
    assert s_stale.status == "STALE"
    assert s_stale.recommendation == "RESET_LITE"

    # ----- scenario 3: forced INVALID -----------------------------------
    print()
    print("=" * 72)
    print("Scenario 3 — forced INVALID (corrupted heartbeat)")
    print("=" * 72)
    HEARTBEAT_PATH.write_text("{this is deliberately not json")
    obs_invalid = ExternalWatchdog(
        HEARTBEAT_PATH, debug=True, artifacts_dir=L2_ARTIFACTS_DIR,
    )
    s_invalid = obs_invalid.check()
    print(f"  L2 verdict: {s_invalid.summary()}")
    assert s_invalid.status == "INVALID"
    assert s_invalid.recommendation == "RESET_HARD"

    # ----- overhead bench -----------------------------------------------
    print()
    print("=" * 72)
    print("Overhead bench — ExternalWatchdog.check() cost")
    print("=" * 72)
    # Rewrite a valid heartbeat so the bench measures the HEALTHY path
    # (the most-common production path).
    hb.beat("tick_phase8a_overhead", wd_l1.last_health)
    bench_obs = ExternalWatchdog(HEARTBEAT_PATH, debug=False)
    samples_ns: list[int] = []
    # Warmup.
    for _ in range(10):
        bench_obs.check()
    # Measure.
    for _ in range(500):
        t0 = time.perf_counter_ns()
        bench_obs.check()
        samples_ns.append(time.perf_counter_ns() - t0)
    mean_ms = statistics.fmean(samples_ns) / 1e6
    med_ms = statistics.median(samples_ns) / 1e6
    p95_ms = sorted(samples_ns)[int(0.95 * len(samples_ns))] / 1e6
    print(
        f"  over 500 calls: "
        f"mean={mean_ms:.4f} ms  median={med_ms:.4f} ms  p95={p95_ms:.4f} ms"
    )
    print(f"  NFR: < 1.000 ms per check.  Met: {med_ms < 1.0}")

    # ----- summary sidecar ----------------------------------------------
    summary = {
        "heartbeat_path": str(HEARTBEAT_PATH.resolve()),
        "scenario_1_healthy": {
            "status": s_healthy.status,
            "recommendation": s_healthy.recommendation,
            "age_s": s_healthy.age_s,
        },
        "scenario_2_stale": {
            "status": s_stale.status,
            "recommendation": s_stale.recommendation,
            "age_s": s_stale.age_s,
        },
        "scenario_3_invalid": {
            "status": s_invalid.status,
            "recommendation": s_invalid.recommendation,
            "age_s": s_invalid.age_s,
        },
        "overhead": {
            "mean_ms": mean_ms,
            "median_ms": med_ms,
            "p95_ms": p95_ms,
            "iterations": len(samples_ns),
            "nfr_lt_1ms_met": med_ms < 1.0,
        },
    }
    out_path = (
        Path(__file__).resolve().parent.parent
        / "bench" / "results" / "phase8a_live_validation.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    tmp.replace(out_path)
    print(f"\nSummary JSON: {out_path}")

    # Leave the orchestrator in IDLE for a clean handoff.
    from automation.state import State
    if orch.state is not State.IDLE:
        if orch.state is State.FAILED:
            orch.reset()
    _press_home(adb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
