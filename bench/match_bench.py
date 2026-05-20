"""Phase 0 match bench: measures cv2.matchTemplate latency under the four
combinations referenced by ADR-03/05:

  - full-frame BGR
  - full-frame grayscale
  - ROI-restricted BGR
  - ROI-restricted grayscale

A single representative frame is captured once. A seed template is cropped
from it. Each variant runs >= 500 iterations.

Usage:
    python -m bench.match_bench [--iters N]

Outputs:
    bench/results/match_bench.csv           (per-iteration timings)
    bench/results/match_bench_summary.csv   (aggregate percentiles)
    bench/artifacts/match_frame.png         (the captured frame)
    bench/artifacts/match_template.png      (the cropped template)
"""
from __future__ import annotations

import argparse
import struct
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from bench._common import (
    ARTIFACTS_DIR,
    RESULTS_DIR,
    collect_device_info,
    collect_host_info,
    ensure_dirs,
    percentiles_ns,
    verify_device_or_die,
    write_csv_atomic,
)

DEFAULT_ITERS = 500

# Reference resolution baseline per ADR-04. We resample the captured frame to
# this size before benching, so the numbers are comparable across devices.
REF_W, REF_H = 1080, 1920

# Template size in pixels at the reference resolution. Chosen to match a
# realistic icon-sized template (~10% of width).
TPL_W, TPL_H = 110, 110

# ROI dimensions used for the "ROI restricted" variants. A 1/4 area ROI is
# a realistic operator choice (e.g. a button band).
ROI_W, ROI_H = 540, 480


def _capture_one_frame() -> np.ndarray:
    """Capture one frame via exec-out screencap (raw)."""
    proc = subprocess.run(
        ["adb", "exec-out", "screencap"],
        check=True, capture_output=True, timeout=30,
    )
    buf = proc.stdout
    if len(buf) < 16:
        raise RuntimeError(f"raw buffer too small: {len(buf)}")
    w, h, _fmt, _cs = struct.unpack_from("<IIII", buf, 0)
    expected_16 = 16 + w * h * 4
    expected_12 = 12 + w * h * 4
    if len(buf) == expected_16:
        offset = 16
    elif len(buf) == expected_12:
        w, h, _fmt = struct.unpack_from("<III", buf, 0)
        offset = 12
    else:
        raise RuntimeError(f"unknown raw layout: len={len(buf)} w={w} h={h}")
    rgba = np.frombuffer(buf, dtype=np.uint8, count=w * h * 4, offset=offset).reshape(h, w, 4)
    return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)


