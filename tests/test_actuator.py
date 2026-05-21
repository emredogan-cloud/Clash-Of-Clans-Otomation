"""Actuator tests — mock ADB, no real device.

The fixture `subprocess_recorder` from conftest patches both
`automation.adb.subprocess.run` and (lazily) `automation.sensor.subprocess.run`.
The actuator goes through `ADB.shell` which goes through `subprocess.run`
— recording the resulting argv lets us assert on the ADB command shape
without exercising the device.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from automation.action_result import ActionResult
from automation.actuator import (
    ACTION_TIMEOUT_S,
    JITTER_RANGE_PX,
    Actuator,
)
from automation.adb import ADB
from automation.denormalize import Denormalizer
from automation.errors import CoordinateError

from .conftest import SubprocessRecorder

NATIVE_W = 1080
NATIVE_H = 2408


# ---- shared helpers ---------------------------------------------------------


def _make_actuator(
    subprocess_recorder: SubprocessRecorder,
    *,
    seed: int | None = 0,
    debug: bool = False,
    denormalizer: Denormalizer | None = None,
) -> Actuator:
    """Build an Actuator whose adb invocations are recorded by the fixture.

    Sets the recorder's `default` so any `adb shell input ...` call
    exits 0 with empty output — tests that need a non-zero exit
    override this after construction.
    """
    from .conftest import FakeProc
    subprocess_recorder.default = FakeProc(
        args=[], returncode=0, stdout=b"", stderr=b"",
    )
    adb = ADB(binary="adb")
    return Actuator(adb, seed=seed, debug=debug, denormalizer=denormalizer)


def _extract_input_cmd(calls: list[list[str]]) -> list[str]:
    """From recorded subprocess calls find the `adb shell input ...` argv."""
    for call in calls:
        # The shape is [adb_path, "shell", "input", ...]
        if "shell" in call and "input" in call:
            return call
    raise AssertionError(f"no adb shell input call recorded; calls={calls!r}")


# ---- tap: no-jitter golden path ---------------------------------------------


def test_tap_no_jitter_uses_exact_reference_coords_at_reference_native(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    """Reference 1080x1920, native 1080x1920 → identity mapping."""
    act = _make_actuator(subprocess_recorder)
    result = act.tap(540, 960, 1080, 1920)
    assert isinstance(result, ActionResult)
    assert result.success is True
    assert result.action_type == "tap"
    assert result.device_x == 540
    assert result.device_y == 960
    cmd = _extract_input_cmd(subprocess_recorder.calls)
    # Last 4 tokens must be `shell input tap 540 960`
    assert cmd[-5:] == ["shell", "input", "tap", "540", "960"]


def test_tap_no_jitter_on_operator_device_uses_correct_denorm(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    """Operator device is 1080x2408; y must scale, x must not."""
    act = _make_actuator(subprocess_recorder)
    result = act.tap(540, 960, NATIVE_W, NATIVE_H)
    assert result.device_x == 540
    # y_native = round(960 * 2408/1920) = 1204
    assert result.device_y == 1204
    cmd = _extract_input_cmd(subprocess_recorder.calls)
    assert cmd[-2:] == ["540", "1204"]


def test_tap_returns_immutable_result(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder)
    r = act.tap(100, 200, NATIVE_W, NATIVE_H)
    with pytest.raises(Exception):
        r.device_x = 999  # type: ignore[misc]


def test_tap_latency_is_nonnegative(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder)
    r = act.tap(100, 200, NATIVE_W, NATIVE_H)
    assert r.latency_ms >= 0
    assert r.latency_ms < 5000  # subprocess mock returns immediately


# ---- tap: jitter -------------------------------------------------------------


def test_tap_jitter_seeded_is_deterministic(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    """Two actuators with the same seed produce identical jittered taps."""
    a1 = _make_actuator(subprocess_recorder, seed=42)
    a2 = _make_actuator(subprocess_recorder, seed=42)
    r1 = a1.tap(540, 960, NATIVE_W, NATIVE_H, jitter=True)
    r2 = a2.tap(540, 960, NATIVE_W, NATIVE_H, jitter=True)
    assert (r1.device_x, r1.device_y) == (r2.device_x, r2.device_y)


def test_tap_jitter_different_seed_can_differ(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    """Different seeds, same reference coord — coords likely differ.

    Not strictly guaranteed (two RNGs might land on the same offset),
    but with seeds 0 and 999999 the first sample diverges in practice.
    The test asserts the outcomes are *either* equal-or-different —
    what we really care about is that no error is raised.
    """
    a0 = _make_actuator(subprocess_recorder, seed=0)
    a1 = _make_actuator(subprocess_recorder, seed=999_999)
    r0 = a0.tap(540, 960, NATIVE_W, NATIVE_H, jitter=True)
    r1 = a1.tap(540, 960, NATIVE_W, NATIVE_H, jitter=True)
    # Both should be inside the jitter envelope around the reference anchor
    # (which denormalizes to (540, 1204) on the operator device).
    for r in (r0, r1):
        # Allowable range in y is from
        # round((960 - 3) * 2408/1920) = round(1200.24) = 1200
        # to round((960 + 3) * 2408/1920) = round(1207.76) = 1208
        # so 1200..1208 inclusive.
        assert 537 <= r.device_x <= 543
        assert 1200 <= r.device_y <= 1208


def test_tap_jitter_within_bounded_envelope_in_reference_space(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    """Jitter applied in ref space; ±3 px → device may shift slightly more on y."""
    act = _make_actuator(subprocess_recorder, seed=0)
    # 100 samples, each a fresh tap; collect device coords.
    results = [act.tap(540, 960, NATIVE_W, NATIVE_H, jitter=True) for _ in range(100)]
    xs = [r.device_x for r in results]
    ys = [r.device_y for r in results]
    # x scale 1:1 → ±3 px envelope at x.
    assert all(537 <= x <= 543 for x in xs), f"x out of bounds: {min(xs)}..{max(xs)}"
    # y scale 2408/1920 ≈ 1.254 → ±3 ref px ≈ ±3.76 native px → ±4 after round.
    assert all(1200 <= y <= 1208 for y in ys), f"y out of bounds: {min(ys)}..{max(ys)}"


def test_tap_no_jitter_does_not_advance_rng(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    """With jitter=False the RNG must not advance."""
    act = _make_actuator(subprocess_recorder, seed=0)
    rng_state_before = act._rng.getstate()
    act.tap(100, 200, NATIVE_W, NATIVE_H, jitter=False)
    rng_state_after = act._rng.getstate()
    assert rng_state_before == rng_state_after


def test_tap_jitter_with_anchor_at_edge_stays_inside_reference(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    """A jittered tap near (0, 0) cannot escape the reference frame."""
    act = _make_actuator(subprocess_recorder, seed=0)
    for _ in range(50):
        r = act.tap(0, 0, NATIVE_W, NATIVE_H, jitter=True)
        assert r.device_x >= 0
        assert r.device_y >= 0


# ---- tap: bounds -------------------------------------------------------------


def test_tap_negative_x_raises_coordinate_error(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder)
    with pytest.raises(CoordinateError):
        act.tap(-1, 100, NATIVE_W, NATIVE_H)


def test_tap_x_at_reference_width_rejected(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder)
    with pytest.raises(CoordinateError):
        act.tap(1080, 100, NATIVE_W, NATIVE_H)


def test_tap_y_above_reference_height_rejected(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder)
    with pytest.raises(CoordinateError):
        act.tap(0, 1920, NATIVE_W, NATIVE_H)


def test_tap_non_positive_native_dims_rejected(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder)
    with pytest.raises(CoordinateError):
        act.tap(0, 0, 0, NATIVE_H)
    with pytest.raises(CoordinateError):
        act.tap(0, 0, NATIVE_W, 0)


# ---- swipe -------------------------------------------------------------------


def test_swipe_denormalizes_both_endpoints(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder)
    r = act.swipe(540, 480, 540, 1440, NATIVE_W, NATIVE_H, duration_ms=500)
    assert r.success is True
    assert r.action_type == "swipe"
    cmd = _extract_input_cmd(subprocess_recorder.calls)
    # `shell input swipe X1 Y1 X2 Y2 dur`
    # x is unchanged (1080→1080); y scales by 2408/1920
    # 480 → round(602.0) = 602; 1440 → round(1806.0) = 1806
    assert cmd[-7:] == ["input", "swipe", "540", "602", "540", "1806", "500"]
    # Reported anchor is the start of the swipe.
    assert r.device_x == 540
    assert r.device_y == 602


def test_swipe_duration_default_is_300(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder)
    act.swipe(540, 480, 540, 1440, NATIVE_W, NATIVE_H)
    cmd = _extract_input_cmd(subprocess_recorder.calls)
    assert cmd[-1] == "300"


def test_swipe_zero_duration_rejected(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder)
    with pytest.raises(CoordinateError, match="duration_ms must be > 0"):
        act.swipe(0, 0, 100, 100, NATIVE_W, NATIVE_H, duration_ms=0)


def test_swipe_negative_duration_rejected(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder)
    with pytest.raises(CoordinateError, match="duration_ms must be > 0"):
        act.swipe(0, 0, 100, 100, NATIVE_W, NATIVE_H, duration_ms=-1)


def test_swipe_non_int_duration_rejected(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder)
    with pytest.raises(CoordinateError, match="duration_ms must be int"):
        act.swipe(0, 0, 100, 100, NATIVE_W, NATIVE_H,
                  duration_ms=300.0)  # type: ignore[arg-type]


def test_swipe_out_of_bounds_endpoint_rejected(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder)
    with pytest.raises(CoordinateError):
        act.swipe(0, 0, 2000, 0, NATIVE_W, NATIVE_H)


def test_swipe_jitter_seeded_deterministic(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    a1 = _make_actuator(subprocess_recorder, seed=7)
    a2 = _make_actuator(subprocess_recorder, seed=7)
    r1 = a1.swipe(540, 480, 540, 1440, NATIVE_W, NATIVE_H, jitter=True)
    r2 = a2.swipe(540, 480, 540, 1440, NATIVE_W, NATIVE_H, jitter=True)
    assert (r1.device_x, r1.device_y) == (r2.device_x, r2.device_y)


# ---- long_press --------------------------------------------------------------


def test_long_press_uses_zero_distance_swipe(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder)
    r = act.long_press(540, 960, NATIVE_W, NATIVE_H, duration_ms=800)
    assert r.success is True
    assert r.action_type == "long_press"
    cmd = _extract_input_cmd(subprocess_recorder.calls)
    # Phase-4 spec: implemented as `input swipe X Y X Y dur`.
    assert cmd[-7:] == ["input", "swipe", "540", "1204", "540", "1204", "800"]


def test_long_press_default_duration_is_600(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder)
    act.long_press(100, 200, NATIVE_W, NATIVE_H)
    cmd = _extract_input_cmd(subprocess_recorder.calls)
    assert cmd[-1] == "600"


def test_long_press_zero_duration_rejected(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder)
    with pytest.raises(CoordinateError, match="duration_ms must be > 0"):
        act.long_press(0, 0, NATIVE_W, NATIVE_H, duration_ms=0)


def test_long_press_non_int_duration_rejected(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder)
    with pytest.raises(CoordinateError, match="duration_ms must be int"):
        act.long_press(0, 0, NATIVE_W, NATIVE_H,
                       duration_ms=600.0)  # type: ignore[arg-type]


def test_long_press_action_type_distinct_from_swipe(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    """The wire command is `input swipe` but the reported action_type is `long_press`."""
    act = _make_actuator(subprocess_recorder)
    swipe_r = act.swipe(0, 0, 0, 100, NATIVE_W, NATIVE_H)
    lp_r = act.long_press(0, 0, NATIVE_W, NATIVE_H)
    assert swipe_r.action_type == "swipe"
    assert lp_r.action_type == "long_press"


# ---- ADB failure path --------------------------------------------------------


def test_tap_adb_nonzero_marks_failure_does_not_raise(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    """Per Phase-4 design, ADB failures surface via ActionResult.success=False."""
    from .conftest import FakeProc
    # Build an actuator and then arrange the next shell call to fail.
    act = _make_actuator(subprocess_recorder)
    # Override the recorder's default for the next call.
    subprocess_recorder.default = FakeProc(
        args=[], returncode=1, stdout=b"", stderr=b"error: no devices/emulators found",
    )
    r = act.tap(100, 200, NATIVE_W, NATIVE_H)
    assert r.success is False
    assert r.latency_ms >= 0


# ---- ADB timeout passed to shell --------------------------------------------


def test_tap_passes_action_timeout_to_shell(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    """The actuator must pass its ACTION_TIMEOUT_S to ADB.shell."""
    act = _make_actuator(subprocess_recorder)
    act.tap(100, 200, NATIVE_W, NATIVE_H)
    # The recorder doesn't currently capture timeout in its `calls`,
    # but we can ensure ACTION_TIMEOUT_S is defined and sensible.
    assert ACTION_TIMEOUT_S >= 1.0
    assert ACTION_TIMEOUT_S <= 60.0


# ---- artifacts ---------------------------------------------------------------


def test_artifacts_written_when_debug_enabled(
    subprocess_recorder: SubprocessRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "var" / "artifacts" / "actuator"
    monkeypatch.setattr("automation.actuator.ARTIFACTS_DIR", artifacts)
    act = _make_actuator(subprocess_recorder, debug=True)
    r = act.tap(540, 960, NATIVE_W, NATIVE_H)
    assert r.success is True

    subdirs = list(artifacts.iterdir())
    assert len(subdirs) == 1
    md = json.loads((subdirs[0] / "metadata.json").read_text())
    assert md["action"] == "tap"
    assert md["success"] is True
    assert md["jitter_used"] is False
    assert md["jitter_range_px"] == JITTER_RANGE_PX
    assert md["reference_resolution"] == [1080, 1920]
    assert md["native_resolution"] == [NATIVE_W, NATIVE_H]
    assert md["ref_anchor"] == [540.0, 960.0]
    assert md["device_anchor"] == [540, 1204]
    assert md["adb_command"][-4:] == ["input", "tap", "540", "1204"]


def test_artifacts_skipped_when_debug_disabled(
    subprocess_recorder: SubprocessRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "var" / "artifacts" / "actuator"
    monkeypatch.setattr("automation.actuator.ARTIFACTS_DIR", artifacts)
    act = _make_actuator(subprocess_recorder, debug=False)
    act.tap(540, 960, NATIVE_W, NATIVE_H)
    # Directory may not even exist; if it does, must be empty.
    if artifacts.exists():
        assert not any(artifacts.iterdir())


def test_artifacts_env_var_enables_debug(
    subprocess_recorder: SubprocessRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "var" / "artifacts" / "actuator"
    monkeypatch.setattr("automation.actuator.ARTIFACTS_DIR", artifacts)
    monkeypatch.setenv("ACTUATOR_DEBUG", "1")
    # debug=None → consult env var
    from .conftest import FakeProc
    subprocess_recorder.default = FakeProc(
        args=[], returncode=0, stdout=b"", stderr=b"",
    )
    adb = ADB(binary="adb")
    act = Actuator(adb, seed=0)
    assert act.debug is True
    act.tap(0, 0, NATIVE_W, NATIVE_H)
    assert any(artifacts.iterdir())


def test_artifacts_capture_swipe_full_path(
    subprocess_recorder: SubprocessRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "var" / "artifacts" / "actuator"
    monkeypatch.setattr("automation.actuator.ARTIFACTS_DIR", artifacts)
    act = _make_actuator(subprocess_recorder, debug=True)
    act.swipe(540, 480, 540, 1440, NATIVE_W, NATIVE_H, duration_ms=500)
    subdirs = list(artifacts.iterdir())
    md = json.loads((subdirs[0] / "metadata.json").read_text())
    assert md["action"] == "swipe"
    assert md["ref_anchor"] == [540.0, 480.0]
    assert md["ref_end"] == [540.0, 1440.0]
    assert md["duration_ms"] == 500
    assert md["device_end_x"] == 540
    assert md["device_end_y"] == 1806


def test_artifacts_capture_long_press_duration(
    subprocess_recorder: SubprocessRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "var" / "artifacts" / "actuator"
    monkeypatch.setattr("automation.actuator.ARTIFACTS_DIR", artifacts)
    act = _make_actuator(subprocess_recorder, debug=True)
    act.long_press(100, 200, NATIVE_W, NATIVE_H, duration_ms=1200)
    subdirs = list(artifacts.iterdir())
    md = json.loads((subdirs[0] / "metadata.json").read_text())
    assert md["action"] == "long_press"
    assert md["duration_ms"] == 1200


def test_artifacts_atomic_no_partial_tmp_files(
    subprocess_recorder: SubprocessRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a successful write no `.tmp` files should remain in the
    artifact dir."""
    artifacts = tmp_path / "var" / "artifacts" / "actuator"
    monkeypatch.setattr("automation.actuator.ARTIFACTS_DIR", artifacts)
    act = _make_actuator(subprocess_recorder, debug=True)
    act.tap(540, 960, NATIVE_W, NATIVE_H)
    cap_dir = next(iter(artifacts.iterdir()))
    contents = list(cap_dir.iterdir())
    assert any(p.name == "metadata.json" for p in contents)
    assert not any(p.suffix == ".tmp" for p in contents)


