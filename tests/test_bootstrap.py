"""Bootstrap end-to-end tests with full subprocess + sysfs mocking."""
from __future__ import annotations

from pathlib import Path

import pytest

from automation.bootstrap import (
    EXIT_ENV_PRECONDITION,
    EXIT_NO_DEVICE,
    EXIT_OK,
    EXIT_USB_TOO_SLOW,
    EXIT_USB_UNVERIFIED,
    main,
    run,
)
from automation.errors import (
    DeviceNotFoundError,
    USBValidationError,
)

from tests.conftest import make_usb_entry


def _register_typical_adb(rec) -> None:
    rec.register(["version"],
                 stdout="Android Debug Bridge version 1.0.41\nVersion 35.0.0-11411520\n")
    rec.register(["get-state"], stdout="device\n")
    rec.register(["get-serialno"], stdout="DEVSERIAL\n")
    rec.register(["shell", "getprop", "ro.product.manufacturer"], stdout="Xiaomi\n")
    rec.register(["shell", "getprop", "ro.product.model"], stdout="22095RA98C\n")
    rec.register(["shell", "getprop", "ro.build.version.release"], stdout="13\n")
    rec.register(["shell", "getprop", "ro.build.version.sdk"], stdout="33\n")
    rec.register(["shell", "dumpsys", "window"],
                 stdout="init=1080x2408 cur=1080x2408 app=1080x2202\n")


def test_bootstrap_happy_path(subprocess_recorder, fake_sysfs: Path,
                              monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _register_typical_adb(subprocess_recorder)
    make_usb_entry(fake_sysfs, "7-2", serial="DEVSERIAL", speed="480")
    monkeypatch.setattr("automation.fingerprint.USB_DEVICES_PATH", str(fake_sysfs))

    # Redirect runtime dirs into tmp_path.
    import automation.paths as paths
    monkeypatch.setattr(paths, "VAR", tmp_path / "var")
    monkeypatch.setattr(paths, "LOGS", tmp_path / "var" / "logs")
    monkeypatch.setattr(paths, "METRICS", tmp_path / "var" / "metrics")
    monkeypatch.setattr(paths, "ARTIFACTS", tmp_path / "var" / "artifacts")
    monkeypatch.setattr(paths, "TMP", tmp_path / "var" / "tmp")
    monkeypatch.setattr(paths, "RUNTIME_DIRS",
                        (paths.LOGS, paths.METRICS, paths.ARTIFACTS, paths.TMP))

    result = run()
    assert result.usb_speed_verified is True
    fp = result.fingerprint
    assert fp.serial == "DEVSERIAL"
    assert fp.resolution == (1080, 2408)
    assert fp.usb_speed_mbps == 480
    for d in paths.RUNTIME_DIRS:
        assert d.is_dir()


def test_bootstrap_main_returns_zero_on_happy_path(subprocess_recorder, fake_sysfs: Path,
                                                    monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path: Path, capsys) -> None:
    _register_typical_adb(subprocess_recorder)
    make_usb_entry(fake_sysfs, "7-2", serial="DEVSERIAL", speed="480")
    monkeypatch.setattr("automation.fingerprint.USB_DEVICES_PATH", str(fake_sysfs))

    import automation.paths as paths
    monkeypatch.setattr(paths, "VAR", tmp_path / "var")
    monkeypatch.setattr(paths, "LOGS", tmp_path / "var" / "logs")
    monkeypatch.setattr(paths, "METRICS", tmp_path / "var" / "metrics")
    monkeypatch.setattr(paths, "ARTIFACTS", tmp_path / "var" / "artifacts")
    monkeypatch.setattr(paths, "TMP", tmp_path / "var" / "tmp")
    monkeypatch.setattr(paths, "RUNTIME_DIRS",
                        (paths.LOGS, paths.METRICS, paths.ARTIFACTS, paths.TMP))

    rc = main([])
    assert rc == EXIT_OK
    out = capsys.readouterr().out
    assert "DEVSERIAL" in out
    assert "480 Mbps" in out
    assert "READY" in out


def test_bootstrap_rejects_12_mbps_usb(subprocess_recorder, fake_sysfs: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """The 12 Mbps hub failure mode observed in Phase 0 must be rejected."""
    _register_typical_adb(subprocess_recorder)
    make_usb_entry(fake_sysfs, "3-3.3", serial="DEVSERIAL", speed="12")
    monkeypatch.setattr("automation.fingerprint.USB_DEVICES_PATH", str(fake_sysfs))

    with pytest.raises(USBValidationError, match="12 Mbps"):
        run()


def test_bootstrap_main_exits_4_on_too_slow_usb(subprocess_recorder, fake_sysfs: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    _register_typical_adb(subprocess_recorder)
    make_usb_entry(fake_sysfs, "3-3.3", serial="DEVSERIAL", speed="12")
    monkeypatch.setattr("automation.fingerprint.USB_DEVICES_PATH", str(fake_sysfs))

    rc = main([])
    assert rc == EXIT_USB_TOO_SLOW


def test_bootstrap_no_device_returns_exit_3(subprocess_recorder) -> None:
    subprocess_recorder.register(["version"],
                                  stdout="Android Debug Bridge version 1.0.41\nVersion 35.0.0-1\n")
    subprocess_recorder.register(
        ["get-state"], stdout="", stderr="error: no devices/emulators found\n", returncode=1
    )

    with pytest.raises(DeviceNotFoundError):
        run()

    rc = main([])
    assert rc == EXIT_NO_DEVICE


def test_bootstrap_too_old_adb_returns_exit_2(subprocess_recorder) -> None:
    subprocess_recorder.register(
        ["version"], stdout="Android Debug Bridge version 1.0.41\nVersion 33.5.0-1\n"
    )
    rc = main([])
    assert rc == EXIT_ENV_PRECONDITION


def test_bootstrap_unverified_usb_strict_rejects(subprocess_recorder,
                                                  tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    _register_typical_adb(subprocess_recorder)
    # Point at an empty sysfs tree — speed cannot be verified.
    empty_sysfs = tmp_path / "empty_sys"
    empty_sysfs.mkdir()
    monkeypatch.setattr("automation.fingerprint.USB_DEVICES_PATH", str(empty_sysfs))

    rc = main([])
    assert rc == EXIT_USB_UNVERIFIED


def test_bootstrap_unverified_usb_with_allow_flag(subprocess_recorder, tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch,
                                                   capsys) -> None:
    _register_typical_adb(subprocess_recorder)
    empty_sysfs = tmp_path / "empty_sys"
    empty_sysfs.mkdir()
    monkeypatch.setattr("automation.fingerprint.USB_DEVICES_PATH", str(empty_sysfs))

    import automation.paths as paths
    monkeypatch.setattr(paths, "VAR", tmp_path / "var")
    monkeypatch.setattr(paths, "LOGS", tmp_path / "var" / "logs")
    monkeypatch.setattr(paths, "METRICS", tmp_path / "var" / "metrics")
    monkeypatch.setattr(paths, "ARTIFACTS", tmp_path / "var" / "artifacts")
    monkeypatch.setattr(paths, "TMP", tmp_path / "var" / "tmp")
    monkeypatch.setattr(paths, "RUNTIME_DIRS",
                        (paths.LOGS, paths.METRICS, paths.ARTIFACTS, paths.TMP))

    rc = main(["--allow-unverified-usb"])
    assert rc == EXIT_OK
    summary = capsys.readouterr().out
    assert "operator override" in summary.lower()
