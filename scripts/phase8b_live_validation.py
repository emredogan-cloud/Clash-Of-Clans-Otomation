#!/usr/bin/env python3
"""Phase 8B live demo — heartbeat auto-wire + L2 executor + rate limiter.

Demonstrates the six requirements of the Phase 8B prompt's live
validation block:

1. **heartbeat auto-write** — the Phase 7 Watchdog (with the new
   heartbeat kwarg) writes a beat after every supervised tick.
2. **L2 HEALTHY** — the external watchdog reads the fresh beat
   and reports HEALTHY + recommendation=none.
3. **forced STALE** — tighten stale_after_s, sleep, re-check;
   expect STALE + RESET_LITE.
4. **executor RESET_LITE** — the WatchdogActionExecutor consumes
   the RESET_LITE recommendation and kills a controlled target
   subprocess (NOT the framework itself; the demo uses a
   sleep process whose argv contains the keyword "p8b_target",
   and the executor's pkill pattern targets that keyword).
5. **rate limiter block** — issue enough RESET_LITE actions back-
   to-back to exceed the ceiling; the next attempt returns
   ACTION_BLOCKED without spawning a subprocess.
6. **runtime survives** — after the demo, a final supervised tick
   succeeds, confirming the framework is intact.

NO frameworks were harmed in the making of this demo. The
executor's default `pkill -f automation` pattern is replaced with
`pkill -f p8b_target` so the kill is scoped to our spawned sleep
processes.

Usage:
    .venv/bin/python -m scripts.phase8b_live_validation
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from automation.actuator import Actuator
from automation.adb import ADB
from automation.denormalize import Denormalizer
from automation.matcher import Matcher
from automation.orchestrator import Orchestrator
from automation.paths import ARTIFACTS, ensure_runtime_dirs
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
TARGET_TAG = "p8b_target_e5fa713c"  # uniquely identifiable in argv


def _start_settings(adb: ADB) -> None:
    adb.shell(["am", "start", "-W", "-a", "android.settings.SETTINGS"])
    time.sleep(0.8)


def _press_home(adb: ADB) -> None:
    adb.shell(["input", "keyevent", "3"])


def _make_random_template() -> Template:
    rng = np.random.default_rng(seed=20260521)
    img = rng.integers(0, 256, size=(96, 96), dtype=np.uint8)
    return Template(
        name="phase8b_random_miss",
        image_gray=img, width=96, height=96, threshold=0.95, roi=None,
    )


def _spawn_target_sleep(duration_s: float = 30.0) -> subprocess.Popen:
    """Spawn `bash -c 'exec -a <TARGET_TAG> sleep <N>'` so the sleep
    process's argv contains TARGET_TAG, matchable by `pkill -f`."""
    return subprocess.Popen(
        [
            "bash", "-c",
            f"exec -a {TARGET_TAG} sleep {duration_s}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _count_target_processes() -> int:
    """Use pgrep -fc to count live processes matching TARGET_TAG."""
    cp = subprocess.run(
        ["pgrep", "-fc", TARGET_TAG],
        capture_output=True, text=True, check=False, timeout=5,
    )
    try:
        return int(cp.stdout.strip() or "0")
    except ValueError:
        return 0


def main() -> int:
    ensure_runtime_dirs()
    # Clean prior state for a deterministic demo.
    if HEARTBEAT_PATH.exists():
        HEARTBEAT_PATH.unlink()
    if RESTART_LOG.exists():
        RESTART_LOG.unlink()
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESTART_LOG.parent.mkdir(parents=True, exist_ok=True)
    l2_artifacts = ARTIFACTS / "external_watchdog"
    if l2_artifacts.exists():
        shutil.rmtree(l2_artifacts)

    adb = ADB()
    sensor = Sensor(adb, mode="raw")
    matcher = Matcher()
    actuator = Actuator(
        adb, denormalizer=Denormalizer((1080, 1920)), seed=20260521,
    )
    orch = Orchestrator(sensor, matcher, actuator, _make_random_template())
    recovery = RecoveryManager(orch, adb)
    hb = HeartbeatWriter(HEARTBEAT_PATH)
    # Phase 8B: heartbeat auto-write via Watchdog's new kwarg.
    wd_l1 = Watchdog(orch, recovery=recovery, heartbeat=hb)

    _start_settings(adb)

    # ----- scenario 1: heartbeat auto-write ----------------------------
    print("=" * 72)
    print("Scenario 1 — heartbeat auto-write after supervised tick")
    print("=" * 72)
    r1 = wd_l1.run_tick()
    print(f"  TickResult: {r1.summary()}")
    print(f"  heartbeat exists: {HEARTBEAT_PATH.is_file()}")
    if not HEARTBEAT_PATH.is_file():
        print("  FAIL: heartbeat was not written")
        return 1
    beat_payload = json.loads(HEARTBEAT_PATH.read_text())
    assert beat_payload["schema_version"] == 1
    print(
        f"  beat correlation_id: {beat_payload['correlation_id']}  "
        f"degraded: {beat_payload['degraded']}"
    )

    # ----- scenario 2: L2 HEALTHY --------------------------------------
    print()
    print("=" * 72)
    print("Scenario 2 — L2 HEALTHY")
    print("=" * 72)
    l2 = ExternalWatchdog(HEARTBEAT_PATH, stale_after_s=15.0)
    s2 = l2.check()
    print(f"  L2: {s2.summary()}")
    assert s2.status == "HEALTHY"
    assert s2.recommendation == "none"

    # ----- scenario 3: forced STALE ------------------------------------
    print()
    print("=" * 72)
    print("Scenario 3 — forced STALE (stale_after_s=1, sleep 2s)")
    print("=" * 72)
    l2_tight = ExternalWatchdog(HEARTBEAT_PATH, stale_after_s=1.0)
    print("  sleeping 2 s…")
    time.sleep(2.0)
    s3 = l2_tight.check()
    print(f"  L2: {s3.summary()}")
    assert s3.status == "STALE"
    assert s3.recommendation == "RESET_LITE"

    # ----- scenario 4: executor RESET_LITE against controlled target ---
    print()
    print("=" * 72)
    print("Scenario 4 — executor RESET_LITE against controlled target")
    print("=" * 72)
    # Spawn a target so pkill has something to kill.
    target = _spawn_target_sleep(duration_s=60.0)
    time.sleep(0.3)  # let it appear in /proc
    live_before = _count_target_processes()
    print(f"  target sleep procs alive before: {live_before}")
    assert live_before >= 1

    # Override the executor's pkill pattern to TARGET_TAG so we
    # don't touch the framework's own processes.
    limiter = RestartLimiter(
        RESTART_LOG, max_restarts=3, window_s=300,
    )
    executor = WatchdogActionExecutor(
        commands={
            "RESET_LITE": ["pkill", "-TERM", "-f", TARGET_TAG],
            "RESET_HARD": ["pkill", "-KILL", "-f", TARGET_TAG],
        },
        limiter=limiter,
    )
    r4 = executor.execute(s3)  # the STALE+RESET_LITE status from scenario 3
    print(f"  executor: {r4.summary()}")
    # Wait briefly for the OS to reap the killed process.
    time.sleep(0.5)
    target.wait(timeout=5)  # collect zombie
    live_after = _count_target_processes()
    print(f"  target sleep procs alive after: {live_after}")
    assert r4.attempted is True
    assert r4.action_type == "RESET_LITE"
    # pgrep returned 0 → no live target → kill succeeded.
    assert live_after == 0
    print(f"  target.returncode (after kill): {target.returncode}")

    # ----- scenario 5: rate limiter block ------------------------------
    print()
    print("=" * 72)
    print("Scenario 5 — rate limiter block (max 3 within 300 s)")
    print("=" * 72)
    # We already used 1 restart in scenario 4. Burn the next two,
    # then verify the fourth is blocked.
    spawned: list[subprocess.Popen] = []
    for i in range(2):
        spawned.append(_spawn_target_sleep(duration_s=60.0))
    time.sleep(0.3)
    for i in range(2):
        r = executor.execute(s3)
        print(f"  burn #{i+2}: {r.summary()}")
        time.sleep(0.3)
    # Spawn one more target and try to kill it — should be BLOCKED.
    spawned.append(_spawn_target_sleep(duration_s=60.0))
    time.sleep(0.3)
    live_pre = _count_target_processes()
    print(f"  alive before blocked attempt: {live_pre}")
    r5 = executor.execute(s3)
    print(f"  blocked attempt: {r5.summary()}")
    assert r5.blocked is True
    assert r5.attempted is False
    live_post = _count_target_processes()
    print(f"  alive after blocked attempt: {live_post}")
    assert live_post == live_pre  # blocked → no subprocess → target survives
    print(f"  recent_restart_count: {r5.recent_restart_count}")

    # Clean up the remaining target processes manually.
    for p in spawned:
        try:
            p.terminate()
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(timeout=2)

    # ----- scenario 6: runtime survives --------------------------------
    print()
    print("=" * 72)
    print("Scenario 6 — runtime survives (final supervised tick)")
    print("=" * 72)
    if orch.state is State.FAILED:
        orch.reset()
    elif orch.state is not State.IDLE:
        orch._transition(State.FAILED, reason="demo bridge")
        orch.reset()
    r6 = wd_l1.run_tick()
    print(f"  TickResult: {r6.summary()}")
    print(f"  heartbeat updated: {HEARTBEAT_PATH.is_file()}")
    # Read the latest heartbeat to confirm a fresh one was written.
    final_beat = json.loads(HEARTBEAT_PATH.read_text())
    print(f"  final beat correlation_id: {final_beat['correlation_id']}")
    assert final_beat["correlation_id"] != beat_payload["correlation_id"]

    # ----- summary sidecar ---------------------------------------------
    summary = {
        "scenario_1_heartbeat_auto_write": {
            "heartbeat_path": str(HEARTBEAT_PATH.resolve()),
            "first_beat_correlation_id": beat_payload["correlation_id"],
        },
        "scenario_2_l2_healthy": dict(s2.to_debug_dict()),
        "scenario_3_l2_stale": dict(s3.to_debug_dict()),
        "scenario_4_executor_reset_lite": dict(r4.to_debug_dict()),
        "scenario_5_rate_limiter_block": dict(r5.to_debug_dict()),
        "scenario_6_runtime_survives": {
            "final_tick_summary": r6.summary(),
            "final_beat_correlation_id": final_beat["correlation_id"],
        },
        "restart_log_lines": len(RESTART_LOG.read_text().splitlines()),
    }
    out_path = (
        Path(__file__).resolve().parent.parent
        / "bench" / "results" / "phase8b_live_validation.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    tmp.replace(out_path)
    print(f"\nSummary JSON: {out_path}")

    _press_home(adb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
