#!/usr/bin/env python3
"""Phase 5 live validation harness.

Throwaway script that exercises the Orchestrator end-to-end against
the connected device. Demonstrates two distinct FSM outcomes:

- **Fail (search miss):** a random-noise template is constructed
  that cannot possibly exist on the device's current screen. `tick()`
  enters SEARCHING, the matcher returns a MISS, the FSM transitions
  to FAILED. No action is sent.

- **Happy-ish path:** a high-entropy template is cropped from a
  freshly captured frame. `tick()` enters SEARCHING (HIT), ACTING
  (taps the matched anchor), VALIDATING (re-captures and re-matches).
  The orchestrator's exit state depends on whether the tap actually
  changes the screen — taps on inert regions land in VALIDATING-FAIL
  (template still present), taps on a real hit target (an app icon)
  land in IDLE (template gone). We report whatever happens honestly.

This script does NOT install templates on disk, does NOT use a screen
graph, and does NOT run a loop. Each tick() is a single
SENSE→THINK→ACT→VALIDATE cycle.

Usage:
    .venv/bin/python -m scripts.phase5_live_validation

Output:
- Console summary with both ticks.
- An aggregated JSON sidecar at
  `bench/results/phase5_live_validation.json` (atomic write).
- Per-tick artifacts under `var/artifacts/orchestrator/` because
  ORCH_DEBUG is enabled.

Safety:
- Press KEYCODE_HOME at script start to land on a known baseline.
- Tap anchor is the high-entropy patch's centre — in practice the
  lower-middle region of the home screen. Same safe region used in
  Phase-4 live validation.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np

# Make `automation` importable when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation.actuator import Actuator
from automation.adb import ADB
from automation.denormalize import Denormalizer
from automation.frame import Frame
from automation.matcher import Matcher
from automation.orchestrator import Orchestrator
from automation.sensor import Sensor
from automation.template import Template

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _press_home(adb: ADB) -> None:
    """Press KEYCODE_HOME to land on a known baseline."""
    adb.shell(["input", "keyevent", "3"])  # KEYCODE_HOME = 3


def _find_high_entropy_patch(
    frame: Frame,
    *,
    patch_w: int = 96,
    patch_h: int = 96,
    region: tuple[int, int, int, int] | None = None,
) -> tuple[int, int]:
    """Return the (x, y) top-left of the highest-stdev patch in `region`.

    `region` defaults to the lower-middle band of the reference frame.
    Picking a high-entropy patch keeps `TM_CCOEFF_NORMED` matching
    well-conditioned (a solid-colour patch has near-zero variance and
    correlates with everything).
    """
    if region is None:
        # lower-middle band; same safe area Phase 4 used.
        region = (300, 1100, 780, 1700)
    x1, y1, x2, y2 = region
    gray = cv2.cvtColor(frame.image_bgr, cv2.COLOR_BGR2GRAY)
    # Stride 32 px for a quick scan; finer search not needed.
    best = (-1.0, x1, y1)
    for y in range(y1, y2 - patch_h, 32):
        for x in range(x1, x2 - patch_w, 32):
            patch = gray[y:y + patch_h, x:x + patch_w]
            std = float(patch.std())
            if std > best[0]:
                best = (std, x, y)
    return best[1], best[2]


def _make_random_template() -> Template:
    """A random-noise template that cannot occur on the device screen."""
    rng = np.random.default_rng(seed=20260521)
    img = rng.integers(0, 256, size=(96, 96), dtype=np.uint8)
    return Template(
        name="phase5_random_miss",
        image_gray=img,
        width=96, height=96,
        threshold=0.95,  # high → won't false-match random noise
        roi=None,
    )


def _make_high_entropy_template(
    frame: Frame, x: int, y: int, *, w: int = 96, h: int = 96,
) -> Template:
    gray = cv2.cvtColor(frame.image_bgr, cv2.COLOR_BGR2GRAY)
    crop = gray[y:y + h, x:x + w].copy()
    return Template(
        name="phase5_high_entropy_hit",
        image_gray=crop,
        width=w, height=h,
        threshold=0.85,  # slightly relaxed; the screen may shift sub-pixel
        roi=None,
    )


def _tick_summary(result) -> dict:
    return {
        "state_before": result.state_before.value,
        "state_after": result.state_after.value,
        "success": result.success,
        "tick_latency_ms": result.tick_latency_ms,
        "capture_latency_ms": result.capture_latency_ms,
        "match_latency_ms": result.match_latency_ms,
        "action_latency_ms": result.action_latency_ms,
        "ts": result.ts.isoformat(),
    }


def _open_recents(adb: ADB) -> None:
    """KEYCODE_APP_SWITCH — opens the recent-apps switcher."""
    adb.shell(["input", "keyevent", "187"])  # KEYCODE_APP_SWITCH = 187


def _start_settings(adb: ADB) -> None:
    """Open the Settings app — populates the recents list."""
    adb.shell(["am", "start", "-a", "android.settings.SETTINGS"])


def main() -> int:
    os.environ["ORCH_DEBUG"] = "1"  # so artifacts land for the report
    adb = ADB()
    sensor = Sensor(adb, mode="raw")
    matcher = Matcher()
    actuator = Actuator(
        adb, denormalizer=Denormalizer((1080, 1920)), seed=4242,
    )

    _press_home(adb)
    time.sleep(0.6)

    # --- DEMO 1: fail-by-design (search miss) -----------------------
    print("=" * 70)
    print("DEMO 1 — random-noise template (expected: FAILED via search miss)")
    print("=" * 70)
    random_tpl = _make_random_template()
    orch_fail = Orchestrator(sensor, matcher, actuator, random_tpl)
    r_fail = orch_fail.tick()
    print(r_fail.summary())
    print(f"final state: {orch_fail.state.value}")
    assert r_fail.state_after.value == "FAILED"

    # --- DEMO 2: attempt happy path (homescreen high-entropy patch) -
    _press_home(adb)
    time.sleep(0.6)
    print()
    print("=" * 70)
    print("DEMO 2 — high-entropy homescreen template (outcome depends on hit target)")
    print("=" * 70)

    setup_frame = sensor.capture()
    x, y = _find_high_entropy_patch(setup_frame)
    print(f"high-entropy template anchor (reference px): ({x}, {y})")
    real_tpl = _make_high_entropy_template(setup_frame, x, y)
    orch_real = Orchestrator(sensor, matcher, actuator, real_tpl)
    r_real = orch_real.tick()
    print(r_real.summary())
    print(f"final state: {orch_real.state.value}")

    # --- DEMO 3: engineered happy path via recents ------------------
    print()
    print("=" * 70)
    print("DEMO 3 — engineered happy path: tap on Settings card in recents")
    print("=" * 70)
    # Setup: open Settings (populates recents), then HOME, then RECENTS.
    _start_settings(adb)
    time.sleep(1.0)
    _press_home(adb)
    time.sleep(0.6)
    _open_recents(adb)
    time.sleep(1.0)

    # Capture a frame of the recents view, crop a high-entropy patch
    # from the recents card region (upper-middle is where the most
    # recent card thumbnail sits).
    recents_frame = sensor.capture()
    # Recents-card region: roughly the upper 60% of the reference frame.
    rx, ry = _find_high_entropy_patch(
        recents_frame,
        region=(150, 400, 930, 1200),
    )
    print(f"recents-card template anchor (reference px): ({rx}, {ry})")
    recents_tpl = _make_high_entropy_template(
        recents_frame, rx, ry, w=128, h=128,
    )
    # Re-bind the template name so the artifact directory is recognisable.
    recents_tpl = Template(
        name="phase5_recents_card",
        image_gray=recents_tpl.image_gray,
        width=recents_tpl.width, height=recents_tpl.height,
        threshold=0.85, roi=None,
    )
    orch_recents = Orchestrator(sensor, matcher, actuator, recents_tpl)
    r_recents = orch_recents.tick()
    print(r_recents.summary())
    print(f"final state: {orch_recents.state.value}")

    # Cleanup: HOME so we're not stuck in the just-tapped app.
    _press_home(adb)

    # --- summary -----------------------------------------------------
    summary = {
        "device_native": [
            setup_frame.native_width, setup_frame.native_height,
        ],
        "reference": [setup_frame.width, setup_frame.height],
        "demo_1_search_miss": _tick_summary(r_fail),
        "demo_2_homescreen_patch": _tick_summary(r_real),
        "demo_3_recents_card": _tick_summary(r_recents),
        "anchors_ref": {
            "demo_2_homescreen": [x, y],
            "demo_3_recents": [rx, ry],
        },
    }
    out_path = (
        Path(__file__).resolve().parent.parent
        / "bench" / "results" / "phase5_live_validation.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    tmp.replace(out_path)
    print()
    print(f"Summary JSON: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
