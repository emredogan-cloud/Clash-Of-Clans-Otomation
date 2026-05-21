"""ACT layer — `Actuator` issues `adb shell input` events.

Phase 4 implements the action engine specified by ADR-06 (primary
backend = `adb shell input`) and ADR-09 (integer device pixels at the
edge). The actuator's job is narrow on purpose:

- Accept reference-space coordinates (the same space THINK produced).
- Optionally apply bounded jitter (ADR-15) in reference space.
- Denormalize to device pixels (ADR-04 inverse, delegated to
  `denormalize.Denormalizer`).
- Issue exactly one `adb shell input ...` command.
- Measure ADB-shell wall-clock latency.
- Return an `ActionResult`.

Out of scope (deferred to Phase 5+ explicitly):
- State machine / retries / orchestration / decision logic.
- Action queueing, batching, sequences, combos.
- Per-action validation (`VALIDATING` state).
- Pre/post-action delay windows from action-class envelopes.
- `key` / `text` action classes.
- Async / cancellation support (Phase 5 will rebuild around asyncio).

Debug artifacts: when `ACTUATOR_DEBUG=1` (or `Actuator(debug=True)`),
each action writes a per-invocation directory under
`var/artifacts/actuator/<ts>_<action>_<uuid>/` containing
`metadata.json` (atomic write via `tmp` + rename). No screenshots —
the SENSE layer owns frame artifacts.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import random
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .action_result import ActionResult
from .adb import ADB
from .denormalize import DEFAULT_REFERENCE_RESOLUTION, Denormalizer
from .errors import ADBError, CoordinateError
from .paths import ARTIFACTS

_LOG = logging.getLogger(__name__)

ARTIFACTS_DIR: Path = ARTIFACTS / "actuator"

# Jitter bound in reference-space pixels (uniform, symmetric).
JITTER_RANGE_PX: int = 3

# Wall-clock timeout (seconds) for the `adb shell input ...` subprocess.
# Comfortably above the engineering estimate in SYSTEM-ROADMAP §5.4.1
# ("80–250 ms" for `tap`, "80–500 ms" for `swipe`) plus the longest
# `long_press` hold duration we expect in v1.0 (~2 s). USB hub failures
# can extend this; the timeout is the structural ceiling.
ACTION_TIMEOUT_S: float = 10.0


def _parse_bool_env(var: str, default: bool = False) -> bool:
    raw = os.environ.get(var)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


class Actuator:
    """Issue input events to a connected Android device via ADB.

    Public surface (Phase 4):

    - `tap(x, y, native_width, native_height, *, jitter=False) -> ActionResult`
    - `swipe(x1, y1, x2, y2, native_width, native_height, *, duration_ms=300, jitter=False) -> ActionResult`
    - `long_press(x, y, native_width, native_height, *, duration_ms=600, jitter=False) -> ActionResult`

    All coordinate inputs are in the v1.0 reference frame (1080×1920
    by default). The actuator denormalizes once per call against the
    caller-supplied native dimensions; native dimensions are
    typically obtained from `Frame.native_width` / `Frame.native_height`.

    Constructor params:

    - `adb`         : an `ADB` instance.
    - `denormalizer`: an optional `Denormalizer`. Defaults to the
                      v1.0 reference 1080×1920.
    - `seed`        : optional integer to seed the jitter RNG. If
                      `None`, a fresh `random.Random()` is constructed
                      (non-deterministic). Tests pass an integer for
                      reproducibility.
    - `debug`       : write per-action artifacts to
                      `var/artifacts/actuator/`. If `None`, the
                      `ACTUATOR_DEBUG` env var is consulted at
                      construction time only (ADR-13 — no runtime
                      mutation of config).

    Threading: the actuator holds no mutable per-call state on the
    instance apart from the RNG (which is itself thread-unsafe in
    `random.Random`). Phase 4 callers are single-threaded; Phase 5
    rebuilds for asyncio.
    """

    def __init__(
        self,
        adb: ADB,
        *,
        denormalizer: Denormalizer | None = None,
        seed: int | None = None,
        debug: bool | None = None,
    ) -> None:
        self.adb: ADB = adb
        self.denormalizer: Denormalizer = (
            denormalizer if denormalizer is not None
            else Denormalizer(DEFAULT_REFERENCE_RESOLUTION)
        )
        self._rng: random.Random = random.Random(seed)
        self.seed: int | None = seed
        self.debug: bool = (
            debug if debug is not None else _parse_bool_env("ACTUATOR_DEBUG")
        )

    # ---- public API ---------------------------------------------------

    def tap(
        self,
        x: float,
        y: float,
        native_width: int,
        native_height: int,
        *,
        jitter: bool = False,
    ) -> ActionResult:
        """Issue a single tap at reference-space `(x, y)`.

        Steps:
        1. Validate inputs (defensive; the denormalizer also validates).
        2. Optionally sample bounded uniform jitter in reference space.
        3. Denormalize to device pixels.
        4. Time-and-run `adb shell input tap <X> <Y>`.
        5. Build and return `ActionResult`. Optionally persist artifacts.
        """
        x_eff, y_eff = self._maybe_jitter(x, y, jitter)
        device_x, device_y = self.denormalizer.to_native(
            x_eff, y_eff, native_width, native_height
        )
        cmd = ["input", "tap", str(device_x), str(device_y)]
        success, latency_ms = self._invoke_adb(cmd)
        result = self._build_result(
            action_type="tap",
            success=success,
            latency_ms=latency_ms,
            device_x=device_x,
            device_y=device_y,
        )
        if self.debug:
            self._write_artifacts(
                result=result,
                cmd=cmd,
                jitter_used=jitter,
                ref_anchor=(x, y),
                ref_anchor_jittered=(x_eff, y_eff),
                native_size=(native_width, native_height),
                extras={},
            )
        _LOG.debug(
            "tap ref=(%.2f,%.2f) eff=(%.2f,%.2f) native=(%d,%d) latency=%.2f ms success=%s",
            x, y, x_eff, y_eff, device_x, device_y, latency_ms, success,
        )
        return result

    def swipe(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        native_width: int,
        native_height: int,
        *,
        duration_ms: int = 300,
        jitter: bool = False,
    ) -> ActionResult:
        """Issue a swipe from reference-space `(x1, y1)` to `(x2, y2)`.

        `duration_ms` is the swipe duration as passed to
        `adb shell input swipe`. Must be > 0.

        Jitter is applied independently to start and end coordinates
        (bounded ±`JITTER_RANGE_PX` per axis).

        The returned `ActionResult` reports the *start* device-pixel as
        `device_x` / `device_y`. The full start/end pair is preserved
        in artifact metadata when `debug=True`.
        """
        if not isinstance(duration_ms, int) or isinstance(duration_ms, bool):
            raise CoordinateError(
                f"duration_ms must be int, got {type(duration_ms).__name__}"
            )
        if duration_ms <= 0:
            raise CoordinateError(
                f"duration_ms must be > 0, got {duration_ms}"
            )

        x1_eff, y1_eff = self._maybe_jitter(x1, y1, jitter)
        x2_eff, y2_eff = self._maybe_jitter(x2, y2, jitter)
        start_native = self.denormalizer.to_native(
            x1_eff, y1_eff, native_width, native_height
        )
        end_native = self.denormalizer.to_native(
            x2_eff, y2_eff, native_width, native_height
        )
        cmd = [
            "input", "swipe",
            str(start_native[0]), str(start_native[1]),
            str(end_native[0]), str(end_native[1]),
            str(duration_ms),
        ]
        success, latency_ms = self._invoke_adb(cmd)
        result = self._build_result(
            action_type="swipe",
            success=success,
            latency_ms=latency_ms,
            device_x=start_native[0],
            device_y=start_native[1],
        )
        if self.debug:
            self._write_artifacts(
                result=result,
                cmd=cmd,
                jitter_used=jitter,
                ref_anchor=(x1, y1),
                ref_anchor_jittered=(x1_eff, y1_eff),
                native_size=(native_width, native_height),
                extras={
                    "ref_end": [float(x2), float(y2)],
                    "ref_end_jittered": [float(x2_eff), float(y2_eff)],
                    "device_end_x": end_native[0],
                    "device_end_y": end_native[1],
                    "duration_ms": duration_ms,
                },
            )
        _LOG.debug(
            "swipe ref=(%.2f,%.2f)→(%.2f,%.2f) native=(%d,%d)→(%d,%d) "
            "dur=%d latency=%.2f ms success=%s",
            x1, y1, x2, y2,
            start_native[0], start_native[1], end_native[0], end_native[1],
            duration_ms, latency_ms, success,
        )
        return result

    def long_press(
        self,
        x: float,
        y: float,
        native_width: int,
        native_height: int,
        *,
        duration_ms: int = 600,
        jitter: bool = False,
    ) -> ActionResult:
        """Issue a long-press at reference-space `(x, y)` for `duration_ms`.

        Implemented as a zero-distance swipe (`input swipe X Y X Y dur`)
        per the SYSTEM-ROADMAP §5.4.1 action-class table — this is the
        standard `adb shell input` idiom for a hold.

        The returned `ActionResult` has `action_type = "long_press"`
        (not `"swipe"`).
        """
        if not isinstance(duration_ms, int) or isinstance(duration_ms, bool):
            raise CoordinateError(
                f"duration_ms must be int, got {type(duration_ms).__name__}"
            )
        if duration_ms <= 0:
            raise CoordinateError(
                f"duration_ms must be > 0, got {duration_ms}"
            )

        x_eff, y_eff = self._maybe_jitter(x, y, jitter)
        device_x, device_y = self.denormalizer.to_native(
            x_eff, y_eff, native_width, native_height
        )
        cmd = [
            "input", "swipe",
            str(device_x), str(device_y),
            str(device_x), str(device_y),
            str(duration_ms),
        ]
        success, latency_ms = self._invoke_adb(cmd)
        result = self._build_result(
            action_type="long_press",
            success=success,
            latency_ms=latency_ms,
            device_x=device_x,
            device_y=device_y,
        )
        if self.debug:
            self._write_artifacts(
                result=result,
                cmd=cmd,
                jitter_used=jitter,
                ref_anchor=(x, y),
                ref_anchor_jittered=(x_eff, y_eff),
                native_size=(native_width, native_height),
                extras={"duration_ms": duration_ms},
            )
        _LOG.debug(
            "long_press ref=(%.2f,%.2f) native=(%d,%d) dur=%d latency=%.2f ms success=%s",
            x, y, device_x, device_y, duration_ms, latency_ms, success,
        )
        return result

    # ---- internals ----------------------------------------------------

    def _maybe_jitter(
        self, x: float, y: float, jitter: bool,
    ) -> tuple[float, float]:
        """Apply bounded uniform reference-space jitter if requested.

        Distribution: uniform in `[-JITTER_RANGE_PX, +JITTER_RANGE_PX]`
        on each axis, sampled independently. The result is clamped to
        the open interval `[0, reference_dim)` so a jittered
        coordinate at the screen edge cannot escape the reference
        frame.
        """
        if not jitter:
            return float(x), float(y)
        dx = self._rng.uniform(-JITTER_RANGE_PX, +JITTER_RANGE_PX)
        dy = self._rng.uniform(-JITTER_RANGE_PX, +JITTER_RANGE_PX)
        # Clamp to the half-open reference frame; use a small epsilon
        # less than 1 so the result is still < reference_dim after the
        # denormalizer's exclusive upper-bound check.
        ref_w = float(self.denormalizer.reference_width)
        ref_h = float(self.denormalizer.reference_height)
        x_eff = min(max(float(x) + dx, 0.0), ref_w - 1.0)
        y_eff = min(max(float(y) + dy, 0.0), ref_h - 1.0)
        return x_eff, y_eff

    def _invoke_adb(self, args: list[str]) -> tuple[bool, float]:
        """Execute `adb shell <args>` and return `(success, latency_ms)`.

        Wall-clock latency is measured with `perf_counter_ns()` around
        the ADB shell invocation only — no denormalization or
        artifact-write cost is included.

        A non-zero ADB exit is captured as `success=False`; the
        invocation does not raise. This lets callers (Phase 5+)
        observe failures uniformly via `ActionResult.success`. Truly
        malformed inputs (e.g. a coordinate out of range) raise
        `CoordinateError` *before* reaching this method.
        """
        t0 = time.perf_counter_ns()
        success = True
        try:
            self.adb.shell(args, timeout=ACTION_TIMEOUT_S)
        except ADBError as exc:
            success = False
            _LOG.warning("adb shell %s failed: %s", " ".join(args), exc)
        t1 = time.perf_counter_ns()
        return success, (t1 - t0) / 1e6

    def _build_result(
        self,
        *,
        action_type: str,
        success: bool,
        latency_ms: float,
        device_x: int,
        device_y: int,
    ) -> ActionResult:
        return ActionResult(
            success=success,
            action_type=action_type,
            latency_ms=latency_ms,
            device_x=device_x,
            device_y=device_y,
            ts=_dt.datetime.now(tz=_dt.timezone.utc),
        )

    # ---- debug artifacts ---------------------------------------------

    def _write_artifacts(
        self,
        *,
        result: ActionResult,
        cmd: list[str],
        jitter_used: bool,
        ref_anchor: tuple[float, float],
        ref_anchor_jittered: tuple[float, float],
        native_size: tuple[int, int],
        extras: dict[str, Any],
    ) -> None:
        """Write per-action metadata.json. Best-effort; never raises.

        Schema (one file per call, atomic write):

            {
              "action": "tap" | "swipe" | "long_press",
              "success": bool,
              "ts": "ISO 8601",
              "latency_ms": float,
              "jitter_used": bool,
              "jitter_range_px": 3,
              "reference_resolution": [W, H],
              "native_resolution": [W, H],
              "ref_anchor": [x, y],
              "ref_anchor_jittered": [x_eff, y_eff],
              "device_anchor": [device_x, device_y],
              "adb_command": ["input", "tap", "X", "Y"],
              ...action-specific extras
            }
        """
        try:
            ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = result.ts.strftime("%Y%m%dT%H%M%S_%f")
            cap_dir = ARTIFACTS_DIR / (
                f"{ts}_{result.action_type}_{uuid.uuid4().hex[:8]}"
            )
            cap_dir.mkdir(parents=True, exist_ok=True)

            metadata: dict[str, Any] = {
                "action": result.action_type,
                "success": result.success,
                "ts": result.ts.isoformat(),
                "latency_ms": float(result.latency_ms),
                "jitter_used": bool(jitter_used),
                "jitter_range_px": JITTER_RANGE_PX,
                "reference_resolution": [
                    self.denormalizer.reference_width,
                    self.denormalizer.reference_height,
                ],
                "native_resolution": [int(native_size[0]), int(native_size[1])],
                "ref_anchor": [float(ref_anchor[0]), float(ref_anchor[1])],
                "ref_anchor_jittered": [
                    float(ref_anchor_jittered[0]),
                    float(ref_anchor_jittered[1]),
                ],
                "device_anchor": [int(result.device_x), int(result.device_y)]
                if result.device_x is not None and result.device_y is not None
                else None,
                "adb_command": ["shell", *cmd],
            }
            metadata.update(extras)

            _atomic_write_bytes(
                cap_dir / "metadata.json",
                (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
            _LOG.debug("wrote actuator artifacts to %s", cap_dir)
        except (OSError, ValueError) as exc:
            _LOG.warning("could not write actuator artifacts: %s", exc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write `data` to `path` atomically (tmp file + rename + fsync).

    Mirrors the helper in `sensor.py` / `matcher.py`; consolidated into
    a private helper rather than imported to keep the ACT module free
    of cross-layer dependencies. If a future refactor unifies these
    into `paths.py`, replace the three copies.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
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


__all__ = ["Actuator", "ARTIFACTS_DIR", "JITTER_RANGE_PX", "ACTION_TIMEOUT_S"]
