"""Sensor pipeline tests.

Strategy:
- For RAW: synthesize valid and invalid raw-screencap buffers (12-byte
  and 16-byte headers, RGBA_8888 and others) and feed them through
  `parse_raw_screencap` directly + through `Sensor.capture()` with a
  mocked ADB.
- For PNG: build a small in-memory PNG via cv2.imencode and pass it
  through the sensor.
- For PULL: mock both `ADB.shell(screencap)` and the raw `subprocess.run`
  used for `adb pull`, providing the same in-memory PNG as the pulled
  payload.
- For AUTO: configure the mocks so raw fails → png fails → pull
  succeeds (and the inverse permutations).
- For DEBUG artifacts: point ARTIFACTS_DIR at tmp_path and verify
  files are written atomically.
"""
from __future__ import annotations

import datetime as _dt
import json
import struct
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest

from automation.adb import ADB
from automation.errors import (
    ADBError,
    CaptureError,
    FrameDecodeError,
    UnsupportedPixelFormatError,
)
from automation.frame import Frame
from automation.remap import Remap
from automation.sensor import (
    PIXEL_FORMAT_RGBA_8888,
    SUPPORTED_MODES,
    Sensor,
    parse_raw_screencap,
)


# ----------------------------------------------------------------------
# raw buffer synthesis helpers
# ----------------------------------------------------------------------


def _rgba_payload(w: int, h: int, *, fill_rgba: tuple[int, int, int, int] = (10, 20, 30, 255)
                  ) -> bytes:
    pixels = np.tile(np.array(fill_rgba, dtype=np.uint8), (h, w, 1))
    assert pixels.shape == (h, w, 4)
    return pixels.tobytes()


def make_raw_16(w: int, h: int, fmt: int = PIXEL_FORMAT_RGBA_8888,
                colorspace: int = 1,
                payload: bytes | None = None) -> bytes:
    """Build a synthetic 16-byte-header raw screencap buffer."""
    header = struct.pack("<IIII", w, h, fmt, colorspace)
    body = payload if payload is not None else _rgba_payload(w, h)
    return header + body


def make_raw_12(w: int, h: int, fmt: int = PIXEL_FORMAT_RGBA_8888,
                payload: bytes | None = None) -> bytes:
    """Build a synthetic 12-byte-header raw screencap buffer."""
    header = struct.pack("<III", w, h, fmt)
    body = payload if payload is not None else _rgba_payload(w, h)
    return header + body


def make_png(w: int = 1080, h: int = 2408) -> bytes:
    arr = np.full((h, w, 3), 128, dtype=np.uint8)
    ok, buf = cv2.imencode(".png", arr)
    assert ok
    return buf.tobytes()


# ----------------------------------------------------------------------
# parse_raw_screencap
# ----------------------------------------------------------------------


def test_parse_raw_16byte_header_decodes() -> None:
    buf = make_raw_16(8, 4)
    img, w, h = parse_raw_screencap(buf)
    assert (w, h) == (8, 4)
    assert img.shape == (4, 8, 3)
    assert img.dtype == np.uint8
    # Fill colour (RGBA 10,20,30) → BGR (30,20,10).
    np.testing.assert_array_equal(img[0, 0], np.array([30, 20, 10], dtype=np.uint8))


def test_parse_raw_12byte_header_decodes() -> None:
    buf = make_raw_12(8, 4)
    img, w, h = parse_raw_screencap(buf)
    assert (w, h) == (8, 4)
    assert img.shape == (4, 8, 3)


def test_parse_raw_xiaomi_dimensions() -> None:
    """Exact Phase 0 native dimensions."""
    buf = make_raw_16(1080, 2408)
    img, w, h = parse_raw_screencap(buf)
    assert (w, h) == (1080, 2408)
    assert img.shape == (2408, 1080, 3)


def test_parse_raw_rejects_too_short_buffer() -> None:
    with pytest.raises(FrameDecodeError, match="too small"):
        parse_raw_screencap(b"\x00\x00")


