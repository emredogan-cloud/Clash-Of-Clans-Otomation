"""Shared helpers for Phase 0 bench harnesses.

This is throwaway code intended only for Phase 0 measurement. Production
ADB wrapping is Phase 1's responsibility (ADR-07, ADR-11).
"""
from __future__ import annotations

import csv
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

BENCH_VERSION = "phase0-2026-05-20"

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "bench" / "results"
ARTIFACTS_DIR = REPO_ROOT / "bench" / "artifacts"


@dataclass(frozen=True)
class HostInfo:
    host_cpu: str
    host_kernel: str
    bench_version: str


@dataclass(frozen=True)
class DeviceInfo:
    device_serial: str
    device_model: str
    usb_speed: str  # e.g. "12 Mbps", "480 Mbps", "unknown"


def _run(cmd: Sequence[str], timeout: float = 10.0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)


def _read_first_line(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.readline().decode("ascii", errors="replace").strip()
    except OSError:
        return ""


def collect_host_info() -> HostInfo:
    cpu = "unknown"
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    kernel = platform.release()
    return HostInfo(host_cpu=cpu, host_kernel=kernel, bench_version=BENCH_VERSION)


def detect_usb_speed_for_serial(serial: str) -> str:
    """Best-effort lookup: walks /sys/bus/usb/devices/* for a device whose
    serial sysfs attribute matches the adb serial."""
    base = "/sys/bus/usb/devices"
    if not os.path.isdir(base):
        return "unknown"
    for entry in sorted(os.listdir(base)):
        dev_path = os.path.join(base, entry)
        sernum_path = os.path.join(dev_path, "serial")
        if not os.path.isfile(sernum_path):
            continue
        sysfs_serial = _read_first_line(sernum_path)
        if sysfs_serial and sysfs_serial == serial:
            speed = _read_first_line(os.path.join(dev_path, "speed"))
            if speed:
                return f"{speed} Mbps"
            return "unknown"
    return "unknown"


def collect_device_info() -> DeviceInfo:
    serial = _adb_one(["adb", "get-serialno"]).strip()
    model = _adb_one(["adb", "shell", "getprop", "ro.product.model"]).strip()
    usb_speed = detect_usb_speed_for_serial(serial) if serial else "unknown"
    return DeviceInfo(device_serial=serial or "unknown", device_model=model or "unknown", usb_speed=usb_speed)


def _adb_one(cmd: Sequence[str], timeout: float = 10.0) -> str:
    try:
        out = subprocess.check_output(cmd, timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return ""
    return out.decode("utf-8", errors="replace")


def verify_device_or_die() -> None:
    out = _adb_one(["adb", "get-state"])
    if "device" not in out.strip():
        print(f"ERROR: adb get-state did not return 'device' (got: {out!r}). "
              "Connect a device with USB debugging authorized.", file=sys.stderr)
        sys.exit(2)


def percentiles_ns(samples: Iterable[int]) -> dict[str, float]:
    arr = sorted(samples)
    if not arr:
        return {"count": 0, "mean_ms": 0.0, "median_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0,
                "stdev_ms": 0.0, "min_ms": 0.0, "max_ms": 0.0}
    n = len(arr)
    mean = statistics.fmean(arr)
    median = statistics.median(arr)
    stdev = statistics.pstdev(arr) if n > 1 else 0.0

    def _pct(p: float) -> float:
        # nearest-rank
        k = max(0, min(n - 1, int(round(p * (n - 1)))))
        return float(arr[k])

    p95 = _pct(0.95)
    p99 = _pct(0.99)
    return {
        "count": n,
        "mean_ms": mean / 1e6,
        "median_ms": median / 1e6,
        "p95_ms": p95 / 1e6,
        "p99_ms": p99 / 1e6,
        "stdev_ms": stdev / 1e6,
        "min_ms": arr[0] / 1e6,
        "max_ms": arr[-1] / 1e6,
    }


def write_csv_atomic(path: Path, header: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            for row in rows:
                w.writerow(row)
            f.flush()
            os.fsync(f.fileno())
        shutil.move(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def ensure_dirs() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
