#!/usr/bin/env python3
"""Phase 8B final soak — continuous supervised ticks for >= 2 hours.

Per the Phase 8B prompt's Group C:

- Real device.
- Continuous supervised ticks via the Phase 7 Watchdog (with the
  Phase 8B heartbeat auto-wire).
- Demonstrates: normal runtime, at least one recovery, at least
  one L2 recommendation, at least one executor restart action,
  continued runtime.
- Collects: ticks, success/fail, retries, timeout count,
  recoveries, restarts, heartbeat health, watchdog verdicts.
- Persists incrementally to `bench/results/final_soak.json`
  (atomic JSON rewrite every 30 ticks).

Configuration via env var:
    SOAK_DURATION_S — default 7200 (= 2 hours).
    SOAK_FAULT_INJECT_EVERY — default 50 (force a fault every Nth
                              tick to exercise the recovery path).
    SOAK_L2_CHECK_EVERY — default 10 (run an L2 watchdog check
                          every Nth tick).
    SOAK_EXECUTOR_DEMO_AFTER_S — default 600 (after 10 minutes,
                                 do one bounded executor demo
                                 against a controlled target).

The soak runs the L2 executor against a CONTROLLED TARGET
(spawned sleep process tagged with a unique argv keyword) so it
cannot kill the soak script itself. The executor's pkill pattern
is overridden to that tag; the framework defaults are NOT used
during the soak.

Reliability metrics computed at end:
    longest_healthy_streak_ticks  — longest run of consecutive
                                    ticks without an exception
                                    or timeout.
    timeout_rate                   — timeouts / ticks_total.
    recovery_success_rate          — recoveries_succeeded /
                                     recoveries_attempted.
    restart_count                  — number of executor restart
                                     events recorded.

Usage:
    .venv/bin/python -m scripts.phase8b_soak
    SOAK_DURATION_S=3600 .venv/bin/python -m scripts.phase8b_soak
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from automation.actuator import Actuator
from automation.adb import ADB
from automation.denormalize import Denormalizer
from automation.matcher import Matcher
from automation.orchestrator import Orchestrator
from automation.paths import ARTIFACTS, LOGS, METRICS, ensure_runtime_dirs
from automation.recovery import RecoveryManager
from automation.sensor import Sensor
from automation.state import State
from automation.template import Template
from automation.watchdog import Watchdog
from watchdog.action import RestartLimiter, WatchdogActionExecutor
from watchdog.heartbeat import HeartbeatWriter
from watchdog.watchdog import ExternalWatchdog


logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


HEARTBEAT_PATH = Path("var/watchdog/heartbeat.json")
RESTART_LOG = Path("var/run/watchdog-restarts.log")
SOAK_TARGET_TAG = "phase8b_soak_target"
SUMMARY_PATH = Path("bench/results/final_soak.json")


@dataclass
class SoakStats:
    """Aggregated counters maintained for the full soak run."""

    started_at: str
    duration_target_s: float
    ticks_total: int = 0
    ticks_success: int = 0
    ticks_failed: int = 0
    ticks_search_only: int = 0
    ticks_validated: int = 0  # cannot distinguish from validated_retry here
    exceptions_caught: int = 0
    timeouts_flagged: int = 0
    recoveries_attempted: int = 0
    recoveries_succeeded: int = 0
    heartbeats_written: int = 0
    l2_checks_run: int = 0
    l2_healthy: int = 0
    l2_stale: int = 0
    l2_missing: int = 0
    l2_invalid: int = 0
    restarts_attempted: int = 0
    restarts_blocked: int = 0
    restart_targets_killed: int = 0
    fault_injections_total: int = 0
    longest_healthy_streak_ticks: int = 0
    current_healthy_streak: int = 0
    tick_latency_samples_ms: list[float] = field(default_factory=list)
    last_correlation_id: str | None = None
    last_l2_verdict: str | None = None
    ended_at: str | None = None
    elapsed_s: float | None = None


def _start_settings(adb: ADB) -> None:
    adb.shell(["am", "start", "-W", "-a", "android.settings.SETTINGS"])
    time.sleep(0.8)


def _press_home(adb: ADB) -> None:
    adb.shell(["input", "keyevent", "3"])


def _make_random_template() -> Template:
    rng = np.random.default_rng(seed=20260521)
    img = rng.integers(0, 256, size=(96, 96), dtype=np.uint8)
    return Template(
        name="phase8b_soak_random",
        image_gray=img, width=96, height=96, threshold=0.95, roi=None,
    )


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    tmp.replace(path)


def _spawn_soak_target() -> subprocess.Popen:
    """Spawn a sleep process with `SOAK_TARGET_TAG` in its argv."""
    return subprocess.Popen(
        ["bash", "-c", f"exec -a {SOAK_TARGET_TAG} sleep 60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _persist(stats: SoakStats) -> None:
    """Atomic JSON dump of the current stats. Called periodically."""
    snapshot = dict(stats.__dict__)
    # Avoid persisting the raw tick-latency list every time — only at end.
    snapshot["tick_latency_ms_summary"] = (
        {
            "n": len(stats.tick_latency_samples_ms),
            "mean": statistics.fmean(stats.tick_latency_samples_ms),
            "median": statistics.median(stats.tick_latency_samples_ms),
            "p95": sorted(stats.tick_latency_samples_ms)[
                int(0.95 * (len(stats.tick_latency_samples_ms) - 1))
            ],
        }
        if stats.tick_latency_samples_ms
        else None
    )
    snapshot.pop("tick_latency_samples_ms", None)
    _atomic_write_json(SUMMARY_PATH, snapshot)


def _ensure_idle(orch: Orchestrator) -> None:
    """Pre-tick guard — orchestrator must be IDLE."""
    if orch.state is State.FAILED:
        orch.reset()
    elif orch.state is not State.IDLE:
        orch._transition(State.FAILED, reason="soak bridge")
        orch.reset()


def main() -> int:
    ensure_runtime_dirs()
    duration_s = float(os.environ.get("SOAK_DURATION_S", "7200"))
    fault_inject_every = int(os.environ.get("SOAK_FAULT_INJECT_EVERY", "50"))
    l2_check_every = int(os.environ.get("SOAK_L2_CHECK_EVERY", "10"))
    executor_demo_after_s = float(
        os.environ.get("SOAK_EXECUTOR_DEMO_AFTER_S", "600")
    )

    # Reset state files for the soak run.
    if HEARTBEAT_PATH.exists():
        HEARTBEAT_PATH.unlink()
    if RESTART_LOG.exists():
        RESTART_LOG.unlink()

    adb = ADB()
    sensor = Sensor(adb, mode="raw")
    matcher = Matcher()
    actuator = Actuator(
        adb, denormalizer=Denormalizer((1080, 1920)), seed=20260521,
    )
    orch = Orchestrator(sensor, matcher, actuator, _make_random_template())
    recovery = RecoveryManager(orch, adb)
    hb = HeartbeatWriter(HEARTBEAT_PATH)
    wd_l1 = Watchdog(orch, recovery=recovery, heartbeat=hb)

    # L2 observer — runs in-process within this soak script (cheap;
    # the prompt does NOT require a separate process for the soak).
    l2_obs = ExternalWatchdog(HEARTBEAT_PATH, stale_after_s=15.0)

    # L2 executor — pkill pattern points at the SOAK_TARGET_TAG so
    # we never accidentally kill the soak script. Even the soak's
    # own argv (which contains "phase8b_soak") would NOT match the
    # tag (different string).
    limiter = RestartLimiter(RESTART_LOG, max_restarts=3, window_s=300)
    executor = WatchdogActionExecutor(
        commands={
            "RESET_LITE": ["pkill", "-TERM", "-f", SOAK_TARGET_TAG],
            "RESET_HARD": ["pkill", "-KILL", "-f", SOAK_TARGET_TAG],
        },
        limiter=limiter,
    )

    started_at = datetime.now(tz=timezone.utc)
    stats = SoakStats(
        started_at=started_at.isoformat(),
        duration_target_s=duration_s,
    )
    _persist(stats)

    print(
        f"phase8b soak: duration_target={duration_s}s "
        f"fault_inject_every={fault_inject_every} "
        f"l2_check_every={l2_check_every} "
        f"executor_demo_after_s={executor_demo_after_s}"
    )

    # Inert baseline (Phase 6.5).
    _start_settings(adb)

    executor_demo_done = False
    t_start_s = time.monotonic()

    while True:
        elapsed = time.monotonic() - t_start_s
        if elapsed >= duration_s:
            break
        try:
            _ensure_idle(orch)

            # Fault injection schedule:
            # - every N ticks (with N >= 5): force orch into SEARCHING
            #   so the next tick() raises InvalidTransitionError →
            #   watchdog catches → recovery.
            fault_now = (
                stats.ticks_total > 0
                and fault_inject_every > 0
                and (stats.ticks_total % fault_inject_every) == 0
            )
            if fault_now:
                orch._transition(
                    State.SEARCHING, reason="soak: synthetic fault",
                )
                stats.fault_injections_total += 1

            t_call0 = time.perf_counter_ns()
            try:
                result = wd_l1.run_tick()
            except Exception as exc:  # noqa: BLE001
                # The Phase 7 Watchdog should never propagate; if it
                # does, treat as a catastrophic event but keep going.
                stats.exceptions_caught += 1
                stats.ticks_total += 1
                stats.ticks_failed += 1
                stats.current_healthy_streak = 0
                if stats.ticks_total % 30 == 0:
                    _persist(stats)
                _ensure_idle(orch)
                continue
            t_call1 = time.perf_counter_ns()
            wall_ms = (t_call1 - t_call0) / 1e6

            stats.ticks_total += 1
            stats.tick_latency_samples_ms.append(wall_ms)
            if result.success:
                stats.ticks_success += 1
            else:
                stats.ticks_failed += 1
            if result.action_latency_ms is None:
                stats.ticks_search_only += 1
            else:
                stats.ticks_validated += 1

            # Health-based counters.
            health = wd_l1.last_health
            if health.degraded:
                # Differentiate timeout vs raised vs recovery-cycled.
                # The watchdog's last_health.last_error encodes which.
                if "TimeoutFault" in (health.last_error or ""):
                    stats.timeouts_flagged += 1
                # Recovery is only invoked when degraded; the watchdog
                # ran it.
                stats.recoveries_attempted += 1
                # `orchestrator_ok` reflects post-recovery FSM state.
                if health.orchestrator_ok:
                    stats.recoveries_succeeded += 1
                stats.current_healthy_streak = 0
            else:
                stats.current_healthy_streak += 1
                stats.longest_healthy_streak_ticks = max(
                    stats.longest_healthy_streak_ticks,
                    stats.current_healthy_streak,
                )

            if HEARTBEAT_PATH.is_file():
                stats.heartbeats_written += 1
                try:
                    blob = json.loads(HEARTBEAT_PATH.read_text())
                    stats.last_correlation_id = blob.get("correlation_id")
                except (OSError, json.JSONDecodeError):
                    pass

            # Periodic L2 check.
            if (
                stats.ticks_total > 0
                and (stats.ticks_total % l2_check_every) == 0
            ):
                vstatus = l2_obs.check()
                stats.l2_checks_run += 1
                stats.last_l2_verdict = vstatus.status
                if vstatus.status == "HEALTHY":
                    stats.l2_healthy += 1
                elif vstatus.status == "STALE":
                    stats.l2_stale += 1
                elif vstatus.status == "MISSING":
                    stats.l2_missing += 1
                elif vstatus.status == "INVALID":
                    stats.l2_invalid += 1

            # One-shot executor demo after the configured delay.
            if (
                not executor_demo_done
                and elapsed >= executor_demo_after_s
            ):
                executor_demo_done = True
                # Spawn a controlled target, run the executor against
                # a synthetic STALE recommendation, count whether
                # the target was killed.
                target = _spawn_soak_target()
                time.sleep(0.4)
                from watchdog.watchdog import (
                    RECOMMENDATION_RESET_LITE,
                    WatchdogStatus,
                )
                synth = WatchdogStatus(
                    status="STALE", age_s=20.0,
                    recommendation=RECOMMENDATION_RESET_LITE,
                    ts=datetime.now(tz=timezone.utc),
                )
                r = executor.execute(synth)
                if r.attempted:
                    stats.restarts_attempted += 1
                if r.blocked:
                    stats.restarts_blocked += 1
                # Wait for kill propagation, then count.
                time.sleep(0.5)
                try:
                    target.wait(timeout=2)
                    if target.returncode is not None and target.returncode < 0:
                        # Killed by signal (returncode is -SIGTERM = -15).
                        stats.restart_targets_killed += 1
                except subprocess.TimeoutExpired:
                    target.kill()
                    target.wait(timeout=2)
                print(f"executor demo (tick #{stats.ticks_total}): {r.summary()}")

            # Persist every 30 ticks.
            if stats.ticks_total % 30 == 0:
                _persist(stats)
                # Friendly progress print every 30 ticks.
                rate = stats.ticks_total / max(elapsed, 1.0)
                print(
                    f"[{int(elapsed):5d}s] "
                    f"ticks={stats.ticks_total:5d} "
                    f"success={stats.ticks_success} fail={stats.ticks_failed} "
                    f"timeouts={stats.timeouts_flagged} "
                    f"recoveries={stats.recoveries_attempted} "
                    f"streak={stats.longest_healthy_streak_ticks} "
                    f"l2:{stats.last_l2_verdict} "
                    f"({rate:.2f} tick/s)"
                )

        except KeyboardInterrupt:
            print("KeyboardInterrupt — flushing and exiting cleanly.")
            break
        except Exception as exc:  # noqa: BLE001
            # Should never happen — but be paranoid in a long soak.
            stats.exceptions_caught += 1
            print(f"OUTER except: {type(exc).__name__}: {exc}")
            time.sleep(1.0)

    ended_at = datetime.now(tz=timezone.utc)
    stats.ended_at = ended_at.isoformat()
    stats.elapsed_s = (ended_at - started_at).total_seconds()
    _persist(stats)
    # Final compact summary print.
    print()
    print("=" * 72)
    print("SOAK COMPLETE")
    print("=" * 72)
    print(f"  duration_target_s:               {stats.duration_target_s}")
    print(f"  elapsed_s:                       {stats.elapsed_s:.1f}")
    print(f"  ticks_total:                     {stats.ticks_total}")
    print(f"  ticks_success / ticks_failed:    {stats.ticks_success} / {stats.ticks_failed}")
    print(f"  exceptions_caught:               {stats.exceptions_caught}")
    print(f"  timeouts_flagged:                {stats.timeouts_flagged}")
    print(f"  recoveries_attempted/succeeded:  {stats.recoveries_attempted} / {stats.recoveries_succeeded}")
    print(f"  fault_injections_total:          {stats.fault_injections_total}")
    print(f"  longest_healthy_streak_ticks:    {stats.longest_healthy_streak_ticks}")
    print(f"  heartbeats_written:              {stats.heartbeats_written}")
    print(f"  l2 (healthy/stale/missing/inv):  {stats.l2_healthy}/{stats.l2_stale}/{stats.l2_missing}/{stats.l2_invalid}")
    print(f"  restarts attempted/blocked/kills: {stats.restarts_attempted}/{stats.restarts_blocked}/{stats.restart_targets_killed}")
    print(f"  summary JSON: {SUMMARY_PATH}")

    _press_home(adb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