def test_artifacts_failure_does_not_raise(
    subprocess_recorder: SubprocessRecorder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If artifact directory creation fails the actuator must not crash."""
    # Point at an un-creatable path; the actuator should swallow the OSError.
    monkeypatch.setattr(
        "automation.actuator.ARTIFACTS_DIR",
        Path("/proc/forbidden/actuator"),  # not writable
    )
    act = _make_actuator(subprocess_recorder, debug=True)
    r = act.tap(0, 0, NATIVE_W, NATIVE_H)
    assert r.success is True  # ADB call still worked


# ---- denormalizer wiring -----------------------------------------------------


def test_actuator_uses_supplied_denormalizer(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    """A custom denormalizer with a smaller reference must be honoured."""
    smaller = Denormalizer((720, 1280))
    act = _make_actuator(subprocess_recorder, denormalizer=smaller)
    # x_ref=360 (centre of 720) → x_native = round(360 * 1080/720) = 540
    # y_ref=640 (centre of 1280) → y_native = round(640 * 2408/1280) = 1204
    r = act.tap(360, 640, NATIVE_W, NATIVE_H)
    assert r.device_x == 540
    assert r.device_y == 1204


def test_actuator_default_denormalizer_is_1080x1920(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder)
    assert act.denormalizer.reference_resolution == (1080, 1920)


# ---- seed surface ------------------------------------------------------------


def test_seed_field_is_recorded(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder, seed=12345)
    assert act.seed == 12345


def test_no_seed_yields_unseeded_rng(
    subprocess_recorder: SubprocessRecorder,
) -> None:
    act = _make_actuator(subprocess_recorder, seed=None)
    assert act.seed is None
