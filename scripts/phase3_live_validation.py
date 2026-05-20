"""Phase 3 live validation against the connected device.

Procedure:
1. Capture one reference frame with the Sensor (default raw mode).
2. Build three Templates cropped from that frame:
   - easy: a corner of the status bar / top-of-screen (low-detail).
   - medium: an interior 110x110 patch around (cx, cy).
   - intentional miss: a randomized texture that does not occur on the device.
3. For each template, run Matcher in both ROI-restricted and full-frame modes.
4. Print confidence + latency per case. Compare against frozen NFRs.

This script is throwaway — it is run once for the Phase 3 report and is
not part of the test suite.
"""
from __future__ import annotations

import statistics
import sys

import cv2
import numpy as np

from automation.adb import ADB
from automation.frame import Frame
from automation.matcher import Matcher
from automation.sensor import Sensor
from automation.template import Template


def bench(matcher: Matcher, frame: Frame, template: Template, iters: int = 20) -> dict:
    latencies = []
    confs = []
    found_flags = []
    for _ in range(iters):
        r = matcher.match(frame, template)
        latencies.append(r.match_latency_ms)
        confs.append(r.confidence)
        found_flags.append(r.found)
    latencies.sort()
    return {
        "n": iters,
        "found_rate": sum(found_flags) / iters,
        "mean_ms": statistics.fmean(latencies),
        "median_ms": statistics.median(latencies),
        "p95_ms": latencies[int(0.95 * (iters - 1))],
        "min_ms": latencies[0],
        "max_ms": latencies[-1],
        "mean_confidence": statistics.fmean(confs),
        "median_confidence": statistics.median(confs),
    }


def _pick_textured_crop(image_bgr, w: int = 110, h: int = 110,
                         search_step: int = 80) -> tuple[int, int]:
    """Walk the frame on a grid and return the top-left of the most-textured
    `w`x`h` patch (measured by grayscale standard deviation)."""
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    H_, W_ = gray.shape
    best = (0, 0, -1.0)
    for y in range(0, H_ - h, search_step):
        for x in range(0, W_ - w, search_step):
            patch = gray[y:y + h, x:x + w]
            std = float(patch.std())
            if std > best[2]:
                best = (x, y, std)
    return best[0], best[1]


def main() -> int:
    adb = ADB()
    sensor = Sensor(adb, mode="raw")
    print("=== capturing reference frame ===")
    frame = sensor.capture()
    print(f"  {frame.shape_summary()}")
    W, H = frame.width, frame.height

    # Save the reference frame for inspection.
    cv2.imwrite("/tmp/phase3_live_frame.png", frame.image_bgr)

    # Build three templates by cropping from the captured frame. Use
    # textured patches so cv2.matchTemplate has discriminative signal —
    # picking the dead-centre of a frame whose content is sparse (e.g.
    # a status bar above a black workspace) makes for a degenerate test.
    matcher = Matcher()
    cases: list[tuple[str, Template]] = []

    # 1. easy: most-textured patch in the top quarter of the frame.
    top_x, top_y = _pick_textured_crop(frame.image_bgr[:H // 4])
    e_x, e_y, e_w, e_h = top_x, top_y, 110, 110
    print(f"  easy crop @ ({e_x}, {e_y})")
    easy_gray = cv2.cvtColor(frame.image_bgr[e_y:e_y + e_h, e_x:e_x + e_w], cv2.COLOR_BGR2GRAY).copy()
    easy_roi = (max(0, e_x - 50), max(0, e_y - 50),
                min(W, e_x + e_w + 50), min(H, e_y + e_h + 50))
    easy_full = Template("easy_full", easy_gray.copy(), e_w, e_h, threshold=0.9, roi=None)
    easy_roi_t = Template("easy_roi", easy_gray.copy(), e_w, e_h, threshold=0.9, roi=easy_roi)
    cases.append(("easy_full", easy_full))
    cases.append(("easy_roi", easy_roi_t))

    # 2. medium: most-textured patch in the centre half of the frame.
    mid_band = frame.image_bgr[H // 4:3 * H // 4]
    m_off_x, m_off_y = _pick_textured_crop(mid_band)
    m_x, m_y = m_off_x, H // 4 + m_off_y
    print(f"  medium crop @ ({m_x}, {m_y})")
    medium_gray = cv2.cvtColor(frame.image_bgr[m_y:m_y + 110, m_x:m_x + 110], cv2.COLOR_BGR2GRAY).copy()
    medium_roi = (max(0, m_x - 100), max(0, m_y - 100),
                  min(W, m_x + 110 + 100), min(H, m_y + 110 + 100))
    medium_full = Template("medium_full", medium_gray.copy(), 110, 110, threshold=0.9, roi=None)
    medium_roi_t = Template("medium_roi", medium_gray.copy(), 110, 110, threshold=0.9, roi=medium_roi)
    cases.append(("medium_full", medium_full))
    cases.append(("medium_roi", medium_roi_t))

    # 3. intentional miss: a random-textured 110x110 template (not on screen).
    rng = np.random.default_rng(seed=42)
    miss_gray = rng.integers(0, 256, size=(110, 110), dtype=np.uint8)
    miss_full = Template("miss_full", miss_gray.copy(), 110, 110, threshold=0.9, roi=None)
    miss_roi_t = Template("miss_roi", miss_gray.copy(), 110, 110, threshold=0.9,
                          roi=(0, 0, 500, 500))
    cases.append(("miss_full", miss_full))
    cases.append(("miss_roi", miss_roi_t))

    print()
    print("=== bench (20 iters / case) ===")
    hdr = f"{'case':<14} {'roi?':<5} {'found%':>7} {'conf med':>10} {'median ms':>10} {'p95 ms':>8}  pos"
    print(hdr)
    print("-" * len(hdr))
    for name, tpl in cases:
        # Probe once to print position
        r = matcher.match(frame, tpl)
        b = bench(matcher, frame, tpl, iters=20)
        pos = f"@({r.x},{r.y})" if r.found else "MISS"
        roi_flag = "yes" if tpl.roi else "no"
        print(f"  {name:<12} {roi_flag:<5} {int(b['found_rate'] * 100):>5}% "
              f"{b['median_confidence']:>10.3f} "
              f"{b['median_ms']:>10.2f} {b['p95_ms']:>8.2f}  {pos}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
