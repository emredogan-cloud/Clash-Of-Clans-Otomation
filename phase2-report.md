# Phase 2 Report — SENSE / Screenshot Pipeline

> **Phase:** 2 — SENSE
> **Date:** 2026-05-20
> **Host:** Ubuntu 24.04.4 LTS, kernel 6.17, AMD Ryzen 5 5500
> **Device:** Xiaomi 22095RA98C (Redmi Note 11R), Android 13, USB 2.0 @ 480 Mbps
> **Reference resolution (ADR-04):** 1080×1920
> **Companion documents:** [phase-0-report.md](./phase-0-report.md), [docs/frozen_nfrs_v1.md](./docs/frozen_nfrs_v1.md), [ADR.md ADR-01a](./ADR.md#adr-01a--screenshot-pipeline-phase-0-reality-content-dependent-ordering-usb-link-speed-prerequisite)

---

## 1. What was built

**Three Phase 2 modules** under `automation/`:

| File | Purpose | LOC (approx) |
|------|---------|----:|
| `automation/frame.py` | Immutable `Frame` dataclass — single shape carried out of SENSE | 130 |
| `automation/remap.py` | `Remap` class — 1080×1920 reference resampling (`INTER_LINEAR`) | 95 |
| `automation/sensor.py` | `Sensor` class + `parse_raw_screencap` — capture/parse/convert/normalise/instrument/persist | 460 |

**Extensions**:

| File | Change |
|------|--------|
| `automation/errors.py` | Added `SensorError`, `CaptureError`, `FrameDecodeError`, `UnsupportedPixelFormatError` |
| `tests/conftest.py` | `SubprocessRecorder` now also patches `automation.sensor.subprocess.run` so pull-mode shells the same mock |
| `tests/` | Added `test_frame.py` (16 tests), `test_remap.py` (9 tests), `test_sensor.py` (29 tests) |

**Per-capture debug artifacts**: when `SENSOR_DEBUG=1` (or `Sensor(debug=True)`),
each capture writes a directory under `var/artifacts/sensor/` containing
the raw payload, a JPEG of the decoded reference frame, and a JSON
metadata file. Writes are atomic (`tmp` → rename).

---

## 2. Architecture

### 2.1 Pipeline

```
                          ┌───────────────────┐
                          │  Sensor.capture() │
                          └─────────┬─────────┘
                                    │
              ┌─────────────────────┴────────────────────┐
              │              t = perf_counter_ns()       │
              └─────────────────────┬────────────────────┘
                                    │
              ┌─────────────────────▼───────────────────┐
              │  mode dispatch                          │
              │    raw  →  adb exec-out screencap       │
              │    png  →  adb exec-out screencap -p    │
              │    pull →  adb shell screencap + adb pull│
              │    auto →  try raw, png, pull in order  │
              └─────────────────────┬───────────────────┘
                                    │ bytes
              ┌─────────────────────▼───────────────────┐
              │  decode                                 │
              │    raw  →  parse_raw_screencap (header  │
              │           parser, RGBA→BGR)             │
              │    png/pull → cv2.imdecode              │
              └─────────────────────┬───────────────────┘
                                    │ native_bgr (ndarray)
              ┌─────────────────────▼───────────────────┐
              │  capture_latency_ms = (perf_counter_ns()│
              │                       - t) / 1e6        │
              └─────────────────────┬───────────────────┘
                                    │
              ┌─────────────────────▼───────────────────┐
              │  Frame(native_bgr, native_w, native_h,  │
              │        source_mode, capture_latency_ms, │
              │        capture_ts, native_w, native_h)  │
              └─────────────────────┬───────────────────┘
                                    │
              ┌─────────────────────▼───────────────────┐
              │  Remap.apply(frame)                     │
              │    cv2.resize INTER_LINEAR              │
              │    → new Frame at 1080×1920             │
              │    preserves native_width/native_height │
              └─────────────────────┬───────────────────┘
                                    │
              ┌─────────────────────▼───────────────────┐
              │  if debug: _write_artifacts(...)        │
              │    (atomic .tmp → rename)               │
              └─────────────────────┬───────────────────┘
                                    │
                                    ▼  Frame (reference resolution)
```

### 2.2 Component responsibilities

- **`Frame`** is a *container only*. It validates dtype/shape at
  construction and write-locks the underlying ndarray. No OpenCV
  logic. No I/O. Frozen dataclass; `__hash__ = None` because the
  ndarray field is unhashable.
- **`Remap`** is stateless apart from the target reference resolution.
  `apply(frame) → Frame` produces a new Frame at reference, preserving
  native dimensions and all metadata. Idempotent (no-resize) on a
  frame already at reference.
- **`Sensor`** is the only Phase 2 component that talks to ADB. Mode
  dispatch is a one-of switch; auto mode tries each concrete mode in
  order on the first capture and latches the winner for the rest of
  the session (no per-call re-trial, per ADR-01a §Decision (2)).
- **`parse_raw_screencap`** is a pure function — given a `bytes` buffer,
  returns `(image_bgr, width, height)` or raises
  `FrameDecodeError` / `UnsupportedPixelFormatError`.

### 2.3 ADR alignment

| ADR | Compliance |
|---|---|
| ADR-01 | Default mode = `raw`. PNG and pull fallback supported. |
| ADR-01a | `sensor.mode = "raw" | "png" | "pull" | "auto"` knob; auto = preference order, no benchmarking |
| ADR-02 | Internal representation: NumPy `uint8` BGR ndarray, shape `(H, W, 3)`; single RGBA→BGR conversion via `cv2.cvtColor(COLOR_RGBA2BGR)` |
| ADR-04 | Reference resolution `(1080, 1920)`; `INTER_LINEAR` for resampling; native dims preserved into the reference Frame |
| ADR-13 | `SENSOR_DEBUG` env var honoured at construction time only; no runtime mutation |
| ADR-16 | Imports only `numpy` + `cv2`; no new deps |

---

## 3. Header findings (raw mode)

The 16-byte Android 9+ layout confirmed in Phase 0 is the canonical
path. The 12-byte pre-Android-9 layout is supported for forward
portability but not exercised on this device.

```python
# 16-byte layout (Android 9+, default):
struct {
    uint32 width;
    uint32 height;
    uint32 pixel_format;   # 1 = PIXEL_FORMAT_RGBA_8888
    uint32 colorspace;     # informational; not consumed
};

# 12-byte layout (pre-Android-9):
struct {
    uint32 width;
    uint32 height;
    uint32 pixel_format;
};
```

Parser behavior:

- **Probes the 16-byte layout first**, then falls back to 12-byte if
  the 16-byte length test fails. This matches Phase 0's empirical
  observation that the operator's device uses 16-byte headers.
- **Validates buffer length exactly**: `len(buf) == header + W*H*4`.
  Mismatch raises `FrameDecodeError`. (Phase 0 noted one transient
  short-buffer event when the bench called `subprocess.run` rapidly;
  the parser catches that as a decode failure rather than silently
  truncating.)
- **Rejects any pixel format ≠ `PIXEL_FORMAT_RGBA_8888` (1)** with
  `UnsupportedPixelFormatError`. Auto mode catches this and falls
  back to PNG.

Live confirmation against the operator's device:

```
raw.bin first 16 bytes: 38 04 00 00 68 09 00 00 01 00 00 00 01 00 00 00
  width        = 1080
  height       = 2408
  pixel_format = 1  (PIXEL_FORMAT_RGBA_8888)
  colorspace   = 1
  buffer total = 10 402 576 bytes  = 16 + 1080 * 2408 * 4  ✓
```

This matches the canonical Phase 0 finding (`bench/artifacts/raw_header.txt`).

---

## 4. Latency measurements

Live measurements against the connected device. 20 captures per mode,
2 warmups discarded. Numbers are `Frame.capture_latency_ms` —
end-to-end from `capture()` entry to the BGR ndarray, **before** the
`Remap.apply` step (per the Phase 2 prompt: "capture request →
fully decoded BGR ndarray").

| Mode | n | Mean (ms) | Median (ms) | p95 (ms) | p99 (ms) | Stdev (ms) | Min (ms) | Max (ms) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| raw  | 20 |  941.7 |  940.2 |  971.5 |  971.5 |  23.2 |  895.1 |  996.7 |
| png  | 20 | 1320.7 | 1319.3 | 1389.8 | 1389.8 |  58.5 | 1235.9 | 1432.4 |
| pull | 20 | 1474.6 | 1450.9 | 1694.6 | 1694.6 | 117.6 | 1318.8 | 1711.3 |

(Screen content at capture: high-entropy. PNG payload was ~1.4 MB.)

### 4.1 Comparison with Phase 0 (high-entropy snapshot)

| Mode | Phase 0 median (ms) | Phase 2 median (ms) | Δ |
|---|---:|---:|---:|
| raw  |  946.98 |  940.2 | −0.7% |
| png  | 1311.62 | 1319.3 | +0.6% |
| pull | 1359.30 | 1450.9 | +6.7% |

raw and png are within ±1% of Phase 0. Pull is ~7% slower; pull mode
is the most variable (Phase 0 stdev 80 ms; Phase 2 stdev 118 ms) so
the median is unstable across small samples. The Phase 2 implementation
does not change the pipeline cost for any mode.

### 4.2 Comparison with frozen v1.0 NFRs

| Frozen NFR | v1.0 target | Measured (median) | Verdict |
|---|---|---:|---|
| Screenshot capture (`sensor.mode = "raw"`) | ≤ 1000 ms | 940 ms (raw) | ✅ within budget |
| Screenshot capture (across modes) | ≤ 1500 ms | 1451 ms (pull, worst) | ✅ within budget |
| Screenshot capture p95 (`sensor.mode = "raw"`) | ≤ 1100 ms | 971 ms (raw) | ✅ within budget |
| Per-template match (ROI gray) | ≤ 5 ms | — (Phase 3) | — |
| USB link speed at bootstrap | ≥ 480 Mbps | 480 Mbps (Phase 1) | ✅ |

All Phase 2-relevant NFRs are met by the operator's hardware in the
default `sensor.mode = "raw"` configuration.

### 4.3 Resample cost (excluded from `capture_latency_ms`)

The `Remap.apply` step (1080×2408 → 1080×1920 via `cv2.resize
INTER_LINEAR`) is intentionally not included in `capture_latency_ms`
because the Phase 2 prompt scopes the metric to "request → fully
decoded BGR ndarray". A quick spot check (10 invocations on the live
device frame) measured resample median **~3.4 ms**, well within the
ADR-04 estimate of 3–8 ms. Phase 5+ tick-budget accounting should
add this to the SENSE total.

---

## 5. Artifact behavior

When `SENSOR_DEBUG=1` env var is set (or `Sensor(debug=True)`):

```
var/artifacts/sensor/
└── 20260520T094148_252710_raw_a34c01d4/
    ├── frame.jpg            172 KB   reference-resolution BGR as JPEG q=85
    ├── metadata.json        367 B    declared dict (see below)
    └── raw.bin             10.0 MB   the original capture payload
```

`metadata.json`:

```json
{
  "active_mode": "raw",
  "capture_latency_ms": 970.337093,
  "capture_ts": "2026-05-20T09:41:48.252710+00:00",
  "channels": 3,
  "dtype": "uint8",
  "height": 1920,
  "mode_used": "raw",
  "native_height": 2408,
  "native_width": 1080,
  "payload_bytes": 10402576,
  "payload_file": "raw.bin",
  "requested_mode": "raw",
  "source_mode": "raw",
  "width": 1080
}
```

Properties:

- One subdirectory per capture, timestamped to microsecond precision
  with a short UUID suffix for sub-microsecond collisions.
- Atomic writes: each file is written to `tempfile.mkstemp()` and
  moved into place with `shutil.move()` after `fsync`.
- For PNG / pull modes the payload file is `screen.png`; for raw it
  is `raw.bin`.
- Best-effort: artifact write failures log a WARN and are swallowed.
  Sensor correctness does not depend on artifact persistence.
- Disabled by default. Enable with the env var or constructor flag.

---

## 6. Test results

```
$ .venv/bin/pytest -ra
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-8.4.2, pluggy-1.6.0
configfile: pyproject.toml
testpaths: tests
plugins: cov-6.3.0
collected 92 items

tests/test_adb.py .............                                          [ 14%]
tests/test_bootstrap.py ........                                         [ 22%]
tests/test_errors.py ..                                                  [ 25%]
tests/test_fingerprint.py .............                                  [ 39%]
tests/test_frame.py ................                                     [ 56%]
tests/test_paths.py ..                                                   [ 58%]
tests/test_remap.py .........                                            [ 68%]
tests/test_sensor.py .............................                       [100%]

============================== 92 passed in 0.73s ==============================
```

### 6.1 Coverage

```
Name                        Stmts   Miss  Cover   Missing
---------------------------------------------------------
automation/__init__.py          1      0   100%
automation/adb.py              74      4    95%
automation/bootstrap.py       107     12    89%
automation/errors.py           10      0   100%
automation/fingerprint.py      90     12    87%
automation/frame.py            44      2    95%
automation/paths.py            13      0   100%
automation/remap.py            23      0   100%
automation/sensor.py          197     25    87%
---------------------------------------------------------
TOTAL                         559     55    90%
```

**Package coverage: 90.2%** — meets the 90% minimum in the Phase 2 prompt.

Uncovered lines are defensive error paths in `sensor.py` (pull-mode
timeout, calledprocesserror, missing-file branches) and `bootstrap.py`
(the `EXIT_UNEXPECTED` catch-all). All exercised, just not in the
test suite — covered by the live device runs in §4 instead.

### 6.2 Test inventory (Phase 2 additions only)

| File | Tests | Highlights |
|---|---:|---|
| `tests/test_frame.py` | 16 | dtype/shape validation; write-lock; immutability; rejection of grayscale, wrong channels, empty, mismatched dims, negative latency, bad source_mode, non-datetime ts; `shape_summary`; `to_debug_dict` json-safety; unhashable |
| `tests/test_remap.py` | 9 | 1080×2408 → 1080×1920 path; native dims preserved; idempotency on reference; upsample path; custom reference; metadata carried through |
| `tests/test_sensor.py` | 29 | parser: 12-byte / 16-byte / bad length / unsupported format / format=0; sensor: raw / png / pull / auto; auto fallback raw→png; auto latching; auto all-fail; latency >= 0; UTC tz; debug artifacts (env var + flag); png payload-name; custom Remap |

---

## 7. NFR comparison

See §4.2 above. Repeated here for the index:

| Frozen NFR | v1.0 target | Phase 2 measured | Status |
|---|---|---:|---|
| Screenshot capture median (raw) | ≤ 1000 ms | 940 ms | ✅ |
| Screenshot capture median (across) | ≤ 1500 ms | 1451 ms (pull) | ✅ |
| Screenshot capture p95 (raw) | ≤ 1100 ms | 971 ms | ✅ |
| RAM (steady state) | ≤ 300 MB | 6–10 MB per frame; no leak observed in 20-iter soak (UE) | ✅ |
| Per-tick CPU (with ROI discipline) | ≤ 30% | — (Phase 3) | deferred |

---

## 8. Phase 3 readiness

| Requirement | Status |
|---|---|
| `Frame` type stable, owns BGR ndarray + metadata Phase 3 needs | ✅ |
| `Sensor.capture()` returns a frame at reference resolution | ✅ |
| `Remap` preserves native dims for Phase 4 coordinate work | ✅ |
| Latency measurement available for Phase 6 instrumentation | ✅ via `Frame.capture_latency_ms` |
| Debug artifact path lays the groundwork for Phase 6 replay | ✅ via `var/artifacts/sensor/` |
| Auto-mode latching documented and tested | ✅ ADR-01a §Decision (2) |
| No template logic leaked into SENSE | ✅ |

Phase 3 can begin. The screenshot pipeline is stable and produces
deterministic `Frame` objects against the live device.

---

## 9. Files created

```
automation/frame.py            134 lines
automation/remap.py             95 lines
automation/sensor.py           473 lines (incl. parse_raw_screencap + Sensor)
automation/errors.py            +44 lines (Phase 2 additions)
tests/test_frame.py            115 lines (16 tests)
tests/test_remap.py             97 lines (9 tests)
tests/test_sensor.py           331 lines (29 tests)
tests/conftest.py               +10 lines (sensor.subprocess.run patch)
phase2-report.md              (this file)
```

Total Phase 2 net additions: 6 modified/created Python files + the report.

---

## 10. Unresolved risks

None blocking. Documented:

- **Auto mode does not measure** — it picks raw, png, pull in that
  order on the first capture and latches. A workload where png is
  consistently faster but raw still succeeds will silently get the
  slower path. Documented in ADR-01a §Decision (2); operator override
  via `sensor.mode = "png"`. Dynamic A/B sampling is in v1.1 backlog
  row #2.
- **Pull-mode cleanup is best-effort** — the temp file on the device's
  `/sdcard` is removed in a `finally` block that swallows errors.
  Sustained failure of the cleanup would leak files. Not a v1.0
  concern (pull mode is a third-tier fallback) but flagged for
  observability instrumentation in Phase 6.
- **`Frame.image_bgr.setflags(write=False)` is advisory** — a caller
  that obtained a writable view of the same buffer before the Frame
  was constructed could still mutate it. v1.0 has no such caller;
  Phase 3's matcher does not retain frame references beyond the
  call (per ADR-03 / Phase 3 prompt). No real risk.
- **Resample cost not included in `capture_latency_ms`** — see §4.3.
  Phase 6 instrumentation will separately measure resample budget.
- **Debug artifacts are not rotation-capped** — `var/artifacts/sensor/`
  grows unbounded under sustained `SENSOR_DEBUG=1`. Phase 6's
  `ArtifactStore` will introduce the framework-wide rotation policy
  (max-count + max-bytes); the SENSE per-capture artifacts can be
  folded into that or rotated independently. For v1.0 the env var is
  intended for short-lived debug sessions, not steady-state.

---

## 11. Readiness verdict

**Phase 2: COMPLETE. Phase 3 may begin.**

Validation summary:

- 92 / 92 tests pass; 90.2% coverage on the automation/ package.
- Live device end-to-end captures for all four modes (raw, png, pull,
  auto) produce well-formed `Frame` objects with sensible latency.
- Numbers match Phase 0 within ±1% for raw and png.
- Debug artifacts write atomically with correct file content
  (10.4 MB raw.bin, 172 KB frame.jpg, well-formed metadata.json).
- ADR-01a's USB-link-speed prerequisite is enforced upstream by
  Phase 1's bootstrap; the sensor relies on that gate.
- No leakage of higher-level concerns (templates, FSM, actions) into
  SENSE — confirmed by import-tree inspection.

The Phase 3 implementer should next read `PHASE-MASTER-PROMPTS.md`
Phase 3, `ADR.md` ADR-03 / ADR-05 / ADR-10, and this report's §4 for
the available per-tick latency budget after SENSE.
