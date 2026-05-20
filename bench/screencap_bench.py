"""Phase 0 screencap bench: measures end-to-end latency from request to a
fully-decoded NumPy ndarray for three modes:

  A. screencap + pull       (adb shell screencap /sdcard/x.png; adb pull)
  B. exec-out PNG           (adb exec-out screencap -p)
  C. exec-out raw           (adb exec-out screencap)

Optional D mode (minicap) is intentionally not implemented in v0 because the
operator does not have a vetted minicap binary available; if a future
operator wants to measure minicap, they can add a fourth mode here.

Usage:
    python -m bench.screencap_bench [--iters N] [--modes A,B,C] [--out PATH]

Outputs:
    bench/results/screencap_bench.csv         (per-iteration timings)
    bench/results/screencap_bench_summary.csv (aggregate percentiles)
"""
from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import time
import uuid
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

DEFAULT_ITERS = 200


def _capture_mode_pull() -> tuple[int, np.ndarray]:
    """A. screencap to /sdcard + adb pull."""
    remote = f"/sdcard/_bench_{uuid.uuid4().hex}.png"
    local = ARTIFACTS_DIR / f"_pull_{uuid.uuid4().hex}.png"
    t0 = time.perf_counter_ns()
    subprocess.run(["adb", "shell", "screencap", "-p", remote],
                   check=True, capture_output=True, timeout=15)
    subprocess.run(["adb", "pull", remote, str(local)],
                   check=True, capture_output=True, timeout=15)
    data = local.read_bytes()
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    t1 = time.perf_counter_ns()
    try:
        local.unlink()
    except OSError:
        pass
    try:
        subprocess.run(["adb", "shell", "rm", remote],
                       check=False, capture_output=True, timeout=5)
    except subprocess.SubprocessError:
        pass
    if img is None:
        raise RuntimeError("imdecode returned None on screencap+pull frame")
    return t1 - t0, img


def _capture_mode_png() -> tuple[int, np.ndarray]:
    """B. adb exec-out screencap -p (PNG over stdout)."""
    t0 = time.perf_counter_ns()
    proc = subprocess.run(
        ["adb", "exec-out", "screencap", "-p"],
        check=True, capture_output=True, timeout=15,
    )
    img = cv2.imdecode(np.frombuffer(proc.stdout, dtype=np.uint8), cv2.IMREAD_COLOR)
    t1 = time.perf_counter_ns()
    if img is None:
        raise RuntimeError("imdecode returned None on PNG frame")
    return t1 - t0, img


def _parse_raw_screencap(buf: bytes) -> np.ndarray:
    """Parse the documented raw framebuffer header.

    Documented layout (Android 9+):
        uint32 width
        uint32 height
        uint32 pixel_format
        uint32 colorspace
        bytes  pixels (W * H * 4 for RGBA_8888)

    Pre-Android-9 builds may omit the colorspace word (12-byte header).
    This bench tries 16-byte first and falls back to 12-byte.
    """
    if len(buf) < 16:
        raise RuntimeError(f"raw buffer too small: {len(buf)} bytes")
    w, h, fmt, _cs = struct.unpack_from("<IIII", buf, 0)
    expected_16 = 16 + w * h * 4
    expected_12 = 12 + w * h * 4
    if len(buf) == expected_16 and 0 < w <= 8192 and 0 < h <= 8192:
        header_size = 16
    elif len(buf) == expected_12 and 0 < w <= 8192 and 0 < h <= 8192:
        # 12-byte header (pre-Android-9 layout)
        w, h, fmt = struct.unpack_from("<III", buf, 0)
        header_size = 12
    else:
        # Try inferring layout when device pads or the format is unfamiliar.
        # Prefer 16-byte if w/h look sensible; otherwise raise.
        if 0 < w <= 8192 and 0 < h <= 8192:
            header_size = 16
        else:
            raise RuntimeError(
                f"unknown raw screencap layout: len={len(buf)} w={w} h={h} fmt={fmt}"
            )
    pixels = np.frombuffer(buf, dtype=np.uint8, count=w * h * 4, offset=header_size)
    rgba = pixels.reshape(h, w, 4)
    bgr = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
    return bgr


def _capture_mode_raw() -> tuple[int, np.ndarray]:
    """C. adb exec-out screencap (raw bytes over stdout)."""
    t0 = time.perf_counter_ns()
    proc = subprocess.run(
        ["adb", "exec-out", "screencap"],
        check=True, capture_output=True, timeout=30,
    )
    img = _parse_raw_screencap(proc.stdout)
    t1 = time.perf_counter_ns()
    return t1 - t0, img


MODE_FUNCS = {
    "A_screencap_pull": _capture_mode_pull,
    "B_exec_out_png": _capture_mode_png,
    "C_exec_out_raw": _capture_mode_raw,
}


