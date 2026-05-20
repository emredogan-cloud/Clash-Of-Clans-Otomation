"""ADB wrapper tests against mocked subprocess output."""
from __future__ import annotations

import subprocess

import pytest

from automation.adb import ADB, DeviceListing
from automation.errors import ADBError


ADB_VERSION_STDOUT = (
    "Android Debug Bridge version 1.0.41\n"
    "Version 35.0.0-11411520\n"
    "Installed as /usr/lib/android-sdk/platform-tools/adb\n"
)


def test_adb_version_parses_major_minor_patch(subprocess_recorder) -> None:
    subprocess_recorder.register(["version"], stdout=ADB_VERSION_STDOUT)
    adb = ADB()
    assert adb.adb_version() == (35, 0, 0)


def test_adb_version_raises_on_unparseable_output(subprocess_recorder) -> None:
    subprocess_recorder.register(["version"], stdout="garbage\n")
    adb = ADB()
    with pytest.raises(ADBError, match="could not parse adb version"):
        adb.adb_version()


def test_get_state_returns_device(subprocess_recorder) -> None:
    subprocess_recorder.register(["get-state"], stdout="device\n")
    adb = ADB()
    assert adb.get_state() == "device"


def test_get_state_raises_when_no_device(subprocess_recorder) -> None:
    subprocess_recorder.register(
        ["get-state"], stdout="", stderr="error: no devices/emulators found\n", returncode=1
    )
    adb = ADB()
    with pytest.raises(ADBError, match="no devices/emulators"):
        adb.get_state()


def test_devices_parses_authorized_device(subprocess_recorder) -> None:
    subprocess_recorder.register(
        ["devices", "-l"],
        stdout=(
            "List of devices attached\n"
            "jfzxugsgnnvsrsg6       device 7-2 product:light model:22095RA98C "
            "device:light transport_id:6\n"
        ),
    )
    adb = ADB()
    out = adb.devices()
    assert len(out) == 1
    assert out[0].serial == "jfzxugsgnnvsrsg6"
    assert out[0].state == "device"
    assert "product:light" in out[0].extra


def test_devices_parses_unauthorized(subprocess_recorder) -> None:
    subprocess_recorder.register(
        ["devices", "-l"],
        stdout=(
            "List of devices attached\n"
            "ABCDEF01234            unauthorized usb:1-1\n"
        ),
    )
    adb = ADB()
    out = adb.devices()
    assert len(out) == 1
    assert out[0].state == "unauthorized"


def test_devices_parses_empty(subprocess_recorder) -> None:
    subprocess_recorder.register(["devices", "-l"], stdout="List of devices attached\n")
    adb = ADB()
    assert adb.devices() == []


def test_shell_returns_stdout(subprocess_recorder) -> None:
    subprocess_recorder.register(["shell", "echo", "hi"], stdout="hi\n")
    adb = ADB()
    assert adb.shell(["echo", "hi"]) == "hi\n"


def test_exec_out_returns_bytes(subprocess_recorder) -> None:
    subprocess_recorder.register(["exec-out", "screencap"], stdout=b"\x00\x01\x02")
    adb = ADB()
    assert adb.exec_out(["screencap"]) == b"\x00\x01\x02"


def test_get_serialno(subprocess_recorder) -> None:
    subprocess_recorder.register(["get-serialno"], stdout="jfzxugsgnnvsrsg6\n")
    adb = ADB()
    assert adb.get_serialno() == "jfzxugsgnnvsrsg6"


def test_timeout_raises_adb_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subprocess TimeoutExpired is translated to ADBError."""
    import automation.adb as adb_mod

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(adb_mod.shutil, "which", lambda name: f"/fake/{name}")
    monkeypatch.setattr(adb_mod.subprocess, "run", fake_run)
    adb = adb_mod.ADB()
    with pytest.raises(ADBError, match="timed out"):
        adb.adb_version()


def test_missing_adb_binary_raises_at_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    import automation.adb as adb_mod

    monkeypatch.setattr(adb_mod.shutil, "which", lambda name: None)
    with pytest.raises(ADBError, match="adb binary not found"):
        adb_mod.ADB()


def test_device_listing_dataclass_fields() -> None:
    d = DeviceListing(serial="ABC", state="device", extra="usb:1")
    assert d.serial == "ABC"
    assert d.state == "device"
    assert d.extra == "usb:1"
