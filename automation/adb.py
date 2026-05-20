"""Thin, defensive wrapper around the host `adb` binary.

Phase 1 scope: just enough surface to verify the environment and
fingerprint a device. Every method is a small shell-out with explicit
timeouts and typed errors. No screenshot logic, no input logic, no
caching, no concurrency primitives — those land in later phases.

Subprocess use is intentionally simple (`subprocess.run`); Phase 5/7
will replace it with an asyncio-friendly version once the orchestrator
needs concurrency. Until then, blocking calls with a strict timeout
are fine.
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Sequence

from .errors import ADBError

_LOG = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S: float = 10.0

# `adb version` first line, e.g.
#   Android Debug Bridge version 1.0.41
# second line:
#   Version 35.0.0-11411520
_ADB_VERSION_RE = re.compile(r"^Version\s+(\d+)\.(\d+)\.(\d+)", re.MULTILINE)

# `adb devices` per-device line:
#   <serial>\t<state>[ key:value ...]
# The leading "List of devices attached" header is skipped.
_DEVICE_LINE_RE = re.compile(r"^(?P<serial>\S+)\s+(?P<state>\S+)(?:\s+(?P<extra>.*))?$")


@dataclass(frozen=True)
class DeviceListing:
    """One entry from `adb devices -l`.

    `state` is the ADB protocol state token: `device`, `unauthorized`,
    `offline`, `no permissions`, `recovery`, `sideload`, etc.
    """
    serial: str
    state: str
    extra: str = ""


class ADB:
    """Thin wrapper around the `adb` command-line client.

    All methods are blocking, single-shot, and timeout-bounded. A
    failure surfaces as `ADBError` with the underlying `stderr` content
    where possible.

    The binary path defaults to `"adb"` and is resolved at construction
    time so the wrapper fails fast if `adb` is not on `$PATH`.
    """

    def __init__(self, binary: str = "adb", default_timeout: float = DEFAULT_TIMEOUT_S) -> None:
        resolved = shutil.which(binary)
        if resolved is None:
            raise ADBError(f"adb binary not found on PATH (looked for: {binary!r})")
        self.binary: str = resolved
        self.default_timeout: float = default_timeout

    # --- low-level ---------------------------------------------------

    def _run(
        self,
        args: Sequence[str],
        *,
        timeout: float | None = None,
        capture_stdout_bytes: bool = False,
    ) -> subprocess.CompletedProcess[bytes] | subprocess.CompletedProcess[str]:
        """Execute `adb <args>` with timeout enforcement.

        Returns the `CompletedProcess`. Raises `ADBError` on non-zero
        exit, timeout, or `FileNotFoundError` (latter only if the
        resolved binary was removed between construction and call).
        """
        cmd = [self.binary, *args]
        eff_timeout = self.default_timeout if timeout is None else timeout
        _LOG.debug("adb run: %s (timeout=%s)", cmd, eff_timeout)
        try:
            if capture_stdout_bytes:
                proc = subprocess.run(cmd, capture_output=True, timeout=eff_timeout, check=False)
            else:
                proc = subprocess.run(
                    cmd, capture_output=True, timeout=eff_timeout, check=False, text=True
                )
        except subprocess.TimeoutExpired as exc:
            raise ADBError(f"adb {args[0] if args else ''} timed out after {eff_timeout}s") from exc
        except FileNotFoundError as exc:
            raise ADBError(f"adb binary disappeared: {self.binary}") from exc
        if proc.returncode != 0:
            stderr = (
                proc.stderr.decode("utf-8", errors="replace")
                if isinstance(proc.stderr, bytes)
                else (proc.stderr or "")
            )
            raise ADBError(
                f"adb {' '.join(args)} exited {proc.returncode}: {stderr.strip() or '(no stderr)'}"
            )
        return proc

    # --- public API --------------------------------------------------

    def adb_version(self, *, timeout: float | None = None) -> tuple[int, int, int]:
        """Return the host `adb` client version as `(major, minor, patch)`."""
        proc = self._run(["version"], timeout=timeout)
        stdout = proc.stdout if isinstance(proc.stdout, str) else proc.stdout.decode()
        match = _ADB_VERSION_RE.search(stdout)
        if match is None:
            raise ADBError(f"could not parse adb version from output:\n{stdout!r}")
        return int(match.group(1)), int(match.group(2)), int(match.group(3))

    def get_state(self, *, timeout: float | None = None) -> str:
        """Return the connected device's ADB state token.

        Raises `ADBError` on `adb get-state`'s own non-zero exit, which
        occurs when no device is connected. Callers should catch and
        translate to `DeviceNotFoundError` if a device is required.
        """
        proc = self._run(["get-state"], timeout=timeout)
        return (proc.stdout if isinstance(proc.stdout, str) else proc.stdout.decode()).strip()

    def get_serialno(self, *, timeout: float | None = None) -> str:
        """Return the connected device's serial."""
        proc = self._run(["get-serialno"], timeout=timeout)
        return (proc.stdout if isinstance(proc.stdout, str) else proc.stdout.decode()).strip()

    def devices(self, *, timeout: float | None = None) -> list[DeviceListing]:
        """Return all devices visible to the host adb server.

        Parses `adb devices -l`. Tolerates extra header/footer lines.
        Lines that do not match the device-listing pattern are skipped
        with a DEBUG log; no exception is raised for unknown lines so
        that future adb versions adding decoration to the output do not
        break Phase 1.
        """
        proc = self._run(["devices", "-l"], timeout=timeout)
        stdout = proc.stdout if isinstance(proc.stdout, str) else proc.stdout.decode()
        out: list[DeviceListing] = []
        for raw in stdout.splitlines():
            line = raw.strip()
            if not line or line.lower().startswith("list of devices"):
                continue
            m = _DEVICE_LINE_RE.match(line)
            if m is None:
                _LOG.debug("skipping unparseable devices-line: %r", line)
                continue
            out.append(
                DeviceListing(
                    serial=m.group("serial"),
                    state=m.group("state"),
                    extra=(m.group("extra") or "").strip(),
                )
            )
        return out

    def shell(self, args: Sequence[str], *, timeout: float | None = None) -> str:
        """Run `adb shell <args>` and return stdout as text.

        `args` is a list, NOT a single shell string. This avoids host-side
        quoting bugs but means callers cannot pass a single composed
        shell command; for that, pass `["sh", "-c", "<cmd>"]`.
        """
        proc = self._run(["shell", *args], timeout=timeout)
        return proc.stdout if isinstance(proc.stdout, str) else proc.stdout.decode()

    def exec_out(self, args: Sequence[str], *, timeout: float | None = None) -> bytes:
        """Run `adb exec-out <args>` and return stdout as raw bytes.

        Used by SENSE (Phase 2) for binary screencap data. Exposed in
        Phase 1 only as a tested interface; no SENSE consumer exists yet.
        """
        proc = self._run(["exec-out", *args], timeout=timeout, capture_stdout_bytes=True)
        assert isinstance(proc.stdout, (bytes, bytearray))
        return bytes(proc.stdout)
