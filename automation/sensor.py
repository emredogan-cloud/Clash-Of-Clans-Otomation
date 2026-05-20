"""SENSE layer — `Sensor` and the raw-screencap header parser.

Phase 2 implements the screenshot pipeline specified by ADR-01a:

- Default mode is `raw` (`adb exec-out screencap`). Latency is
  content-deterministic; on the operator's hardware the Phase 0 median
  is ~947 ms.
- `png` mode (`adb exec-out screencap -p`) is faster on low-entropy
  screens, slower on high-entropy screens.
- `pull` mode (`adb shell screencap ... + adb pull`) is the legacy
  fallback, consistently middle-pack to slow.
- `auto` mode tries raw, then falls back to png, then pull, latching on
  the first that succeeds for the rest of the session.

Latency is measured with `time.perf_counter_ns()` from the moment
capture is requested through the production of a fully-decoded BGR
ndarray, before the `Remap` step. The latency on the returned `Frame`
covers only the decode budget; resampling is intentionally NOT
included so the number is comparable across reference-resolution
choices.

Debug artifacts are emitted to `var/artifacts/sensor/<timestamp>/`
when the constructor's `debug=True` is set or the `SENSOR_DEBUG=1`
environment variable is present at construction time. Each capture
writes `metadata.json`, the raw payload, and a JPEG of the decoded
frame, atomically (`.tmp` → rename). Artifacts are advisory; the
sensor's correctness does not depend on them.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import shutil
import struct
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Final

import cv2
import numpy as np

from .adb import ADB
from .errors import (
    ADBError,
    CaptureError,
    FrameDecodeError,
    UnsupportedPixelFormatError,
)
from .frame import Frame
from .paths import ARTIFACTS
from .remap import Remap

_LOG = logging.getLogger(__name__)

ARTIFACTS_DIR: Path = ARTIFACTS / "sensor"

# Supported sensor modes per ADR-01a Decision (2). The values are exact
# strings consumed by both the config knob and the auto-mode latch.
SUPPORTED_MODES: Final[frozenset[str]] = frozenset({"raw", "png", "pull", "auto"})
CONCRETE_MODES: Final[tuple[str, ...]] = ("raw", "png", "pull")

# ADR-02: PIXEL_FORMAT_RGBA_8888 is the only raw format we accept.
PIXEL_FORMAT_RGBA_8888: Final[int] = 1
PIXEL_FORMAT_NAMES: Final[dict[int, str]] = {
    0: "PIXEL_FORMAT_UNKNOWN",
    1: "PIXEL_FORMAT_RGBA_8888",
    2: "PIXEL_FORMAT_RGBX_8888",
    3: "PIXEL_FORMAT_RGB_888",
    4: "PIXEL_FORMAT_RGB_565",
    5: "PIXEL_FORMAT_BGRA_8888",
}

CAPTURE_TIMEOUT_S: float = 30.0


# ---------------------------------------------------------------------------
# Raw header parser
# ---------------------------------------------------------------------------


def parse_raw_screencap(buf: bytes) -> tuple[np.ndarray, int, int]:
    """Decode a raw `adb exec-out screencap` buffer into a BGR ndarray.

    Supports both layouts:

    - Android 9+ (16-byte header):
        uint32 width, uint32 height, uint32 pixel_format, uint32 colorspace
        followed by W*H*4 RGBA bytes.

    - Pre-Android-9 (12-byte header):
        uint32 width, uint32 height, uint32 pixel_format
        followed by W*H*4 RGBA bytes.

    Returns `(image_bgr, width, height)`. Raises:

    - `FrameDecodeError` if the buffer is too short, dimensions are
      implausible, or the declared dimensions do not match the byte
      length under either layout.
    - `UnsupportedPixelFormatError` if `pixel_format != 1`.
    """
    if len(buf) < 12:
        raise FrameDecodeError(
            f"raw screencap buffer too small to contain header: {len(buf)} bytes"
        )

    # Try the 16-byte layout first (modern Android).
    w16, h16, fmt16, _colorspace = struct.unpack_from("<IIII", buf, 0)
    expected_len_16 = 16 + w16 * h16 * 4

    # Try the 12-byte layout.
    w12, h12, fmt12 = struct.unpack_from("<III", buf, 0)
    expected_len_12 = 12 + w12 * h12 * 4

    if 0 < w16 <= 8192 and 0 < h16 <= 8192 and len(buf) == expected_len_16:
        header_size = 16
        width, height, pixel_format = w16, h16, fmt16
    elif 0 < w12 <= 8192 and 0 < h12 <= 8192 and len(buf) == expected_len_12:
        header_size = 12
        width, height, pixel_format = w12, h12, fmt12
    else:
        raise FrameDecodeError(
            f"raw screencap buffer length {len(buf)} does not match either "
            f"layout (16-byte expects {expected_len_16} for {w16}x{h16}; "
            f"12-byte expects {expected_len_12} for {w12}x{h12})"
        )

    if pixel_format != PIXEL_FORMAT_RGBA_8888:
        fmt_name = PIXEL_FORMAT_NAMES.get(pixel_format, f"<unknown {pixel_format}>")
        raise UnsupportedPixelFormatError(
            f"raw screencap declares pixel_format {pixel_format} ({fmt_name}); "
            f"only PIXEL_FORMAT_RGBA_8888 (1) is supported in v1.0"
        )

    pixels = np.frombuffer(buf, dtype=np.uint8, count=width * height * 4, offset=header_size)
    rgba = pixels.reshape(height, width, 4)
    bgr = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
    return bgr, width, height


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------


def _parse_bool_env(var: str, default: bool = False) -> bool:
    raw = os.environ.get(var)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


class Sensor:
    """Capture frames from a connected Android device.

    Public surface:

    - `capture() -> Frame` — performs the full pipeline (capture →
      parse → BGR ndarray → remap → Frame). `Frame.capture_latency_ms`
      records the pre-remap budget (the resampling cost is excluded so
      the number is comparable across reference choices).

    Mode behavior:

    - `"raw"`, `"png"`, `"pull"`: the named pipeline runs every
      capture. Failures raise.
    - `"auto"`: try raw, then png, then pull within the first capture.
      Whichever succeeds is latched into `self.active_mode` for the
      rest of the session. Subsequent captures use only the latched
      mode (no per-call re-trial; see ADR-01a §Decision (2) and
      DESIGN-REVIEW §9.2).

    Constructor params:

    - `adb`: an `ADB` instance.
    - `mode`: `"raw"` (default) | `"png"` | `"pull"` | `"auto"`.
    - `remap`: a `Remap` instance. If omitted, the default
      `Remap()` (1080×1920 reference) is constructed.
    - `debug`: write per-capture artifacts to
      `var/artifacts/sensor/<timestamp>/`. If `None`, the value of the
      `SENSOR_DEBUG` env var is consulted at construction time.
    """

    def __init__(
        self,
        adb: ADB,
        *,
        mode: str = "raw",
        remap: Remap | None = None,
        debug: bool | None = None,
    ) -> None:
        if mode not in SUPPORTED_MODES:
            raise ValueError(
                f"unsupported sensor mode {mode!r}; must be one of {sorted(SUPPORTED_MODES)}"
            )
        self.adb: ADB = adb
        self.requested_mode: str = mode
        self.active_mode: str | None = None if mode == "auto" else mode
        self.remap: Remap = remap if remap is not None else Remap()
        self.debug: bool = debug if debug is not None else _parse_bool_env("SENSOR_DEBUG")
        self._auto_fallback_logged: bool = False

    # ---- public API ---------------------------------------------------

    def capture(self) -> Frame:
        """Capture, parse, convert, normalise, and return a `Frame`.

        The end-to-end pipeline runs once per call. Raises `SensorError`
        (or a subclass) on any failure that propagates past the mode's
        fallback logic.
        """
        t_start = time.perf_counter_ns()

        if self.requested_mode == "auto" and self.active_mode is None:
            payload, native_bgr, native_w, native_h, mode_used = self._capture_and_decode_auto()
        else:
            mode_used = self.active_mode if self.active_mode is not None else self.requested_mode
            payload = self._capture_one(mode_used)
            # Decode payload to native BGR ndarray.
            native_bgr, native_w, native_h = self._decode(payload, mode_used)

        t_decoded = time.perf_counter_ns()
        capture_latency_ms = (t_decoded - t_start) / 1e6
        capture_ts = _dt.datetime.now(tz=_dt.timezone.utc)

        native_frame = Frame(
            image_bgr=native_bgr,
            width=native_w,
            height=native_h,
            source_mode=mode_used,
            capture_latency_ms=capture_latency_ms,
            capture_ts=capture_ts,
            native_width=native_w,
            native_height=native_h,
        )

        ref_frame = self.remap.apply(native_frame)

        if self.debug:
            self._write_artifacts(payload, ref_frame, mode_used)

        _LOG.debug(
            "capture mode=%s latency=%.1f ms native=%dx%d -> ref=%dx%d",
            mode_used, capture_latency_ms, native_w, native_h,
            ref_frame.width, ref_frame.height,
        )
        return ref_frame

    # ---- mode dispatch ------------------------------------------------

    def _capture_one(self, mode: str) -> bytes:
        if mode == "raw":
            return self._capture_raw()
        if mode == "png":
            return self._capture_png()
        if mode == "pull":
            return self._capture_pull()
        # SUPPORTED_MODES gate above should make this unreachable.
        raise ValueError(f"unreachable: unknown concrete mode {mode!r}")

    def _capture_and_decode_auto(
        self,
    ) -> tuple[bytes, np.ndarray, int, int, str]:
        """First-capture auto path: try modes in order, latch the winner.

        Tries `raw` first, then `png`, then `pull`. For each mode we
        attempt both capture AND decode; a mode is considered to have
        succeeded only if a BGR ndarray is produced. The first
        succeeding mode is latched into `self.active_mode` for the
        rest of the session.

        Per ADR-01a §Decision (2) and DESIGN-REVIEW §9.2, the auto
        sampler is intentionally simple in v1.0: no benchmarking, no
        heuristics, just preference order. The dynamic A/B variant is
        v1.1 backlog row #2.
        """
        last_exc: Exception | None = None
        for mode in CONCRETE_MODES:
            try:
                payload = self._capture_one(mode)
                native_bgr, native_w, native_h = self._decode(payload, mode)
            except (CaptureError, ADBError, FrameDecodeError) as exc:
                _LOG.warning("auto: %s pipeline failed: %s", mode, exc)
                last_exc = exc
                continue
            if mode != "raw":
                _LOG.info(
                    "auto-mode latched on fallback %r (earlier modes failed)", mode
                )
                self._auto_fallback_logged = True
            else:
                _LOG.info("auto-mode latched on %r", mode)
            self.active_mode = mode
            return payload, native_bgr, native_w, native_h, mode
        raise CaptureError(
            "auto-mode: all three capture modes (raw, png, pull) failed; "
            f"last error: {last_exc!r}"
        )

    # ---- mode implementations -----------------------------------------

    def _capture_raw(self) -> bytes:
        try:
            data = self.adb.exec_out(["screencap"], timeout=CAPTURE_TIMEOUT_S)
        except ADBError as exc:
            raise CaptureError(f"adb exec-out screencap (raw) failed: {exc}") from exc
        if not data:
            raise CaptureError("adb exec-out screencap (raw) returned empty buffer")
        return data

    def _capture_png(self) -> bytes:
        try:
            data = self.adb.exec_out(["screencap", "-p"], timeout=CAPTURE_TIMEOUT_S)
        except ADBError as exc:
            raise CaptureError(f"adb exec-out screencap -p (png) failed: {exc}") from exc
        if not data:
            raise CaptureError("adb exec-out screencap -p (png) returned empty buffer")
        return data

    def _capture_pull(self) -> bytes:
        """Legacy `screencap` + `adb pull` round trip.

        Implemented directly with `subprocess` (and a temp file on
        device + host) because the Phase-1 `ADB` wrapper does not yet
        expose `pull`. Promoting `pull` into the wrapper is Phase 1's
        responsibility if and when other callers need it; Phase 2's
        single use does not warrant the wrapper change.
        """
        remote = f"/sdcard/_sensor_{uuid.uuid4().hex}.png"
        local_dir = Path(tempfile.mkdtemp(prefix="sensor_pull_"))
        local_file = local_dir / "frame.png"
        try:
            try:
                self.adb.shell(
                    ["screencap", "-p", remote], timeout=CAPTURE_TIMEOUT_S
                )
            except ADBError as exc:
                raise CaptureError(f"adb shell screencap (pull mode) failed: {exc}") from exc
            try:
                subprocess.run(
                    [self.adb.binary, "pull", remote, str(local_file)],
                    capture_output=True,
                    check=True,
                    timeout=CAPTURE_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired as exc:
                raise CaptureError(f"adb pull timed out: {exc}") from exc
            except subprocess.CalledProcessError as exc:
                stderr = exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
                raise CaptureError(f"adb pull failed: {stderr.strip() or exc!r}") from exc
            if not local_file.is_file():
                raise CaptureError(f"adb pull did not produce {local_file}")
            data = local_file.read_bytes()
            if not data:
                raise CaptureError(f"adb pull produced empty file at {local_file}")
            return data
        finally:
            # Best-effort cleanup; do not raise from finally.
            try:
                self.adb.shell(["rm", remote], timeout=5.0)
            except ADBError:
                pass
            try:
                if local_file.exists():
                    local_file.unlink()
            except OSError:
                pass
            try:
                local_dir.rmdir()
            except OSError:
                pass

    # ---- decode -------------------------------------------------------

    def _decode(self, payload: bytes, mode: str) -> tuple[np.ndarray, int, int]:
        """Decode a capture payload to a native BGR ndarray.

        For `raw` mode this delegates to `parse_raw_screencap`. For
        `png` and `pull` modes it uses `cv2.imdecode`.
        """
        if mode == "raw":
            return parse_raw_screencap(payload)
        # png / pull
        array = np.frombuffer(payload, dtype=np.uint8)
        img = cv2.imdecode(array, cv2.IMREAD_COLOR)
        if img is None:
            raise FrameDecodeError(f"cv2.imdecode returned None on {mode!r} payload "
                                    f"({len(payload)} bytes)")
        if img.ndim != 3 or img.shape[2] != 3:
            raise FrameDecodeError(
                f"decoded image has unexpected shape {img.shape}; expected (H, W, 3)"
            )
        height, width = img.shape[:2]
        # cv2.imdecode returns BGR by default — exactly what Frame wants.
        return img, width, height

    # ---- debug artifacts ---------------------------------------------

    def _write_artifacts(self, payload: bytes, frame: Frame, mode_used: str) -> None:
        """Write per-capture debug artifacts. Best-effort; never raises."""
        try:
            ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = frame.capture_ts.strftime("%Y%m%dT%H%M%S_%f")
            cap_dir = ARTIFACTS_DIR / f"{ts}_{mode_used}_{uuid.uuid4().hex[:8]}"
            cap_dir.mkdir(parents=True, exist_ok=True)

            # Payload file (raw bytes vs png bytes).
            payload_name = "raw.bin" if mode_used == "raw" else "screen.png"
            _atomic_write_bytes(cap_dir / payload_name, payload)

            # Decoded JPEG of the *reference* frame. Encode in memory
            # and atomic-write the bytes so the temp file's extension
            # is not interpreted by cv2.
            ok, jpeg_buf = cv2.imencode(
                ".jpg", frame.image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85]
            )
            if ok:
                _atomic_write_bytes(cap_dir / "frame.jpg", jpeg_buf.tobytes())

            # Metadata.
            metadata = {
                "requested_mode": self.requested_mode,
                "active_mode": self.active_mode,
                "mode_used": mode_used,
                "payload_bytes": len(payload),
                "payload_file": payload_name,
                **frame.to_debug_dict(),
            }
            _atomic_write_bytes(
                cap_dir / "metadata.json",
                (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
            _LOG.debug("wrote sensor artifacts to %s", cap_dir)
        except (OSError, ValueError) as exc:
            # Never let artifact writing crash the sensor.
            _LOG.warning("could not write sensor artifacts: %s", exc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically (tmp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        shutil.move(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
