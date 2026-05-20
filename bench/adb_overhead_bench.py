"""Phase 0 adb overhead bench: measures the round-trip latency of
`adb shell echo hi`. This is a proxy for the unavoidable per-call cost
of subprocess + adb client + adb server + USB + adbd + shell + USB-back.

Usage:
    python -m bench.adb_overhead_bench [--iters N]

Outputs:
    bench/results/adb_overhead_bench.csv          (per-iteration timings)
    bench/results/adb_overhead_bench_summary.csv  (aggregate percentiles)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

from bench._common import (
    RESULTS_DIR,
    collect_device_info,
    collect_host_info,
    ensure_dirs,
    percentiles_ns,
    verify_device_or_die,
    write_csv_atomic,
)

DEFAULT_ITERS = 200


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 0 adb overhead bench")
    ap.add_argument("--iters", type=int, default=DEFAULT_ITERS)
    ap.add_argument("--warmup", type=int, default=3)
    args = ap.parse_args()

    ensure_dirs()
    verify_device_or_die()
    host = collect_host_info()
    dev = collect_device_info()

    print(f"# host:   {host.host_cpu} | {host.host_kernel}")
    print(f"# device: {dev.device_serial} {dev.device_model} usb={dev.usb_speed}")
    print(f"# iters:  {args.iters} warmup={args.warmup}")

    common = (dev.device_serial, dev.device_model, dev.usb_speed,
              host.host_cpu, host.host_kernel, host.bench_version)
    per_iter_rows: list[list[object]] = []

    for _ in range(args.warmup):
        try:
            subprocess.run(["adb", "shell", "echo", "hi"],
                           check=True, capture_output=True, timeout=10)
        except subprocess.SubprocessError as exc:
            print(f"WARN: warmup failed: {exc}", file=sys.stderr)

    timings: list[int] = []
    for i in range(args.iters):
        t0 = time.perf_counter_ns()
        try:
            proc = subprocess.run(["adb", "shell", "echo", "hi"],
                                  check=True, capture_output=True, timeout=10)
        except subprocess.SubprocessError as exc:
            print(f"ERROR: iter {i} failed: {exc}", file=sys.stderr)
            return 3
        t1 = time.perf_counter_ns()
        elapsed_ns = t1 - t0
        timings.append(elapsed_ns)
        ok = proc.stdout.strip() == b"hi"
        per_iter_rows.append([*common, i, elapsed_ns, int(ok)])
        if (i + 1) % 50 == 0:
            pct = percentiles_ns(timings)
            print(f"  {i + 1}/{args.iters} median_ms={pct['median_ms']:.1f}")

    pct = percentiles_ns(timings)
    summary_row = [
        *common, args.iters, args.warmup,
        f"{pct['mean_ms']:.3f}", f"{pct['median_ms']:.3f}",
        f"{pct['p95_ms']:.3f}", f"{pct['p99_ms']:.3f}",
        f"{pct['stdev_ms']:.3f}", f"{pct['min_ms']:.3f}", f"{pct['max_ms']:.3f}",
    ]

    header_iter = [
        "device_serial", "device_model", "usb_speed",
        "host_cpu", "host_kernel", "bench_version",
        "iter", "elapsed_ns", "echo_ok",
    ]
    header_summary = [
        "device_serial", "device_model", "usb_speed",
        "host_cpu", "host_kernel", "bench_version",
        "iters", "warmup",
        "mean_ms", "median_ms", "p95_ms", "p99_ms", "stdev_ms", "min_ms", "max_ms",
    ]
    write_csv_atomic(RESULTS_DIR / "adb_overhead_bench.csv", header_iter, per_iter_rows)
    write_csv_atomic(RESULTS_DIR / "adb_overhead_bench_summary.csv", header_summary, [summary_row])

    print()
    print(f"  mean_ms   = {pct['mean_ms']:.3f}")
    print(f"  median_ms = {pct['median_ms']:.3f}")
    print(f"  p95_ms    = {pct['p95_ms']:.3f}")
    print(f"  p99_ms    = {pct['p99_ms']:.3f}")
    print(f"  stdev_ms  = {pct['stdev_ms']:.3f}")
    print(f"  min_ms    = {pct['min_ms']:.3f}")
    print(f"  max_ms    = {pct['max_ms']:.3f}")
    print(f"wrote {RESULTS_DIR / 'adb_overhead_bench.csv'}")
    print(f"wrote {RESULTS_DIR / 'adb_overhead_bench_summary.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
