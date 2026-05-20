"""Python-side bootstrap: callable as `python -m automation.bootstrap`.

Validates the runtime environment, probes the connected device, and
emits a structured human-readable summary. Returns exit code 0 on
success, non-zero on any precondition failure.

The host-side prerequisites (Python version, adb binary, runtime dirs)
are also covered by `scripts/bootstrap.sh`. This Python entry point
duplicates a subset of those checks so the framework's own runtime
exposes the same fail-fast contract — operators may invoke either, but
the Python entry point is what the systemd unit will run in later
phases.

Exit codes:

    0   success
    2   prerequisite violation (Python too old, adb missing, etc.)
    3   no device / unauthorized device
    4   USB link speed below 480 Mbps
    5   USB link speed unverifiable (warning only — does not exit non-zero
        if `--allow-unverified-usb` is passed; otherwise treated like 4)
    1   unexpected error
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass

from .adb import ADB
from .errors import (
    ADBError,
    AutomationError,
    BootstrapError,
    DeviceNotFoundError,
    USBValidationError,
)
from .fingerprint import DeviceFingerprint, fingerprint
from .paths import RUNTIME_DIRS, ensure_runtime_dirs

_LOG = logging.getLogger("automation.bootstrap")

MIN_PYTHON: tuple[int, int] = (3, 11)
MIN_ADB: tuple[int, int, int] = (34, 0, 0)
MIN_USB_SPEED_MBPS: int = 480

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_ENV_PRECONDITION = 2
EXIT_NO_DEVICE = 3
EXIT_USB_TOO_SLOW = 4
EXIT_USB_UNVERIFIED = 5


@dataclass(frozen=True)
class BootstrapResult:
    """Result of a successful bootstrap run."""

    fingerprint: DeviceFingerprint
    usb_speed_verified: bool


def configure_logging(verbose: bool) -> None:
    """Configure the root logger for human-readable bootstrap output.

    Bootstrap uses plaintext logging (not the structured JSON logger
    that lands in Phase 6) because its consumers are humans reading a
    terminal.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )


def _check_python() -> None:
    if sys.version_info[:2] < MIN_PYTHON:
        raise BootstrapError(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, "
            f"found {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        )


def _check_adb(adb: ADB) -> tuple[int, int, int]:
    version = adb.adb_version()
    if version < MIN_ADB:
        raise BootstrapError(
            f"adb (platform-tools) {MIN_ADB[0]}.{MIN_ADB[1]}.{MIN_ADB[2]}+ required, "
            f"found {version[0]}.{version[1]}.{version[2]}"
        )
    return version


def _check_device(adb: ADB) -> str:
    try:
        state = adb.get_state()
    except ADBError as exc:
        raise DeviceNotFoundError(
            "no Android device detected. Connect the device over USB, enable USB debugging, "
            "and ensure `adb devices` shows it as `device`. Original error: " + str(exc)
        ) from exc
    if state != "device":
        raise DeviceNotFoundError(
            f"device is in state {state!r}; expected `device`. "
            f"For `unauthorized`, accept the USB-debugging prompt on the phone. "
            f"For `no permissions`, install udev rules and re-plug."
        )
    return state


def _check_usb_speed(fp: DeviceFingerprint, allow_unverified: bool) -> bool:
    """Validate the device's USB link speed against the frozen NFR.

    Returns True if verified ≥ 480 Mbps. Raises `USBValidationError` if
    the link is too slow (12 Mbps, 1.5 Mbps). Returns False (without
    raising) only if unverifiable and `allow_unverified` is True.
    """
    speed = fp.usb_speed_mbps
    if speed is None:
        msg = (
            "could not verify USB link speed for serial "
            f"{fp.serial} (sysfs path not resolvable). "
            "Operator must verify manually that the device is plugged into a USB 2.0 "
            "(480 Mbps) or USB 3.x port."
        )
        if allow_unverified:
            _LOG.warning("%s — continuing because --allow-unverified-usb was passed", msg)
            return False
        raise USBValidationError(msg)
    if speed < MIN_USB_SPEED_MBPS:
        raise USBValidationError(
            f"USB link speed for serial {fp.serial} is {speed} Mbps; "
            f"minimum required is {MIN_USB_SPEED_MBPS} Mbps. "
            "Cause is almost always an intermediate USB hub (keyboard, monitor, dock) "
            "downgrading the link to USB 1.1 Full Speed. Replug the cable directly "
            "into a USB 2.0 high-speed or USB 3.x port on the host."
        )
    _LOG.info("USB link speed OK: %d Mbps", speed)
    return True


def run(allow_unverified_usb: bool = False) -> BootstrapResult:
    """Execute the bootstrap procedure. Raises on any precondition failure.

    Steps:
      1. Python version check.
      2. ADB binary + version check.
      3. Connected-device check.
      4. Fingerprint the device (incl. USB-speed sysfs read).
      5. USB-speed gate.
      6. Create runtime directories.

    Returns a `BootstrapResult` carrying the fingerprint and the
    verification status of the USB link.
    """
    _check_python()
    adb = ADB()
    _check_adb(adb)
    _check_device(adb)
    fp = fingerprint(adb)
    usb_verified = _check_usb_speed(fp, allow_unverified=allow_unverified_usb)
    ensure_runtime_dirs()
    return BootstrapResult(fingerprint=fp, usb_speed_verified=usb_verified)


def _print_summary(result: BootstrapResult) -> None:
    fp = result.fingerprint
    print("Bootstrap summary")
    print("=================")
    print("Environment")
    print(f"  python:           {sys.version.split()[0]}")
    print(f"  adb:              {'.'.join(str(x) for x in fp.adb_version)}")
    print("Device")
    print(fp.to_human_summary())
    print("Runtime directories")
    for d in RUNTIME_DIRS:
        print(f"  {d}  {'(present)' if d.is_dir() else '(MISSING)'}")
    print()
    print(
        "Status: READY"
        if result.usb_speed_verified
        else "Status: READY (USB link speed unverified; operator override)"
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code; does not call sys.exit."""
    parser = argparse.ArgumentParser(
        prog="python -m automation.bootstrap",
        description="Validate environment and connected device for the automation framework.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable DEBUG logging on stderr.",
    )
    parser.add_argument(
        "--allow-unverified-usb", action="store_true",
        help=(
            "Proceed even when the USB link speed cannot be read from sysfs. "
            "Has no effect when sysfs reports a speed below the minimum — that "
            "case still fails closed."
        ),
    )
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    try:
        result = run(allow_unverified_usb=args.allow_unverified_usb)
    except DeviceNotFoundError as exc:
        _LOG.error("device check failed: %s", exc)
        return EXIT_NO_DEVICE
    except USBValidationError as exc:
        _LOG.error("USB validation failed: %s", exc)
        speed_unknown = "could not verify USB link speed" in str(exc)
        return EXIT_USB_UNVERIFIED if speed_unknown else EXIT_USB_TOO_SLOW
    except BootstrapError as exc:
        _LOG.error("environment precondition failed: %s", exc)
        return EXIT_ENV_PRECONDITION
    except ADBError as exc:
        _LOG.error("ADB error during bootstrap: %s", exc)
        return EXIT_NO_DEVICE
    except AutomationError as exc:
        _LOG.error("unexpected framework error: %s", exc)
        return EXIT_UNEXPECTED
    except Exception as exc:  # noqa: BLE001 — last-chance handler at the CLI boundary
        _LOG.exception("unexpected error: %s", exc)
        return EXIT_UNEXPECTED

    _print_summary(result)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