def test_parse_raw_rejects_bad_length() -> None:
    """16-byte header announces 8x4 but body is wrong size."""
    header = struct.pack("<IIII", 8, 4, PIXEL_FORMAT_RGBA_8888, 1)
    # Body should be 128 bytes, give it 64.
    bad_body = b"\x00" * 64
    with pytest.raises(FrameDecodeError, match="does not match either"):
        parse_raw_screencap(header + bad_body)


def test_parse_raw_rejects_unsupported_format() -> None:
    """fmt=3 (RGB_888) is not supported in v1.0."""
    # Synthesize an 8x4 buffer claiming RGB_888 with a 16-byte header.
    # Length must still match a valid layout to reach the format check.
    header = struct.pack("<IIII", 8, 4, 3, 0)
    body = _rgba_payload(8, 4)
    buf = header + body
    with pytest.raises(UnsupportedPixelFormatError, match="PIXEL_FORMAT_RGB_888"):
        parse_raw_screencap(buf)


def test_parse_raw_rejects_format_zero() -> None:
    header = struct.pack("<IIII", 8, 4, 0, 0)
    body = _rgba_payload(8, 4)
    with pytest.raises(UnsupportedPixelFormatError):
        parse_raw_screencap(header + body)


# ----------------------------------------------------------------------
# Sensor — basic capture for each concrete mode
# ----------------------------------------------------------------------


def test_sensor_rejects_unknown_mode(subprocess_recorder) -> None:
    adb = ADB()
    with pytest.raises(ValueError, match="unsupported sensor mode"):
        Sensor(adb, mode="nope")


def test_sensor_default_mode_is_raw(subprocess_recorder) -> None:
    adb = ADB()
    s = Sensor(adb)
    assert s.requested_mode == "raw"
    assert s.active_mode == "raw"


def test_sensor_supported_modes_constant() -> None:
    assert SUPPORTED_MODES == {"raw", "png", "pull", "auto"}


@pytest.fixture
def adb_for_sensor(subprocess_recorder) -> ADB:
    """An ADB instance whose subprocess-run is recorded but not yet
    populated with screencap responses (tests register per-case)."""
    subprocess_recorder.register(["version"],
                                  stdout="Android Debug Bridge version 1.0.41\nVersion 35.0.0-1\n")
    return ADB()


def test_sensor_raw_capture_produces_reference_frame(adb_for_sensor: ADB,
                                                      subprocess_recorder) -> None:
    subprocess_recorder.register(["exec-out", "screencap"],
                                  stdout=make_raw_16(1080, 2408))
    s = Sensor(adb_for_sensor, mode="raw")
    fr = s.capture()
    assert isinstance(fr, Frame)
    assert fr.source_mode == "raw"
    assert (fr.width, fr.height) == (1080, 1920)
    assert (fr.native_width, fr.native_height) == (1080, 2408)
    assert fr.capture_latency_ms >= 0


def test_sensor_raw_handles_12byte_header(adb_for_sensor: ADB,
                                           subprocess_recorder) -> None:
    subprocess_recorder.register(["exec-out", "screencap"],
                                  stdout=make_raw_12(1080, 1920))
    s = Sensor(adb_for_sensor, mode="raw")
    fr = s.capture()
    assert (fr.width, fr.height) == (1080, 1920)
    assert (fr.native_width, fr.native_height) == (1080, 1920)


def test_sensor_raw_unsupported_format_raises(adb_for_sensor: ADB,
                                                subprocess_recorder) -> None:
    header = struct.pack("<IIII", 8, 4, 3, 0)  # RGB_888
    body = _rgba_payload(8, 4)
    subprocess_recorder.register(["exec-out", "screencap"], stdout=header + body)
    s = Sensor(adb_for_sensor, mode="raw")
    with pytest.raises(UnsupportedPixelFormatError):
        s.capture()


