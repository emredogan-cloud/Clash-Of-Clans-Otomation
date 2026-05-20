"""Device fingerprinting: read identifying properties off the connected device.

Phase 1 surface: produce a `DeviceFingerprint` dataclass that downstream
phases (Sensor calibration in Phase 2, the orchestrator's CALIBRATING
state in Phase 5) will consume. No persistence; the caller may write
the result to a file if desired.

USB-link-speed resolution walks `/sys/bus/usb/devices/*` and matches the
device's adb serial against each entry's `serial` sysfs attribute. This
is best-effort: not all environments expose the `speed` file (e.g.
some containerized hosts), and a return of `None` is not by itself a
failure.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .adb import ADB
from .errors import ADBError

_LOG = logging.getLogger(__name__)

USB_DEVICES_PATH: str = "/sys/bus/usb/devices"

# `dumpsys window` exposes the display in a line like:
#   init=1080x2408 440dpi mMinSizeOfResizeableTaskDp=200 cur=1080x2408 app=1080x2202 ...
# We prefer the `cur=` value (current logical display) over `init=`, falling
# back to `init=` when `cur=` is absent.
_DUMPSYS_CUR_RE = re.compile(r"\bcur=(\d+)x(\d+)\b")
_DUMPSYS_INIT_RE = re.compile(r"\binit=(\d+)x(\d+)\b")


@dataclass(frozen=True)
class DeviceFingerprint:
    """Identifying properties of the connected device.

    `usb_speed_mbps` is `None` if the sysfs path could not be resolved.
    All other fields are required; absence is a fingerprint failure.
    """

    serial: str
    manufacturer: str
    model: str
    android_version: str  # e.g. "13"
    sdk: int              # API level, e.g. 33
    resolution: tuple[int, int]  # (W, H) in native pixels
    usb_speed_mbps: int | None
    adb_version: tuple[int, int, int]

    def to_human_summary(self) -> str:
        """Multi-line human-readable summary suitable for the bootstrap CLI."""
        w, h = self.resolution
        usb = (
            f"{self.usb_speed_mbps} Mbps"
            if self.usb_speed_mbps is not None
            else "unknown (sysfs unreadable)"
        )
        adb_v = ".".join(str(x) for x in self.adb_version)
        return (
            f"  serial:           {self.serial}\n"
            f"  manufacturer:     {self.manufacturer}\n"
            f"  model:            {self.model}\n"
            f"  android_version:  {self.android_version}\n"
            f"  sdk:              {self.sdk}\n"
            f"  resolution:       {w}x{h}\n"
            f"  usb_speed:        {usb}\n"
            f"  adb_version:      {adb_v}"
        )


def fingerprint(adb: ADB, *, usb_devices_path: str | None = None) -> DeviceFingerprint:
    """Probe the connected device and return its `DeviceFingerprint`.

    Raises `ADBError` if any required property cannot be read. The USB
    speed lookup is best-effort (returns `None` on failure rather than
    raising) because not all hosts expose the relevant sysfs files.
    """
    serial = adb.get_serialno().strip()
    if not serial:
        raise ADBError("adb get-serialno returned empty")

    manufacturer = adb.shell(["getprop", "ro.product.manufacturer"]).strip()
    model = adb.shell(["getprop", "ro.product.model"]).strip()
    android_version = adb.shell(["getprop", "ro.build.version.release"]).strip()
    sdk_raw = adb.shell(["getprop", "ro.build.version.sdk"]).strip()
    if not (manufacturer and model and android_version and sdk_raw):
        raise ADBError(
            f"missing device properties: manufacturer={manufacturer!r} "
            f"model={model!r} android={android_version!r} sdk={sdk_raw!r}"
        )
    try:
        sdk = int(sdk_raw)
    except ValueError as exc:
        raise ADBError(f"could not parse sdk level: {sdk_raw!r}") from exc

    resolution = _read_resolution(adb)
    usb_speed = find_usb_speed(serial, base=usb_devices_path or USB_DEVICES_PATH)
    adb_version = adb.adb_version()

    fp = DeviceFingerprint(
        serial=serial,
        manufacturer=manufacturer,
        model=model,
        android_version=android_version,
        sdk=sdk,
        resolution=resolution,
        usb_speed_mbps=usb_speed,
        adb_version=adb_version,
    )
    _LOG.info(
        "fingerprinted device %s (%s %s, Android %s, %dx%d, USB %s)",
        fp.serial, fp.manufacturer, fp.model, fp.android_version,
        fp.resolution[0], fp.resolution[1],
        f"{fp.usb_speed_mbps} Mbps" if fp.usb_speed_mbps is not None else "?",
    )
    return fp


def _read_resolution(adb: ADB) -> tuple[int, int]:
    """Parse the device display resolution from `dumpsys window`."""
    dump = adb.shell(["dumpsys", "window"])
    cur = _DUMPSYS_CUR_RE.search(dump)
    if cur is not None:
        return int(cur.group(1)), int(cur.group(2))
    init = _DUMPSYS_INIT_RE.search(dump)
    if init is not None:
        return int(init.group(1)), int(init.group(2))
    raise ADBError("could not parse display resolution from `dumpsys window`")


def find_usb_speed(serial: str, *, base: str = USB_DEVICES_PATH) -> int | None:
    """Resolve the USB link speed (in Mbps) for the device with `serial`.

    Walks `/sys/bus/usb/devices/*`, matches each `serial` attribute
    against the adb serial, then reads `speed`. Returns `None` if no
    match is found or if `speed` is unreadable.

    The `base` parameter is provided for test injection of a fake
    sysfs tree.
    """
    if not os.path.isdir(base):
        _LOG.debug("usb devices base %r missing; cannot resolve speed", base)
        return None
    try:
        entries = sorted(os.listdir(base))
    except OSError as exc:
        _LOG.debug("cannot list %r: %s", base, exc)
        return None
    for entry in entries:
        dev_path = Path(base) / entry
        serial_file = dev_path / "serial"
        if not serial_file.is_file():
            continue
        try:
            sysfs_serial = serial_file.read_text(encoding="ascii", errors="replace").strip()
        except OSError:
            continue
        if sysfs_serial != serial:
            continue
        speed_file = dev_path / "speed"
        if not speed_file.is_file():
            _LOG.debug("found device %s at %s but no speed file", serial, dev_path)
            return None
        try:
            speed_raw = speed_file.read_text(encoding="ascii", errors="replace").strip()
        except OSError:
            return None
        try:
            return int(speed_raw)
        except ValueError:
            _LOG.warning("unparseable speed value %r at %s", speed_raw, speed_file)
            return None
    return None
