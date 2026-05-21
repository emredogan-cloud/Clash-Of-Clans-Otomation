#!/usr/bin/env python3
"""CoC template capture — interactive helper for the operator.

The CoC bot needs 6 hand-curated grayscale PNGs under
`templates/`. This script captures the current device screen and
saves it under `var/artifacts/coc_capture/<ts>_<label>.png` so
the operator can crop the templates by hand in any image editor.

Workflow:

1. Put the device in the state for which you need a template.
   e.g., to capture `home_attack_button`, be on the village home
   screen with the Attack button visible.
2. Run: `python -m scripts.coc_template_capture home`
   The `<label>` is a free-form tag for your own bookkeeping.
3. The script prints the file path of the saved PNG and the
   current status of all 6 expected templates
   (✓ present / ✗ missing).
4. Open the PNG in an image editor. Crop a tight rectangle
   around the UI element of interest. Convert to grayscale and
   save as `templates/<expected-name>.png`. Recommended crop
   size: 64×64 to 192×192. Avoid solid-color regions — the
   matcher needs structural content.
5. Re-run with the next label until all 6 templates are
   present.

CLI only — no GUI. The script does not auto-detect or
auto-crop; templates are operator-curated.

Notes:

- The reference resolution is 1080×1920. The saved PNG is at
  reference resolution (after the Sensor's Remap step).
- Coordinates printed by the bot's deployment pattern
  (`DEPLOY_TAP_SEQUENCE_REF`) are reference-space; you can
  re-tune them by editing `coc/bot.py:DEPLOY_TAP_SEQUENCE_REF`.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

# Make the project root importable when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2

from automation.adb import ADB
from automation.sensor import Sensor
from coc.templates import (
    DEFAULT_TEMPLATE_DIR,
    TEMPLATE_SPECS,
    required_filenames,
)

CAPTURE_DIR: Path = Path("var/artifacts/coc_capture")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="coc_template_capture",
        description=(
            "Capture the device screen and save as a PNG so the "
            "operator can crop CoC bot templates by hand."
        ),
    )
    parser.add_argument(
        "label", nargs="?", default="screen",
        help="freeform label for the capture filename "
             "(e.g., 'home', 'attack', 'battle'). Default: 'screen'.",
    )
    args = parser.parse_args(argv)

    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)

    adb = ADB()
    sensor = Sensor(adb, mode="raw")

    print("Capturing device screen…")
    frame = sensor.capture()
    print(
        f"  reference size:   {frame.width}x{frame.height}\n"
        f"  native size:      {frame.native_width}x{frame.native_height}\n"
        f"  capture latency:  {frame.capture_latency_ms:.1f} ms"
    )

    ts = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    out_path = CAPTURE_DIR / f"{ts}_{args.label}.png"
    ok = cv2.imwrite(str(out_path), frame.image_bgr)
    if not ok:
        print(f"\n[ERROR] cv2.imwrite failed: {out_path}", file=sys.stderr)
        return 1
    print(f"  saved:            {out_path}")

    print()
    print("Next steps:")
    print("  1. Open the PNG above in an image editor (GIMP, krita, etc.).")
    print("  2. Crop a tight rectangle around the UI element of interest.")
    print("     Recommended size: 64×64 to 192×192 px (reference space).")
    print("     Avoid solid-color regions — the matcher needs structure.")
    print("  3. Convert to grayscale (Image → Mode → Grayscale).")
    print(f"  4. Save as PNG to `{DEFAULT_TEMPLATE_DIR}/<filename>.png`, where")
    print("     <filename> is one of the following:")
    print()
    print(f"     {'present':>7}  {'filename':<28}  purpose")
    print(f"     {'-------':>7}  {'-' * 28}  {'-' * 50}")
    for spec in TEMPLATE_SPECS:
        path = DEFAULT_TEMPLATE_DIR / spec.filename
        flag = "✓" if path.is_file() else "✗"
        print(f"     {flag:>7}  {spec.filename:<28}  {spec.purpose}")
    print()
    n_have = sum(
        1 for f in required_filenames()
        if (DEFAULT_TEMPLATE_DIR / f).is_file()
    )
    print(f"  Pack status: {n_have}/{len(TEMPLATE_SPECS)} templates present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