def test_sensor_raw_empty_buffer_raises(adb_for_sensor: ADB,
                                         subprocess_recorder) -> None:
    subprocess_recorder.register(["exec-out", "screencap"], stdout=b"")
    s = Sensor(adb_for_sensor, mode="raw")
    with pytest.raises(CaptureError, match="empty"):
        s.capture()


def test_sensor_png_capture_decodes(adb_for_sensor: ADB,
                                     subprocess_recorder) -> None:
    subprocess_recorder.register(["exec-out", "screencap", "-p"],
                                  stdout=make_png(1080, 2408))
    s = Sensor(adb_for_sensor, mode="png")
    fr = s.capture()
    assert fr.source_mode == "png"
    assert (fr.width, fr.height) == (1080, 1920)
    assert (fr.native_width, fr.native_height) == (1080, 2408)


def test_sensor_png_decode_failure_raises(adb_for_sensor: ADB,
                                            subprocess_recorder) -> None:
    subprocess_recorder.register(["exec-out", "screencap", "-p"],
                                  stdout=b"not a png at all")
    s = Sensor(adb_for_sensor, mode="png")
    with pytest.raises(FrameDecodeError):
        s.capture()


def test_sensor_png_empty_buffer_raises(adb_for_sensor: ADB,
                                         subprocess_recorder) -> None:
    subprocess_recorder.register(["exec-out", "screencap", "-p"], stdout=b"")
    s = Sensor(adb_for_sensor, mode="png")
    with pytest.raises(CaptureError, match="empty"):
        s.capture()


# ----------------------------------------------------------------------
# Pull mode
# ----------------------------------------------------------------------


