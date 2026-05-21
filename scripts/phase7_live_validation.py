#!/usr/bin/env python3
"""Phase 7 live validation harness — supervision + timeout + recovery.

Demonstrates the three scenarios required by the Phase 7 prompt:

1. **Normal supervised tick.** Real device, Settings baseline (per
   Phase 6.5 hygiene), random-noise template. The watchdog
   supervises one tick; the orchestrator's SEARCH miss → FAILED
   path is recorded; the watchdog calls RecoveryManager which
   resets the orchestrator to IDLE and re-checks ADB.

2. **Forced timeout (post-hoc).** Same setup, but we configure a
   deliberately-tight search_only budget (e.g. 200 ms) so a real
   ~1 s SEARCH-only tick is flagged as exceeding budget. The
   watchdog records the timeout flag and invokes recovery.

3. **Forced recovery.** Force the orchestrator into a non-IDLE
   state externally and call `run_tick()`. The orchestrator raises
   InvalidTransitionError; the watchdog catches; recovery brings
   the FSM back to IDLE and the *next* `run_tick()` succeeds.

Also measures hardening overhead (watchdog wrapping cost vs raw
tick) and verifies the artifact + log + metrics paths still work.

Usage:
    .venv/bin/python -m scripts.phase7_live_validation

Sidecar JSON at `bench/results/phase7_live_validation.json`.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import statistics
import sys
import time
from pathlib import Path

# Make `automation` importable when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from automation.actuator import Actuator
from automation.adb import ADB
from automation.denormalize import Denormalizer
from automation.logger import StructuredLogger
from automation.matcher import Matcher
from automation.metrics import MetricsCollector
from automation.orchestrator import Orchestrator
from automation.paths import ARTIFACTS, LOGS, METRICS, ensure_runtime_dirs
from automation.recovery import RecoveryManager
from automation.sensor import Sensor
from automation.state import State
from automation.template import Template
from automation.watchdog import Watchdog


logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _start_settings(adb: ADB) -> None:
    """Phase 6.5 inert baseline: pre-launch the system Settings app."""
    adb.shell(["am", "start", "-W", "-a", "android.settings.SETTINGS"])
    time.sleep(0.8)


def _press_home(adb: ADB) -> None:
    adb.shell(["input", "keyevent", "3"])


def _make_random_template() -> Template:
    """Random-noise template — guaranteed SEARCH miss on any screen."""
    rng = np.random.default_rng(seed=20260521)
    img = rng.integers(0, 256, size=(96, 96), dtype=np.uint8)
    return Template(
        name="phase7_random_miss",
        image_gray=img, width=96, height=96, threshold=0.95, roi=None,
    )


def main() -> int:
    ensure_runtime_dirs()
    # Reset per-phase artifacts for a clean demo run.
    wd_artifacts = ARTIFACTS / "watchdog"
    if wd_artifacts.exists():
        shutil.rmtree(wd_artifacts)
    wd_artifacts.mkdir(parents=True, exist_ok=True)

    adb = ADB()
    sensor = Sensor(adb, mode="raw")
    matcher = Matcher()
    actuator = Actuator(
        adb, denormalizer=Denormalizer((1080, 1920)), seed=20260521,
    )
    logger = StructuredLogger(logs_dir=LOGS)
    metrics = MetricsCollector(metrics_dir=METRICS)
    template = _make_random_template()
    orch = Orchestrator(
        sensor, matcher, actuator, template,
        logger=logger, metrics=metrics, debug=True,
    )
    recovery = RecoveryManager(orch, adb, logger=logger)

    _start_settings(adb)

    def _ensure_idle() -> None:
        """Bring the orchestrator back to IDLE between scenarios.

        SEARCH-miss ticks naturally land in FAILED (by design — that's
        the FSM contract). For the live demo to start each scenario
        from IDLE, we explicitly reset between blocks. The Watchdog
        does NOT auto-reset after a FAILED-by-design tick — that
        would conflate normal FSM behaviour with fault recovery.
        """
        if orch.state is State.FAILED:
            orch.reset()
        elif orch.state is not State.IDLE:
            # Mid-FSM (e.g. left over from scenario 3 setup).
            orch._transition(State.FAILED, reason="demo bridge")
            orch.reset()

    # ----- scenario 1: normal supervised tick --------------------------
    print("=" * 70)
    print("Scenario 1 — normal supervised tick (random-noise template)")
    print("=" * 70)
    wd1 = Watchdog(orch, recovery=recovery, debug=True)
    r1 = wd1.run_tick()
    print(f"  TickResult: {r1.summary()}")
    print(f"  Health:     {wd1.last_health.summary()}")
    s1 = {
        "scenario": "normal_supervised_tick",
        "tick_latency_ms": r1.tick_latency_ms,
        "tick_success": r1.success,
        "state_after": r1.state_after.value,
        "health": dict(wd1.last_health.to_debug_dict()),
    }
    _ensure_idle()

    # ----- scenario 2: forced timeout via tight budget -----------------
    print()
    print("=" * 70)
    print("Scenario 2 — forced timeout (tight search_only budget = 200 ms)")
    print("=" * 70)
    wd2 = Watchdog(
        orch, recovery=recovery, debug=True,
        timeout_budgets_ms={
            "search_only": 200,       # deliberately tight
            "validated": 4000,
            "validated_retry": 5000,
        },
    )
    r2 = wd2.run_tick()
    print(f"  TickResult: {r2.summary()}")
    print(f"  Health:     {wd2.last_health.summary()}")
    timeout_observed = (
        wd2.last_health.last_error is not None
        and "TimeoutFault" in wd2.last_health.last_error
    )
    print(f"  Timeout flagged: {timeout_observed}")
    s2 = {
        "scenario": "forced_timeout",
        "tick_latency_ms": r2.tick_latency_ms,
        "budget_ms": 200,
        "timeout_flagged": timeout_observed,
        "health": dict(wd2.last_health.to_debug_dict()),
    }
    _ensure_idle()

    # ----- scenario 3: forced recovery via mid-FSM state ---------------
    print()
    print("=" * 70)
    print("Scenario 3 — forced recovery (orchestrator left in mid-FSM state)")
    print("=" * 70)
    # Force the orchestrator into an unexpected mid-FSM state by walking
    # through the same _transition chokepoint recovery itself uses. After
    # this, tick() will raise InvalidTransitionError because state != IDLE.
    orch._transition(State.SEARCHING, reason="phase7-demo: synthetic fault")
    print(f"  pre-tick state: {orch.state.value}")
    wd3 = Watchdog(orch, recovery=recovery, debug=True)
    r3 = wd3.run_tick()
    print(f"  TickResult: {r3.summary()}")
    print(f"  Health:     {wd3.last_health.summary()}")
    print(f"  post-recovery state: {orch.state.value}")
    s3 = {
        "scenario": "forced_recovery",
        "pre_tick_state": "SEARCHING",
        "tick_success": r3.success,
        "state_after": r3.state_after.value,
        "post_recovery_state": orch.state.value,
        "health": dict(wd3.last_health.to_debug_dict()),
    }

    # ----- overhead bench: watchdog wrap cost vs raw tick --------------
    print()
    print("=" * 70)
    print("Overhead bench — watchdog wrap cost (off-tick, payload-only)")
    print("=" * 70)
    # Don't go to the device for the overhead bench — measure the
    # wrapping cost only, which is what Phase 7 owns. The actual
    # device-touching tick cost is identical with/without watchdog.
    overhead_samples_ns: list[int] = []

    class _NoopOrch:
        state = State.IDLE
        def tick(self):
            return r1  # reuse the scenario-1 TickResult
        def reset(self):
            pass
        def _transition(self, *_a, **_kw):
            pass

    noop_wd = Watchdog(_NoopOrch(), debug=False)  # type: ignore[arg-type]
    for _ in range(500):
        t0 = time.perf_counter_ns()
        noop_wd.run_tick()
        overhead_samples_ns.append(time.perf_counter_ns() - t0)
    overhead_mean_ms = statistics.fmean(overhead_samples_ns) / 1e6
    overhead_med_ms = statistics.median(overhead_samples_ns) / 1e6
    overhead_p95_ms = sorted(overhead_samples_ns)[int(0.95 * len(overhead_samples_ns))] / 1e6
    print(
        f"  watchdog wrap cost over 500 calls (noop orchestrator): "
        f"mean={overhead_mean_ms:.3f} ms  median={overhead_med_ms:.3f} ms  "
        f"p95={overhead_p95_ms:.3f} ms"
    )
    # Approx % of a real search_only tick (~1.0 s on this device).
    overhead_pct_of_search = 100.0 * overhead_med_ms / 1000.0
    print(f"  ≈ {overhead_pct_of_search:.3f}% of a 1000 ms search_only tick")

    # Persist metrics so dashboards reflect the demo.
    metrics_path = metrics.persist()
    print(f"\nMetrics persisted at {metrics_path}")

    # ----- summary sidecar ----------------------------------------------
    summary = {
        "scenario_1_normal_supervised": s1,
        "scenario_2_forced_timeout": s2,
        "scenario_3_forced_recovery": s3,
        "overhead": {
            "mean_ms": overhead_mean_ms,
            "median_ms": overhead_med_ms,
            "p95_ms": overhead_p95_ms,
            "approx_pct_of_1s_tick": overhead_pct_of_search,
            "n_iterations": len(overhead_samples_ns),
        },
        "counters_final": dict(metrics.counters_view()),
    }
    out_path = (
        Path(__file__).resolve().parent.parent
        / "bench" / "results" / "phase7_live_validation.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    tmp.replace(out_path)
    print(f"\nSummary JSON: {out_path}")

    # Leave the device on the launcher.
    _press_home(adb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
