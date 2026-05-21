#!/usr/bin/env python3
"""CoC live validation — one full trophy-drop loop on the real device.

Prerequisites:

- ADB sees the device (`adb devices` lists it as `device`).
- Clash of Clans is installed on the device
  (`com.supercell.clashofclans`).
- The template pack under `templates/` is complete. The
  operator captures + crops via
  `scripts/coc_template_capture.py`.

This script:

1. Loads the template pack. If incomplete → reports exactly
   which templates are missing and exits non-zero. **No fake
   success.**
2. Builds Sensor + Matcher + Actuator + ADB.
3. Constructs a `CoCTrophyBot` and calls `bot.run_once()`.
4. Persists the result to
   `bench/results/coc_live_validation.json` and prints a
   honest summary (visited states, failure_reason if any).

Usage:
    .venv/bin/python -m scripts.coc_live_validation
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

# Make the project root importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from automation.actuator import Actuator
from automation.adb import ADB
from automation.denormalize import Denormalizer
from automation.matcher import Matcher
from automation.sensor import Sensor
from coc.bot import CoCTrophyBot
from coc.templates import (
    DEFAULT_TEMPLATE_DIR,
    TemplatePackError,
    load_template_pack,
    required_filenames,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


SIDE_CAR_PATH = Path("bench/results/coc_live_validation.json")


def main() -> int:
    # ----- preflight: device + template pack ----------------------------
    adb = ADB()
    try:
        state = adb.get_state().strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] adb get-state failed: {exc}", file=sys.stderr)
        return 2
    if state != "device":
        print(
            f"[FAIL] device not ready: adb get-state = {state!r}",
            file=sys.stderr,
        )
        return 2

    try:
        pack = load_template_pack(DEFAULT_TEMPLATE_DIR)
    except TemplatePackError as exc:
        print("[FAIL] template pack incomplete:", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        print(file=sys.stderr)
        print("Required filenames under templates/:", file=sys.stderr)
        for fn in required_filenames():
            present = "✓" if (DEFAULT_TEMPLATE_DIR / fn).is_file() else "✗"
            print(f"  {present} {fn}", file=sys.stderr)
        print(
            "\nRun `python -m scripts.coc_template_capture <label>` to "
            "capture screens, crop the targets by hand, save as the named "
            f"PNGs under {DEFAULT_TEMPLATE_DIR.resolve()}/, and re-run.",
            file=sys.stderr,
        )
        # Persist a sidecar describing the block so post-mortems are easy.
        SIDE_CAR_PATH.parent.mkdir(parents=True, exist_ok=True)
        SIDE_CAR_PATH.write_text(json.dumps({
            "success": False,
            "blocked_by": "template_pack_incomplete",
            "error": str(exc),
            "required_filenames": required_filenames(),
            "present_filenames": [
                fn for fn in required_filenames()
                if (DEFAULT_TEMPLATE_DIR / fn).is_file()
            ],
        }, indent=2, sort_keys=True) + "\n")
        return 1

    print(
        f"loaded {len(pack)} templates: "
        + ", ".join(pack.names())
    )

    # ----- build framework + bot ----------------------------------------
    sensor = Sensor(adb, mode="raw")
    matcher = Matcher()
    actuator = Actuator(
        adb, denormalizer=Denormalizer((1080, 1920)), seed=20260521,
    )
    bot = CoCTrophyBot(
        sensor=sensor,
        matcher=matcher,
        actuator=actuator,
        templates=pack,
        adb=adb,
    )

    # ----- run one loop -------------------------------------------------
    print()
    print("=" * 72)
    print("Running one trophy-drop loop…")
    print("=" * 72)
    result = bot.run_once()
    print()
    print(result.summary())
    if not result.success:
        print(f"\nblocked at: {result.failure_step}")
        print(f"reason:     {result.failure_reason}")

    # ----- persist sidecar ----------------------------------------------
    SIDE_CAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    SIDE_CAR_PATH.write_text(
        json.dumps(result.to_debug_dict(), indent=2, sort_keys=True) + "\n"
    )
    print(f"\nsidecar: {SIDE_CAR_PATH}")
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