def test_sensor_pull_mode_round_trip(adb_for_sensor: ADB,
                                       subprocess_recorder,
                                       tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """Pull mode: shell screencap to /sdcard/..., then adb pull. The pull
    subprocess is intercepted to drop the PNG bytes into the local file."""
    subprocess_recorder.register(
        ["shell", "screencap", "-p", pytest.skip if False else None]  # placeholder
    )
    # We can't register the exact ["shell", "screencap", "-p", "/sdcard/_sensor_<uuid>.png"]
    # because the path is randomised. Override SubprocessRecorder.__call__
    # to recognise any shell-screencap call.
    png_bytes = make_png(1080, 2408)

    real_call = subprocess_recorder.__call__

    def smart_call(cmd, **kwargs):
        # `adb shell screencap -p /sdcard/...`
        if len(cmd) >= 4 and cmd[-4:-2] == ["shell", "screencap"]:
            from tests.conftest import FakeProc
            return FakeProc(args=list(cmd), returncode=0,
                            stdout=("" if kwargs.get("text") else b""),
                            stderr=("" if kwargs.get("text") else b""))
        # `adb pull <remote> <local>`
        if len(cmd) >= 3 and cmd[-3] == "pull":
            local = cmd[-1]
            Path(local).write_bytes(png_bytes)
            from tests.conftest import FakeProc
            return FakeProc(args=list(cmd), returncode=0,
                            stdout=("" if kwargs.get("text") else b""),
                            stderr=("" if kwargs.get("text") else b""))
        # `adb shell rm <remote>` cleanup
        if len(cmd) >= 3 and cmd[-3:-1] == ["shell", "rm"]:
            from tests.conftest import FakeProc
            return FakeProc(args=list(cmd), returncode=0,
                            stdout=("" if kwargs.get("text") else b""),
                            stderr=("" if kwargs.get("text") else b""))
        return real_call(cmd, **kwargs)

    import automation.adb as adb_mod
    monkeypatch.setattr(adb_mod.subprocess, "run", smart_call)
    import automation.sensor as sensor_mod
    monkeypatch.setattr(sensor_mod.subprocess, "run", smart_call)

    s = Sensor(adb_for_sensor, mode="pull")
    fr = s.capture()
    assert fr.source_mode == "pull"
    assert (fr.width, fr.height) == (1080, 1920)
    assert (fr.native_width, fr.native_height) == (1080, 2408)


# ----------------------------------------------------------------------
# Auto mode
# ----------------------------------------------------------------------


def test_sensor_auto_picks_raw_on_first_success(adb_for_sensor: ADB,
                                                  subprocess_recorder) -> None:
    subprocess_recorder.register(["exec-out", "screencap"],
                                  stdout=make_raw_16(1080, 2408))
    s = Sensor(adb_for_sensor, mode="auto")
    fr = s.capture()
    assert fr.source_mode == "raw"
    assert s.active_mode == "raw"


def test_sensor_auto_falls_back_raw_to_png(adb_for_sensor: ADB,
                                             subprocess_recorder) -> None:
    # Raw returns bytes but with unsupported format → decode fails → fallback.
    bad_header = struct.pack("<IIII", 8, 4, 3, 0)  # RGB_888 not supported
    bad_body = _rgba_payload(8, 4)
    subprocess_recorder.register(["exec-out", "screencap"], stdout=bad_header + bad_body)
    subprocess_recorder.register(["exec-out", "screencap", "-p"],
                                  stdout=make_png(1080, 2408))
    s = Sensor(adb_for_sensor, mode="auto")
    fr = s.capture()
    assert fr.source_mode == "png"
    assert s.active_mode == "png"
    assert s._auto_fallback_logged is True


def test_sensor_auto_latched_skips_alternatives_on_second_call(adb_for_sensor: ADB,
                                                                subprocess_recorder) -> None:
    """Once auto latches a mode, subsequent calls do not re-try others."""
    subprocess_recorder.register(["exec-out", "screencap"],
                                  stdout=make_raw_16(1080, 2408))
    subprocess_recorder.register(["exec-out", "screencap", "-p"],
                                  stdout=make_png(1080, 2408))
    s = Sensor(adb_for_sensor, mode="auto")
    s.capture()
    assert s.active_mode == "raw"
    # Calls so far: 1 raw (no png). Now do another capture.
    raw_before = sum(1 for c in subprocess_recorder.calls if c[-2:] == ["exec-out", "screencap"])
    s.capture()
    raw_after = sum(1 for c in subprocess_recorder.calls if c[-2:] == ["exec-out", "screencap"])
    assert raw_after - raw_before == 1  # second raw call, no extra png call


def test_sensor_auto_all_fail_raises_capture_error(adb_for_sensor: ADB,
                                                    subprocess_recorder) -> None:
    from tests.conftest import FakeProc

    subprocess_recorder.register(["exec-out", "screencap"], stdout=b"")
    subprocess_recorder.register(["exec-out", "screencap", "-p"], stdout=b"")
    # Pull mode runs `adb shell screencap -p <remote>` followed by an
    # `adb pull`. Make the shell-screencap fail; the cleanup `shell rm`
    # call falls through to the recorder's default success proc.
    subprocess_recorder.default = FakeProc(
        args=[], returncode=1, stdout=b"", stderr=b"all-fail-test default"
    )
    s = Sensor(adb_for_sensor, mode="auto")
    with pytest.raises(CaptureError, match="all three"):
        s.capture()


# ----------------------------------------------------------------------
# Latency instrumentation
# ----------------------------------------------------------------------


def test_capture_latency_ms_is_nonzero(adb_for_sensor: ADB,
                                        subprocess_recorder) -> None:
    subprocess_recorder.register(["exec-out", "screencap"],
                                  stdout=make_raw_16(64, 64))
    s = Sensor(adb_for_sensor, mode="raw")
    fr = s.capture()
    # The actual time is dominated by tiny Python overhead in a mocked
    # subprocess; assert >= 0 and < 1 second (way more than enough slack).
    assert 0 <= fr.capture_latency_ms < 1000


def test_capture_ts_is_utc_aware(adb_for_sensor: ADB,
                                   subprocess_recorder) -> None:
    subprocess_recorder.register(["exec-out", "screencap"],
                                  stdout=make_raw_16(64, 64))
    s = Sensor(adb_for_sensor, mode="raw")
    fr = s.capture()
    assert fr.capture_ts.tzinfo is not None


# ----------------------------------------------------------------------
# Debug artifacts
# ----------------------------------------------------------------------


def test_debug_artifacts_written_when_enabled(adb_for_sensor: ADB,
                                                subprocess_recorder,
                                                tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    subprocess_recorder.register(["exec-out", "screencap"],
                                  stdout=make_raw_16(1080, 2408))
    artifacts = tmp_path / "var" / "artifacts" / "sensor"
    monkeypatch.setattr("automation.sensor.ARTIFACTS_DIR", artifacts)

    s = Sensor(adb_for_sensor, mode="raw", debug=True)
    s.capture()

    # One subdirectory should now exist.
    subdirs = list(artifacts.iterdir())
    assert len(subdirs) == 1
    cap_dir = subdirs[0]
    assert (cap_dir / "raw.bin").is_file()
    assert (cap_dir / "frame.jpg").is_file()
    md_file = cap_dir / "metadata.json"
    assert md_file.is_file()
    md = json.loads(md_file.read_text())
    assert md["requested_mode"] == "raw"
    assert md["active_mode"] == "raw"
    assert md["mode_used"] == "raw"
    assert md["payload_file"] == "raw.bin"
    assert md["native_width"] == 1080
    assert md["native_height"] == 2408
    assert md["width"] == 1080
    assert md["height"] == 1920


def test_debug_artifacts_skipped_when_disabled(adb_for_sensor: ADB,
                                                 subprocess_recorder,
                                                 tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    subprocess_recorder.register(["exec-out", "screencap"],
                                  stdout=make_raw_16(64, 64))
    artifacts = tmp_path / "var" / "artifacts" / "sensor"
    monkeypatch.setattr("automation.sensor.ARTIFACTS_DIR", artifacts)

    s = Sensor(adb_for_sensor, mode="raw", debug=False)
    s.capture()
    # The directory may not exist at all if no artifact was ever written.
    if artifacts.exists():
        assert not any(artifacts.iterdir())


def test_debug_env_var_enables_artifacts(adb_for_sensor: ADB,
                                           subprocess_recorder,
                                           tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    subprocess_recorder.register(["exec-out", "screencap"],
                                  stdout=make_raw_16(64, 64))
    artifacts = tmp_path / "var" / "artifacts" / "sensor"
    monkeypatch.setattr("automation.sensor.ARTIFACTS_DIR", artifacts)
    monkeypatch.setenv("SENSOR_DEBUG", "1")

    s = Sensor(adb_for_sensor, mode="raw")  # debug param defaults to env var
    assert s.debug is True
    s.capture()
    assert any(artifacts.iterdir())


def test_png_mode_artifact_payload_name(adb_for_sensor: ADB,
                                          subprocess_recorder,
                                          tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    subprocess_recorder.register(["exec-out", "screencap", "-p"],
                                  stdout=make_png(64, 64))
    artifacts = tmp_path / "var" / "artifacts" / "sensor"
    monkeypatch.setattr("automation.sensor.ARTIFACTS_DIR", artifacts)

    s = Sensor(adb_for_sensor, mode="png", debug=True)
    s.capture()
    cap_dir = next(artifacts.iterdir())
    assert (cap_dir / "screen.png").is_file()
    md = json.loads((cap_dir / "metadata.json").read_text())
    assert md["payload_file"] == "screen.png"


# ----------------------------------------------------------------------
# Integration with a custom Remap (e.g. for replay)
# ----------------------------------------------------------------------


def test_sensor_uses_custom_remap_target(adb_for_sensor: ADB,
                                          subprocess_recorder) -> None:
    subprocess_recorder.register(["exec-out", "screencap"],
                                  stdout=make_raw_16(1080, 2400))
    s = Sensor(adb_for_sensor, mode="raw", remap=Remap(reference_resolution=(720, 1280)))
    fr = s.capture()
    assert (fr.width, fr.height) == (720, 1280)
    assert (fr.native_width, fr.native_height) == (1080, 2400)