def parse_modes(arg: str) -> list[str]:
    out = []
    mapping = {"A": "A_screencap_pull", "B": "B_exec_out_png", "C": "C_exec_out_raw"}
    for token in arg.split(","):
        token = token.strip().upper()
        if not token:
            continue
        if token in mapping:
            out.append(mapping[token])
        else:
            raise SystemExit(f"unknown mode: {token!r} (use comma-separated A,B,C)")
    return out or list(MODE_FUNCS.keys())


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 0 screencap bench")
    ap.add_argument("--iters", type=int, default=DEFAULT_ITERS,
                    help=f"iterations per mode (default {DEFAULT_ITERS})")
    ap.add_argument("--modes", default="A,B,C",
                    help="comma-separated subset of A/B/C")
    ap.add_argument("--warmup", type=int, default=3,
                    help="warmup iterations per mode (discarded)")
    args = ap.parse_args()

    ensure_dirs()
    verify_device_or_die()
    host = collect_host_info()
    dev = collect_device_info()
    modes = parse_modes(args.modes)

    print(f"# host:   {host.host_cpu} | {host.host_kernel}")
    print(f"# device: {dev.device_serial} {dev.device_model} usb={dev.usb_speed}")
    print(f"# modes:  {modes} iters={args.iters} warmup={args.warmup}")

    per_iter_rows: list[list[object]] = []
    summary_rows: list[list[object]] = []

    common = (dev.device_serial, dev.device_model, dev.usb_speed,
              host.host_cpu, host.host_kernel, host.bench_version)

    for mode in modes:
        func = MODE_FUNCS[mode]
        # warmup
        for _ in range(args.warmup):
            try:
                _ = func()
            except Exception as exc:
                print(f"WARN: warmup for {mode} failed: {exc}", file=sys.stderr)
        timings: list[int] = []
        last_shape = (0, 0, 0)
        failures = 0
        for i in range(args.iters):
            try:
                elapsed_ns, img = func()
            except Exception as exc:
                failures += 1
                print(f"ERROR: iter {i} mode={mode}: {exc}", file=sys.stderr)
                # Abort the bench non-zero — Phase 0 prohibits partial CSVs.
                return 3
            timings.append(elapsed_ns)
            last_shape = img.shape
            per_iter_rows.append([
                *common, mode, i, elapsed_ns,
                last_shape[1], last_shape[0], last_shape[2],
            ])
            if (i + 1) % 25 == 0:
                print(f"  {mode} {i + 1}/{args.iters} median_ms="
                      f"{percentiles_ns(timings)['median_ms']:.1f}")
        pct = percentiles_ns(timings)
        summary_rows.append([
            *common, mode, args.iters, args.warmup,
            last_shape[1], last_shape[0], last_shape[2],
            f"{pct['mean_ms']:.3f}", f"{pct['median_ms']:.3f}",
            f"{pct['p95_ms']:.3f}", f"{pct['p99_ms']:.3f}",
            f"{pct['stdev_ms']:.3f}", f"{pct['min_ms']:.3f}", f"{pct['max_ms']:.3f}",
            failures,
        ])

    header_iter = [
        "device_serial", "device_model", "usb_speed",
        "host_cpu", "host_kernel", "bench_version",
        "mode", "iter", "elapsed_ns", "frame_w", "frame_h", "frame_channels",
    ]
    header_summary = [
        "device_serial", "device_model", "usb_speed",
        "host_cpu", "host_kernel", "bench_version",
        "mode", "iters", "warmup",
        "frame_w", "frame_h", "frame_channels",
        "mean_ms", "median_ms", "p95_ms", "p99_ms", "stdev_ms", "min_ms", "max_ms",
        "failures",
    ]
    write_csv_atomic(RESULTS_DIR / "screencap_bench.csv", header_iter, per_iter_rows)
    write_csv_atomic(RESULTS_DIR / "screencap_bench_summary.csv", header_summary, summary_rows)

    print()
    print("Mode                       count   mean    median   p95     p99     stdev   min     max")
    for row in summary_rows:
        mode = row[6]
        iters = row[7]
        mean_ms, median_ms, p95_ms, p99_ms, stdev_ms, min_ms, max_ms = row[13:20]
        print(f"  {mode:<25} {iters:<5}  "
              f"{mean_ms:>6}  {median_ms:>6}  {p95_ms:>6}  {p99_ms:>6}  "
              f"{stdev_ms:>6}  {min_ms:>6}  {max_ms:>6}  (ms)")
    print()
    print(f"wrote {RESULTS_DIR / 'screencap_bench.csv'}")
    print(f"wrote {RESULTS_DIR / 'screencap_bench_summary.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