def _resample_to_ref(frame: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    if (w, h) == (REF_W, REF_H):
        return frame
    # Keep aspect by letterbox: resize keeping width, pad/crop height.
    # For the bench we simply resize directly to REF_W x REF_H — distortion
    # is acceptable because we are timing matchTemplate, not measuring
    # detection accuracy.
    interp = cv2.INTER_AREA if (w > REF_W or h > REF_H) else cv2.INTER_LINEAR
    return cv2.resize(frame, (REF_W, REF_H), interpolation=interp)


def _make_template(frame: np.ndarray) -> tuple[np.ndarray, tuple[int, int]]:
    """Crop a template from a busy region of the frame.

    Picks a region near the centre that is unlikely to be entirely uniform
    (status bars / pure black backgrounds can defeat normalized correlation,
    which is irrelevant for the timing measurement but worth avoiding).
    """
    cx = REF_W // 2
    cy = REF_H // 2
    x0 = max(0, cx - TPL_W // 2)
    y0 = max(0, cy - TPL_H // 2)
    tpl = frame[y0:y0 + TPL_H, x0:x0 + TPL_W].copy()
    return tpl, (x0, y0)


def _make_roi(frame: np.ndarray, anchor: tuple[int, int]) -> np.ndarray:
    """Crop a ROI around the template anchor.

    The anchor (top-left of the template within the reference frame) is
    centered inside the ROI when possible.
    """
    ax, ay = anchor
    rx0 = max(0, min(REF_W - ROI_W, ax + TPL_W // 2 - ROI_W // 2))
    ry0 = max(0, min(REF_H - ROI_H, ay + TPL_H // 2 - ROI_H // 2))
    return frame[ry0:ry0 + ROI_H, rx0:rx0 + ROI_W].copy()


def _bench_variant(image: np.ndarray, template: np.ndarray, iters: int) -> list[int]:
    timings: list[int] = []
    for _ in range(iters):
        t0 = time.perf_counter_ns()
        cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
        t1 = time.perf_counter_ns()
        timings.append(t1 - t0)
    return timings


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 0 match bench")
    ap.add_argument("--iters", type=int, default=DEFAULT_ITERS)
    ap.add_argument("--warmup", type=int, default=5)
    args = ap.parse_args()

    ensure_dirs()
    verify_device_or_die()
    host = collect_host_info()
    dev = collect_device_info()

    print(f"# host:   {host.host_cpu} | {host.host_kernel}")
    print(f"# device: {dev.device_serial} {dev.device_model} usb={dev.usb_speed}")

    frame_native = _capture_one_frame()
    h_native, w_native = frame_native.shape[:2]
    print(f"# captured native frame: {w_native}x{h_native}")
    frame_bgr = _resample_to_ref(frame_native)
    print(f"# resampled to reference: {REF_W}x{REF_H}")
    template_bgr, anchor = _make_template(frame_bgr)
    template_gray = cv2.cvtColor(template_bgr, cv2.COLOR_BGR2GRAY)
    frame_gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    roi_bgr = _make_roi(frame_bgr, anchor)
    roi_gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)

    # Save artifacts so the bench is reproducible from disk.
    cv2.imwrite(str(ARTIFACTS_DIR / "match_frame.png"), frame_bgr)
    cv2.imwrite(str(ARTIFACTS_DIR / "match_template.png"), template_bgr)
    cv2.imwrite(str(ARTIFACTS_DIR / "match_roi.png"), roi_bgr)

    variants = [
        ("full_frame_bgr", frame_bgr, template_bgr),
        ("full_frame_gray", frame_gray, template_gray),
        ("roi_bgr", roi_bgr, template_bgr),
        ("roi_gray", roi_gray, template_gray),
    ]

    common = (dev.device_serial, dev.device_model, dev.usb_speed,
              host.host_cpu, host.host_kernel, host.bench_version)
    per_iter_rows: list[list[object]] = []
    summary_rows: list[list[object]] = []

    for name, image, tpl in variants:
        # Sanity: ensure template smaller than image (else matchTemplate
        # raises). All four variants are valid here by construction.
        if tpl.shape[0] > image.shape[0] or tpl.shape[1] > image.shape[1]:
            print(f"ERROR: template larger than image for variant {name}", file=sys.stderr)
            return 4
        # Warmup
        for _ in range(args.warmup):
            cv2.matchTemplate(image, tpl, cv2.TM_CCOEFF_NORMED)
        timings = _bench_variant(image, tpl, args.iters)
        for i, ns in enumerate(timings):
            per_iter_rows.append([
                *common, name, i, ns,
                image.shape[1], image.shape[0],
                (image.shape[2] if image.ndim == 3 else 1),
                tpl.shape[1], tpl.shape[0],
            ])
        pct = percentiles_ns(timings)
        summary_rows.append([
            *common, name, args.iters, args.warmup,
            image.shape[1], image.shape[0],
            (image.shape[2] if image.ndim == 3 else 1),
            tpl.shape[1], tpl.shape[0],
            f"{pct['mean_ms']:.4f}", f"{pct['median_ms']:.4f}",
            f"{pct['p95_ms']:.4f}", f"{pct['p99_ms']:.4f}",
            f"{pct['stdev_ms']:.4f}", f"{pct['min_ms']:.4f}", f"{pct['max_ms']:.4f}",
        ])
        print(f"  {name:<20} mean_ms={pct['mean_ms']:.3f}  median_ms={pct['median_ms']:.3f}  "
              f"p95={pct['p95_ms']:.3f}  p99={pct['p99_ms']:.3f}")

    header_iter = [
        "device_serial", "device_model", "usb_speed",
        "host_cpu", "host_kernel", "bench_version",
        "variant", "iter", "elapsed_ns",
        "image_w", "image_h", "image_channels",
        "template_w", "template_h",
    ]
    header_summary = [
        "device_serial", "device_model", "usb_speed",
        "host_cpu", "host_kernel", "bench_version",
        "variant", "iters", "warmup",
        "image_w", "image_h", "image_channels",
        "template_w", "template_h",
        "mean_ms", "median_ms", "p95_ms", "p99_ms", "stdev_ms", "min_ms", "max_ms",
    ]
    write_csv_atomic(RESULTS_DIR / "match_bench.csv", header_iter, per_iter_rows)
    write_csv_atomic(RESULTS_DIR / "match_bench_summary.csv", header_summary, summary_rows)
    print(f"wrote {RESULTS_DIR / 'match_bench.csv'}")
    print(f"wrote {RESULTS_DIR / 'match_bench_summary.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
