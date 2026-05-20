"""Fingerprint module tests: USB-speed sysfs walk + device-prop parsing."""
from __future__ import annotations

from pathlib import Path

import pytest

from automation.adb import ADB
from automation.errors import ADBError
from automation.fingerprint import (
    DeviceFingerprint,
    find_usb_speed,
    fingerprint,
)

from tests.conftest import make_usb_entry


# ---------- find_usb_speed ----------------------------------------------------


def test_find_usb_speed_matches_serial_returns_480(fake_sysfs: Path) -> None:
    make_usb_entry(fake_sysfs, "7-2", serial="jfzxugsgnnvsrsg6", speed="480")
    assert find_usb_speed("jfzxugsgnnvsrsg6", base=str(fake_sysfs)) == 480


def test_find_usb_speed_rejects_12_mbps_hub_failure(fake_sysfs: Path) -> None:
    """Phase 0 observed 12 Mbps via a USB 1.1 keyboard hub. The bench
    must surface this exact number; the bootstrap rejects it."""
    make_usb_entry(fake_sysfs, "3-3.3", serial="ABCDEF", speed="12")
    assert find_usb_speed("ABCDEF", base=str(fake_sysfs)) == 12


def test_find_usb_speed_returns_none_when_no_match(fake_sysfs: Path) -> None:
    make_usb_entry(fake_sysfs, "1-1", serial="OTHER", speed="480")
    assert find_usb_speed("MISSING", base=str(fake_sysfs)) is None


def test_find_usb_speed_returns_none_when_speed_file_missing(fake_sysfs: Path) -> None:
    make_usb_entry(fake_sysfs, "1-1", serial="X", speed=None)
    assert find_usb_speed("X", base=str(fake_sysfs)) is None


def test_find_usb_speed_handles_missing_base() -> None:
    assert find_usb_speed("anything", base="/does/not/exist/xyz123") is None


def test_find_usb_speed_disambiguates_multiple_devices(fake_sysfs: Path) -> None:
    make_usb_entry(fake_sysfs, "1-1", serial="OTHER", speed="480")
    make_usb_entry(fake_sysfs, "7-2", serial="WANTED", speed="5000")
    make_usb_entry(fake_sysfs, "2-1.4", serial="ANOTHER", speed="12")
    assert find_usb_speed("WANTED", base=str(fake_sysfs)) == 5000


def test_find_usb_speed_ignores_unparseable_speed(fake_sysfs: Path,
                                                  caplog: pytest.LogCaptureFixture) -> None:
    make_usb_entry(fake_sysfs, "1-1", serial="X", speed="not-a-number")
    with caplog.at_level("WARNING", logger="automation.fingerprint"):
        assert find_usb_speed("X", base=str(fake_sysfs)) is None


# ---------- fingerprint() ----------------------------------------------------


@pytest.fixture
def adb_with_typical_device(subprocess_recorder) -> ADB:
    """Returns an ADB instance whose backing subprocess responds like
    the operator's Xiaomi 22095RA98C."""
    subprocess_recorder.register(["get-serialno"], stdout="jfzxugsgnnvsrsg6\n")
    subprocess_recorder.register(
        ["shell", "getprop", "ro.product.manufacturer"], stdout="Xiaomi\n"
    )
    subprocess_recorder.register(
        ["shell", "getprop", "ro.product.model"], stdout="22095RA98C\n"
    )
    subprocess_recorder.register(
        ["shell", "getprop", "ro.build.version.release"], stdout="13\n"
    )
    subprocess_recorder.register(
        ["shell", "getprop", "ro.build.version.sdk"], stdout="33\n"
    )
    subprocess_recorder.register(
        ["shell", "dumpsys", "window"],
        stdout=(
            "WINDOW MANAGER DISPLAY CONTENTS\n"
            "  Display: mDisplayId=0\n"
            "    init=1080x2408 440dpi mMinSizeOfResizeableTaskDp=200 "
            "cur=1080x2408 app=1080x2202\n"
        ),
    )
    subprocess_recorder.register(["version"],
                                  stdout="Android Debug Bridge version 1.0.41\nVersion 35.0.0-1\n")
    return ADB()


def test_fingerprint_populates_all_fields(adb_with_typical_device: ADB,
                                          fake_sysfs: Path) -> None:
    make_usb_entry(fake_sysfs, "7-2", serial="jfzxugsgnnvsrsg6", speed="480")
    fp = fingerprint(adb_with_typical_device, usb_devices_path=str(fake_sysfs))
    assert fp == DeviceFingerprint(
        serial="jfzxugsgnnvsrsg6",
        manufacturer="Xiaomi",
        model="22095RA98C",
        android_version="13",
        sdk=33,
        resolution=(1080, 2408),
        usb_speed_mbps=480,
        adb_version=(35, 0, 0),
    )


def test_fingerprint_usb_speed_none_when_sysfs_absent(adb_with_typical_device: ADB,
                                                       tmp_path: Path) -> None:
    fp = fingerprint(adb_with_typical_device, usb_devices_path=str(tmp_path / "missing"))
    assert fp.usb_speed_mbps is None
    # All non-USB fields still populated:
    assert fp.serial == "jfzxugsgnnvsrsg6"
    assert fp.resolution == (1080, 2408)


def test_fingerprint_raises_when_serial_empty(subprocess_recorder) -> None:
    subprocess_recorder.register(["get-serialno"], stdout="\n")
    subprocess_recorder.register(["version"],
                                  stdout="Android Debug Bridge version 1.0.41\nVersion 35.0.0-1\n")
    adb = ADB()
    with pytest.raises(ADBError, match="empty"):
        fingerprint(adb)


def test_fingerprint_raises_when_dumpsys_has_no_resolution(subprocess_recorder) -> None:
    subprocess_recorder.register(["get-serialno"], stdout="X\n")
    subprocess_recorder.register(["shell", "getprop", "ro.product.manufacturer"], stdout="M\n")
    subprocess_recorder.register(["shell", "getprop", "ro.product.model"], stdout="MM\n")
    subprocess_recorder.register(["shell", "getprop", "ro.build.version.release"], stdout="13\n")
    subprocess_recorder.register(["shell", "getprop", "ro.build.version.sdk"], stdout="33\n")
    subprocess_recorder.register(["shell", "dumpsys", "window"],
                                  stdout="no display info here\n")
    subprocess_recorder.register(["version"],
                                  stdout="Android Debug Bridge version 1.0.41\nVersion 35.0.0-1\n")
    adb = ADB()
    with pytest.raises(ADBError, match="parse display resolution"):
        fingerprint(adb)


def test_to_human_summary_contains_key_fields() -> None:
    fp = DeviceFingerprint(
        serial="S", manufacturer="M", model="MDL", android_version="13", sdk=33,
        resolution=(1080, 1920), usb_speed_mbps=480, adb_version=(35, 0, 0),
    )
    summary = fp.to_human_summary()
    assert "S" in summary
    assert "MDL" in summary
    assert "1080x1920" in summary
    assert "480 Mbps" in summary
    assert "35.0.0" in summary


def test_to_human_summary_with_unknown_usb_speed() -> None:
    fp = DeviceFingerprint(
        serial="S", manufacturer="M", model="MDL", android_version="13", sdk=33,
        resolution=(1080, 1920), usb_speed_mbps=None, adb_version=(35, 0, 0),
    )
    assert "unknown" in fp.to_human_summary()
