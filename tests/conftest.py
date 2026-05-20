"""Shared pytest fixtures.

These build small in-memory or tmp-path fakes for `subprocess` and the
USB sysfs tree so unit tests do not depend on a connected device.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest


@dataclass
class FakeProc:
    """Stand-in for `subprocess.CompletedProcess[bytes|str]`."""

    args: list[str]
    returncode: int
    stdout: bytes | str
    stderr: bytes | str


@dataclass
class SubprocessRecorder:
    """Records calls to a fake `subprocess.run` and replays canned responses.

    `responses` maps a tuple of argv tokens (first match, longest first
    isn't necessary — exact-tuple match is used) to a `FakeProc`. A
    default `FakeProc` is returned for unmatched calls; tests can also
    set `raise_on_call` to a sequence of exceptions to inject.
    """

    responses: dict[tuple[str, ...], FakeProc] = field(default_factory=dict)
    default: FakeProc | None = None
    calls: list[list[str]] = field(default_factory=list)

    def register(self, argv: list[str], stdout: bytes | str = b"", stderr: bytes | str = b"",
                 returncode: int = 0) -> None:
        self.responses[tuple(argv)] = FakeProc(
            args=argv, returncode=returncode, stdout=stdout, stderr=stderr
        )

    def __call__(self, cmd, *, capture_output: bool = True, timeout: float | None = None,
                 check: bool = False, text: bool = False, **kwargs) -> FakeProc:
        self.calls.append(list(cmd))
        key = tuple(cmd)
        proc = self.responses.get(key)
        if proc is None:
            # Try matching by prefix (drop the binary path prefix). This
            # allows tests to register `["version"]` while the real call
            # passes `["/path/to/adb", "version"]`.
            for argv, candidate in self.responses.items():
                if tuple(cmd[-len(argv):]) == argv:
                    proc = candidate
                    break
        if proc is None and self.default is not None:
            proc = self.default
        if proc is None:
            raise AssertionError(f"unexpected subprocess.run call: {cmd!r}")
        if text and isinstance(proc.stdout, bytes):
            proc = FakeProc(
                args=proc.args, returncode=proc.returncode,
                stdout=proc.stdout.decode("utf-8", errors="replace"),
                stderr=(proc.stderr.decode("utf-8", errors="replace")
                        if isinstance(proc.stderr, bytes) else proc.stderr),
            )
        elif not text and isinstance(proc.stdout, str):
            proc = FakeProc(
                args=proc.args, returncode=proc.returncode,
                stdout=proc.stdout.encode("utf-8"),
                stderr=(proc.stderr.encode("utf-8")
                        if isinstance(proc.stderr, str) else proc.stderr),
            )
        return proc


@pytest.fixture
def subprocess_recorder(monkeypatch: pytest.MonkeyPatch) -> SubprocessRecorder:
    """Replace `automation.adb.subprocess.run` with a recorder."""
    rec = SubprocessRecorder()
    import automation.adb as adb_mod
    monkeypatch.setattr(adb_mod.subprocess, "run", rec)
    # Also pin shutil.which so `ADB()` construction does not depend on
    # whether the real `adb` binary is on PATH inside the test runner.
    monkeypatch.setattr(adb_mod.shutil, "which", lambda name: f"/fake/path/{name}")
    return rec


@pytest.fixture
def fake_sysfs(tmp_path: Path) -> Path:
    """An empty sysfs-style directory for USB device tests.

    Tests typically populate this via the `make_usb_entry` helper. The
    fixture returns the base path; tests pass it via
    `find_usb_speed(serial, base=str(fake_sysfs))`.
    """
    base = tmp_path / "sys_bus_usb_devices"
    base.mkdir()
    return base


def make_usb_entry(base: Path, name: str, *, serial: str | None,
                   speed: str | None) -> Path:
    """Helper for tests: create one fake `/sys/bus/usb/devices/<name>` entry."""
    dev = base / name
    dev.mkdir()
    if serial is not None:
        (dev / "serial").write_text(serial + "\n", encoding="ascii")
    if speed is not None:
        (dev / "speed").write_text(speed + "\n", encoding="ascii")
    return dev
