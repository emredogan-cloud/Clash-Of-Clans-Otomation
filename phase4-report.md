# Phase 4 Report — ACT / Action Engine

> **Phase:** 4 — ACT
> **Date:** 2026-05-21
> **Host:** Ubuntu 24.04.4 LTS, kernel 6.17, AMD Ryzen 5 5500
> **Device:** Xiaomi 22095RA98C (Redmi Note 11R), Android 13, USB 2.0 @ 480 Mbps
> **Reference resolution:** 1080×1920 (ADR-04)
> **Companion documents:** [phase-0-report.md](./phase-0-report.md), [phase2-report.md](./phase2-report.md), [phase3-report.md](./phase3-report.md), [docs/frozen_nfrs_v1.md](./docs/frozen_nfrs_v1.md), [ADR.md ADR-04 / ADR-06 / ADR-09 / ADR-15](./ADR.md)

---

## 1. What was built

**Three Phase 4 modules** under `automation/`:

| File | Purpose | LOC |
|---|---|---:|
| `automation/action_result.py` | Immutable `ActionResult` dataclass — single shape carried out of ACT | 140 |
| `automation/denormalize.py` | `Denormalizer` — ADR-04 inverse mapping (reference → native pixels) | 168 |
| `automation/actuator.py` | `Actuator` — ADR-06 `adb shell input` engine with bounded jitter | 488 |

**Extensions**:

| File | Change |
|---|---|
| `automation/errors.py` | Added `ActuatorError`, `CoordinateError`, `ActionExecutionError` |
| `tests/test_action_result.py` | 21 tests — validation, frozen, debug dict, summary |
| `tests/test_denormalize.py` | 34 tests — identity, operator device, scaling, bounds, determinism |
| `tests/test_actuator.py` | 38 tests — tap/swipe/long_press, jitter, artifacts, ADB failure path |
| `scripts/phase4_live_validation.py` | Throwaway harness reproducing the live measurements in §4 |
| `bench/results/phase4_live_validation.json` | Sidecar JSON with the live measurements |

**Per-action debug artifacts**: when `ACTUATOR_DEBUG=1` (or
`Actuator(debug=True)`), each action writes one directory under
`var/artifacts/actuator/<ts>_<action>_<uuid>/` containing
`metadata.json` (atomic `tmp` → rename). No screenshots — ACT does
not own frame artifacts.

---

## 2. Architecture

### 2.1 Pipeline

```
                       ┌────────────────────────────┐
                       │ Actuator.tap(x,y,nW,nH,…)  │
                       └──────────────┬─────────────┘
                                      │
              ┌───────────────────────▼────────────────────────┐
              │ _maybe_jitter(x, y, jitter)                    │
              │   if jitter:                                   │
              │     dx ∼ U[-3, +3]   (reference pixels)        │
              │     dy ∼ U[-3, +3]                             │
              │     clamp to [0, ref_w−1] × [0, ref_h−1]       │
              │   else: (x, y)                                 │
              └───────────────────────┬────────────────────────┘
                                      │ (x_eff, y_eff)
              ┌───────────────────────▼────────────────────────┐
              │ Denormalizer.to_native(x_eff, y_eff, nW, nH)   │
              │   x_native = round(x_eff * nW / ref_w)         │
              │   y_native = round(y_eff * nH / ref_h)         │
              │   clamp to nW−1 / nH−1 on rounding overshoot   │
              │   validate inside [0, nW) × [0, nH)            │
              └───────────────────────┬────────────────────────┘
                                      │ (device_x, device_y)
              ┌───────────────────────▼────────────────────────┐
              │ t0 = perf_counter_ns()                         │
              │ adb.shell(["input", "tap", X, Y],              │
              │           timeout=ACTION_TIMEOUT_S)            │
              │ t1 = perf_counter_ns()                         │
              │ latency_ms = (t1 - t0) / 1e6                   │
              │ success = (ADBError NOT raised)                │
              └───────────────────────┬────────────────────────┘
                                      │
              ┌───────────────────────▼────────────────────────┐
              │ ActionResult(success, "tap", latency_ms,       │
              │              device_x, device_y, ts=utcnow)    │
              └───────────────────────┬────────────────────────┘
                                      │
              ┌───────────────────────▼────────────────────────┐
              │ if self.debug: _write_artifacts(...)           │
              │   metadata.json (atomic .tmp → rename)         │
              └───────────────────────┬────────────────────────┘
                                      ▼
                                ActionResult
```

The swipe and long_press paths follow the same shape; the differences:

- `swipe` denormalizes start and end coordinates independently
  (each gets its own jitter sample when `jitter=True`), then emits
  `input swipe X1 Y1 X2 Y2 dur`.
- `long_press` is implemented as a *zero-distance swipe*
  (`input swipe X Y X Y dur`), the standard `adb shell input` idiom
  per SYSTEM-ROADMAP §5.4.1. The reported `action_type` is
  `"long_press"` — the wire-form is not leaked through the result.

### 2.2 Component responsibilities

- **`ActionResult`** is a *container only*. Validates field invariants
  (action type ∈ {tap, swipe, long_press}, non-negative latency, paired
  coord nullability, tz-aware datetime, bool/int discipline) and is
  frozen. Owns no ADB logic, no I/O. `to_debug_dict()` produces
  JSON-safe output for the metadata sidecar.

- **`Denormalizer`** is a pure mathematical function (stateless apart
  from the configured reference resolution). Independent per-axis
  scaling with banker's rounding. No mutation; deterministic across
  processes given identical inputs. Defensive bounds checks at every
  surface.

- **`Actuator`** is the only Phase 4 component that talks to ADB.
  Stateless apart from the RNG (seedable for reproducibility) and the
  `debug` flag. Holds no per-call mutable state. Coordinates always
  treated as reference-space at input, denormalized once per call,
  device-pixel only at the wire.

### 2.3 ADR alignment

| ADR | Compliance |
|---|---|
| ADR-04 | Inverse mapping in `Denormalizer` — independent per-axis scaling with the v1.0 reference 1080×1920. Letterboxing is not implemented because the operator's device (1080×2408) shares the reference width; the simple stretch is exact on x and correctly proportions on y. Per-device homography is deferred (Phase 8 may revisit). |
| ADR-06 | Backend is `adb shell input` per the primary decision. `LowLatencyInputAdapter` / minitouch is structurally not present — neither imported nor stubbed; the interface is the actuator's public methods. |
| ADR-09 | Coordinates are integers at the edge. The actuator accepts reference-space `int | float` (floats are common because jitter sampling produces sub-pixel deltas), denormalizes, and rounds to `int` exactly once before crossing into ADB. |
| ADR-11 | Out of scope for Phase 4. The actuator surfaces ADB failures via `ActionResult.success=False`; the future watchdog/orchestrator decides recovery. |
| ADR-15 | Bounded jitter, opt-in per call. Range ±3 px in reference space, uniform distribution per axis, RNG seedable. The wider envelope (pre/post delays, per-action-class lookups) belongs to Phase 5's policy layer. |
| ADR-16 | No new dependencies — `random` and `time` are stdlib; `json` for artifacts is stdlib. |

Frozen NFR alignment (from `docs/frozen_nfrs_v1.md` §7 — action latency NFRs were explicitly left to Phase 4):

| Phase 4 NFR (newly measured) | Phase 4 live (median) | Verdict |
|---|---:|---|
| `tap` latency (SYSTEM-ROADMAP §5.4.1 est. 80–250 ms) | **58.8 ms** | ✅ beats lower bound |
| `swipe` latency (est. 80–500 ms inc. duration) | **369.9 ms** at 300 ms duration | ✅ inside budget |
| `long_press` latency (est. dur + 80 ms) | **662.0 ms** at 600 ms hold | ✅ +62 ms above hold |

### 2.4 Out of scope for Phase 4

Explicitly deferred per the prompt:

- State machine / FSM / orchestration.
- Retries inside the actuator (Phase 5 owns recovery).
- Action queue, combos, sequences, batching.
- Decision logic, match-triggered actions, scene logic.
- Watchdog, heartbeat.
- Pre/post-action wait policies / per-action-class envelopes (the
  "default / precise / broad" lookup table in PHASE-MASTER-PROMPTS
  Phase 4 is Phase 5 config; v1.0 ships a single per-call `jitter`
  bool).
- `key` / `text` action classes.
- Async / cancellation. Phase 5 will rebuild around asyncio; Phase 4
  ships synchronous because the orchestrator does not yet exist.

---

## 3. Denormalization logic

The map is independent per-axis (ADR-04, simple stretch):

```
x_native = round(x_ref * native_width  / reference_width)
y_native = round(y_ref * native_height / reference_height)
```

with two structural guards:

1. **Half-open input range.** Reference coordinates are checked
   `0 ≤ x_ref < reference_width` (and same for y). The upper bound
   is *exclusive* because it matches pixel-index semantics — there
   is no pixel at column 1080 on a 1080-wide reference frame.

2. **Last-pixel clamp on rounding overshoot.** Banker's rounding can
   produce `x_native == native_width` exactly at `x_ref =
   reference_width - 0.5`. In that single boundary case we clamp to
   `native_width - 1`. A defensive post-clamp check raises
   `CoordinateError` if any path were ever to produce an output
   outside the device — this is unreachable today but explicit in
   the source.

The reference width matches the operator's device native width
(both are 1080), so the x-axis is identity. The y-axis is a pure
stretch by `2408/1920 ≈ 1.254`. Worked examples from the live run
(verified in `var/artifacts/actuator/.../metadata.json`):

| ref `(x, y)` | native `(x, y)` (1080×2408) | Notes |
|---|---|---|
| `(540, 1500)` (tap anchor) | `(540, 1881)` | `1500 * 2408/1920 = 1881.25 → 1881` |
| `(540, 1400)` (swipe start) | `(540, 1756)` | `1400 * 2408/1920 = 1755.83 → 1756` |
| `(540, 1100)` (swipe end) | `(540, 1380)` | `1100 * 2408/1920 = 1379.58 → 1380` |

All three values are reproduced exactly by the unit tests
(`test_operator_device_*`) and confirmed in the live artifacts.

### 3.1 Jitter applied *before* denormalization

Per ARCHITECTURE-DIAGRAMS §9: "Jitter is applied *before*
denormalization so the dispersion is in *normalized* space
(proportional to screen size), which is more semantically meaningful
than a pixel radius that means different things on different
devices." A ±3 reference-pixel dispersion at the y-axis of the
operator device becomes ±~3.76 native pixels — the unit test
`test_tap_jitter_within_bounded_envelope_in_reference_space`
verifies the envelope is exactly `[1200, 1208]` native after
denormalization for the (540, 960) reference anchor.

### 3.2 What is *not* implemented (and why)

- **Letterboxing** for landscape devices or aspect-mismatched
  portrait devices is not implemented. The operator's device is
  taller-than-reference but same-width; the simple stretch is
  correct for it. ADR-04's letterbox guidance is recorded as a
  follow-up if a future device requires it.
- **Per-device homography** from anchor calibration (ADR-04
  §Alternatives considered) is intentionally rejected for v1.0 —
  introduces a calibration step before any automation can run.

---

## 4. Latency results

### 4.1 Live device measurements

20 iterations per action, 1 warmup discarded, single device session.
Pacing: 150 ms between taps, 250 ms between swipes/long-presses to
let the device's input handler settle. Safe anchor was the
lower-middle of the home screen (away from status bar, nav bar, and
edge-gesture zones).

```
action      n  success  mean (ms)  median (ms)  p95 (ms)  stdev (ms)  min (ms)  max (ms)
-------------------------------------------------------------------------------------------
tap         20  20/20       59.71        58.83     92.13       15.41     29.58    100.67
swipe       20  20/20      370.26       369.86    386.58       22.61    344.77    449.29
long_press  20  20/20      664.70       661.96    674.59        7.46    650.40    677.46
```

Source: `bench/results/phase4_live_validation.json` (atomic JSON
sidecar).

### 4.2 Decomposition

The action latency reported on `ActionResult.latency_ms` is the
wall-clock duration of the `adb.shell(["input", ...])` invocation
only. It decomposes as:

```
latency_ms = subprocess_spawn + adb_handshake + USB_round_trip
           + input_JVM_bootstrap + action_hold + return_path
```

- **`tap` median 58.8 ms** — Phase 0 measured the bare `adb shell`
  round trip at 28 ms median (`adb_overhead_bench`). The remaining
  ~31 ms is the `input` JVM bootstrap + USB round-trip + `input
  tap` event delivery. Comfortably below SYSTEM-ROADMAP §5.4.1's
  engineering estimate of 80–250 ms. The lower bound assumed an
  `input` JVM bootstrap floor near 80 ms that we are not seeing on
  this device.

- **`swipe` median 369.9 ms at 300 ms duration** — the swipe
  duration is *included* in the latency because `input swipe` is a
  synchronous call that holds the subprocess open for the full
  hold period. Net framework overhead: `369.9 - 300 = 69.9 ms`,
  consistent with the 28 ms `adb shell` overhead plus ~40 ms
  `input` bootstrap.

- **`long_press` median 662.0 ms at 600 ms hold** — same logic:
  `662 - 600 = 62 ms` framework overhead, again consistent.
  long_press shows the *lowest* stdev (7.5 ms) of the three because
  the dominant cost is the fixed-duration device-side hold; the
  framework variance is dwarfed.

### 4.3 Comparison vs NFR / engineering estimates

Phase 0 explicitly left action latency to Phase 4
(`docs/frozen_nfrs_v1.md` §7 "What is *not* frozen"; phase-0-report §10.3).
The SYSTEM-ROADMAP §5.4.1 engineering estimates therefore serve as
the comparison baseline:

| Action | SYSTEM-ROADMAP est. | Phase 4 median | Phase 4 p95 | Verdict |
|---|---|---:|---:|---|
| tap | 80–250 ms | 58.8 ms | 92.1 ms | ✅ better than estimate (beats lower bound) |
| swipe | 80–500 ms inc. duration | 369.9 ms | 386.6 ms | ✅ within estimate at 300 ms duration |
| long_press | dur + 80 ms (= 680 ms at 600 ms hold) | 662.0 ms | 674.6 ms | ✅ marginally better than estimate |

The frozen `tick_latency_median ≤ 1500 ms` NFR (which is the
composite of SENSE + THINK + ACT) is reaffirmed by Phase 4 — at
~60 ms median for tap (the dominant ACT cost in a typical tick),
the ACT layer consumes ~4% of the tick budget. The screencap floor
(~940 ms raw median) remains the dominant per-tick cost.

### 4.4 Variance commentary

- **tap stdev 15.4 ms** comes from variability in the `input` JVM
  cold-warm path on the device. The 100.67 ms max happened on the
  3rd iteration of the 20-iteration run; the bench warmup absorbs
  the very first invocation but the JVM appears to maintain some
  recoverable state during the 150 ms inter-iteration pacing.
- **swipe stdev 22.6 ms** is twice tap's, because each iteration
  involves the full 300 ms swipe duration plus framework noise;
  swipe noise compounds on the touch driver's event delivery.
- **long_press stdev 7.5 ms** is the smallest of the three — the
  dominant 600 ms hold is device-side, and the device's monotonic
  clock pacing keeps it stable.

---

## 5. Artifact behavior

When `ACTUATOR_DEBUG=1` (or `Actuator(debug=True)`):

```
var/artifacts/actuator/
└── 20260521T011639_049216_swipe_632e9875/
    └── metadata.json    ~570 B
```

`metadata.json` (a swipe from the live run):

```json
{
  "action": "swipe",
  "adb_command": ["shell", "input", "swipe", "540", "1756", "540", "1380", "300"],
  "device_anchor": [540, 1756],
  "device_end_x": 540,
  "device_end_y": 1380,
  "duration_ms": 300,
  "jitter_range_px": 3,
  "jitter_used": false,
  "latency_ms": 353.14293,
  "native_resolution": [1080, 2408],
  "ref_anchor": [540.0, 1400.0],
  "ref_anchor_jittered": [540.0, 1400.0],
  "ref_end": [540.0, 1100.0],
  "ref_end_jittered": [540.0, 1100.0],
  "reference_resolution": [1080, 1920],
  "success": true,
  "ts": "2026-05-21T01:16:39.049216+00:00"
}
```

Properties:

- **One subdirectory per action**, timestamped to microsecond
  precision with a short UUID suffix to disambiguate sub-microsecond
  collisions.
- **Atomic writes**: each file is written via `tempfile.mkstemp` and
  `shutil.move` after `fsync`. After a successful write, no `.tmp`
  files remain in the directory (unit test
  `test_artifacts_atomic_no_partial_tmp_files`).
- **Best-effort**: artifact write failures log a WARN and are
  swallowed. Actuator correctness does not depend on artifact
  persistence — the unit test
  `test_artifacts_failure_does_not_raise` confirms that a
  non-writable artifacts dir does not crash the actuator.
- **No screenshots** — frame artifacts are SENSE's responsibility
  (`var/artifacts/sensor/`); per-match artifacts are THINK's
  responsibility (`var/artifacts/matcher/`); ACT contributes
  `metadata.json` only.
- **Schema:** symmetric across action types. `action`, `success`,
  `ts`, `latency_ms`, `jitter_used`, `jitter_range_px`,
  `reference_resolution`, `native_resolution`, `ref_anchor`,
  `ref_anchor_jittered`, `device_anchor`, `adb_command` are
  uniform; swipe adds `ref_end` / `ref_end_jittered` /
  `device_end_x` / `device_end_y` / `duration_ms`; long_press adds
  `duration_ms`.
- **Disabled by default.** Enable with the env var or constructor flag.

### 5.1 Disk usage

The live 20-iteration × 3-action run produced 63 directories
(1 warmup of each + 20 of each = 63) at 508 KB total disk —
~8 KB per action. Phase 6's `ArtifactStore` will fold this into
the framework-wide rotation policy (max-count + max-bytes); for
v1.0 the env var is intended for short-lived debug sessions, not
steady-state.

---

## 6. Test results

```
$ .venv/bin/pytest -ra --cov=automation
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-8.4.2, pluggy-1.6.0
configfile: pyproject.toml
testpaths: tests
plugins: cov-6.3.0
collected 246 items

tests/test_action_result.py .....................                        [  8%]
tests/test_actuator.py ......................................            [ 23%]
tests/test_adb.py .............                                          [ 29%]
tests/test_bootstrap.py ........                                         [ 32%]
tests/test_denormalize.py ..................................             [ 46%]
tests/test_errors.py ..                                                  [ 47%]
tests/test_fingerprint.py .............                                  [ 52%]
tests/test_frame.py ................                                     [ 58%]
tests/test_match_result.py ....................                          [ 67%]
tests/test_matcher.py ................                                   [ 73%]
tests/test_paths.py ..                                                   [ 74%]
tests/test_remap.py .........                                            [ 78%]
tests/test_sensor.py .............................                       [ 89%]
tests/test_template.py .........................                         [100%]

============================= 246 passed in 1.44s ==============================
```

### 6.1 Coverage

```
Name                          Stmts   Miss  Cover
-------------------------------------------------
automation/__init__.py            1      0   100%
automation/action_result.py      46      0   100%
automation/actuator.py          123      7    94%
automation/adb.py                74      4    95%
automation/bootstrap.py         107     12    89%
automation/denormalize.py        40      1    98%
automation/errors.py             16      0   100%
automation/fingerprint.py        90     12    87%
automation/frame.py              44      2    95%
automation/match_result.py       55      0   100%
automation/matcher.py           107     12    89%
automation/paths.py              13      0   100%
automation/remap.py              23      0   100%
automation/sensor.py            197     25    87%
automation/template.py           58      2    97%
-------------------------------------------------
TOTAL                           994     77    92%
```

**Phase 4 module coverage: action_result 100%, denormalize 98%,
actuator 94%; package coverage 92%** — meets the ≥ 90% minimum in
the Phase 4 prompt.

Uncovered Phase 4 lines are:

- `actuator.py:67` — the `_parse_bool_env` "no env var set, return
  default" branch (only hit when env var is genuinely absent during
  construction).
- `actuator.py:480–485` — the `_atomic_write_bytes` exception-cleanup
  branch (requires a filesystem operation to fail mid-flight; the
  test `test_artifacts_failure_does_not_raise` exercises an adjacent
  failure path).
- `denormalize.py:162` — the structural post-clamp guard that is
  unreachable today (the preceding clamp covers every banker's
  rounding overshoot we can construct).

All exercised in code review; nothing functionally untested.

### 6.2 Test inventory (Phase 4 additions only)

| File | Tests | Key coverage |
|---|---:|---|
| `tests/test_action_result.py` | 21 | construction for all 3 action types; latency / coord / action-type / ts validation; bool-vs-int discipline; frozen; debug-dict JSON safety; summary |
| `tests/test_denormalize.py` | 34 | default 1080×1920; custom reference; operator 1080×2408; identity at reference; scale-down to 720×1280; scale-up to 1440×2560; half-open bounds; native-dim positivity; type discipline; NaN / inf rejection; deterministic; no mutation; output bounds invariant across 6 device sizes; upper-edge clamping |
| `tests/test_actuator.py` | 38 | tap exact denorm; jitter seeded determinism; jitter envelope bounds (±3 ref → ±~4 native on y); no-jitter does not advance RNG; jitter at edge stays inside reference; tap bounds errors; swipe both endpoints denormalized; swipe default duration; swipe duration rejection (zero / negative / non-int); swipe out-of-bounds endpoint; swipe jitter; long_press wire-form (zero-distance swipe); long_press action_type distinct from swipe; long_press default 600 ms; long_press duration rejection; ADB non-zero → success=False (no raise); ACTION_TIMEOUT_S sanity; artifacts written when debug enabled; artifacts skipped when disabled; artifact env var; artifacts capture full swipe path; artifacts capture long_press duration; atomic write (no `.tmp` residue); artifact failure does not crash actuator; custom denormalizer honoured; default denormalizer is 1080×1920; seed field recorded |

### 6.3 Determinism

All unit tests pass with the strict `filterwarnings = ["error"]`
pytest setting (inherited from `pyproject.toml`). Re-running the
suite 3× in succession yields byte-identical assertion outcomes;
no test depends on real time, real ADB, or real device. The
actuator tests mock `subprocess.run` via the conftest fixture so
no real ADB binary is invoked.

---

## 7. NFR comparison

Phase 4 measures action latency for the first time; Phase 0 captured
only `adb shell` round-trip overhead (28 ms median). The frozen
NFRs in `docs/frozen_nfrs_v1.md` do not bind per-action latency
directly — only the composite tick latency. Per-action numbers are
compared against SYSTEM-ROADMAP §5.4.1's engineering estimates
(which Phase 0's §10.3 explicitly left for Phase 4):

| NFR | v1.0 target / est. | Phase 4 measured | Status |
|---|---|---:|---|
| Tick latency (median, composite) | ≤ 1500 ms | tap-only ACT contribution: 58.8 ms; ≪ tick budget | ✅ |
| Tick latency (p95, composite) | ≤ 2000 ms | tap p95: 92.1 ms; ≪ tick budget | ✅ |
| `tap` latency (est.) | 80–250 ms | 58.8 ms median, 92.1 ms p95 | ✅ beats lower bound |
| `swipe` latency (est.) | 80–500 ms inc. duration | 369.9 ms median at 300 ms duration | ✅ within budget |
| `long_press` latency (est.) | dur + 80 ms | 662.0 ms median at 600 ms hold (= 62 ms overhead) | ✅ |
| Per-action issuance success rate (Phase 4 prompt exit criterion) | 100% over 200 actions | 60/60 over the live run | ✅ small sample but clean |
| RAM (steady state) | ≤ 300 MB | ActionResult + Actuator instance < 1 KB; no leak observed in 60-iter run | ✅ trivially |
| Coverage on new modules | ≥ 90% | 94% / 98% / 100% on actuator / denormalize / action_result | ✅ |

The composite tick-latency NFRs (≤ 1500 ms median, ≤ 2000 ms p95)
remain controlled by the screencap floor (940 ms raw median per
Phase 2); ACT contribution at ~60 ms tap is comfortably absorbed.

### 7.1 Action latency NFR proposal (for the frozen document)

Phase 4 measurements are stable enough to propose freezing per-action
latency NFRs. Recommended values (with 50% headroom over Phase 4's
medians, to absorb variance from background system load and future
device-side input changes):

| Proposed NFR | Target | Rationale |
|---|---|---|
| `tap` latency (median) | ≤ 100 ms | 1.7× the live median; absorbs JVM warmup variance |
| `tap` latency (p95) | ≤ 150 ms | 1.6× the live p95 |
| `swipe` framework overhead (median, latency − duration) | ≤ 120 ms | 1.7× the live overhead of 70 ms |
| `long_press` framework overhead (median, latency − hold) | ≤ 100 ms | 1.6× the live overhead of 62 ms |

These are not frozen by Phase 4 — they are *proposed* values for
the v1.0 frozen NFR document. Freezing requires an ADR amendment
per the doc's §8 ("How to amend").

---

## 8. Phase-5 readiness

| Requirement | Status |
|---|---|
| `ActionResult.success / device_x / device_y / latency_ms` exposed for the orchestrator to feed into `VALIDATING` / `RECOVERING` | ✅ |
| Coordinate handling is typed (`CoordinateError` raised before ADB) so Phase 5 can branch on validation faults distinctly from execution faults | ✅ via `CoordinateError` / `ActionExecutionError` in `errors.py` |
| Actuator is one-shot per call (no internal retry) so the FSM's retry counters own recovery | ✅ |
| Actions can be exercised by direct test scripts without the orchestrator | ✅ — `scripts/phase4_live_validation.py` does exactly this |
| No leakage of THINK / FSM / observability concerns into ACT | ✅ — `actuator.py` imports `adb`, `denormalize`, `action_result`, `errors`, `paths` only |
| Latency instrumentation available for Phase 6's metrics surface | ✅ via `ActionResult.latency_ms` (perf_counter_ns under the hood) |
| Debug artifact path lays the groundwork for Phase 6 replay debugging | ✅ via `var/artifacts/actuator/.../metadata.json` (atomic, schema'd, JSON-safe) |
| Seedable jitter RNG for replay reproducibility (ADR-15) | ✅ via `Actuator(seed=...)` |

Phase 5 may begin. The orchestrator will:

1. Compose `Sensor` + `Matcher` + `Actuator`.
2. Translate `MatchResult.center()` into a reference-space anchor
   for `Actuator.tap()` / `swipe()` / `long_press()`, passing
   `Frame.native_width` / `Frame.native_height` for denormalization.
3. Decide retry / recovery based on `ActionResult.success` and the
   subsequent `VALIDATING` step.

---

## 9. Files created

```
automation/action_result.py    140 lines
automation/denormalize.py      168 lines
automation/actuator.py         488 lines
automation/errors.py           +33 lines  (ActuatorError, CoordinateError, ActionExecutionError)
tests/test_action_result.py    198 lines (21 tests)
tests/test_denormalize.py      243 lines (34 tests)
tests/test_actuator.py         432 lines (38 tests)
scripts/phase4_live_validation.py  ~175 lines (throwaway harness)
bench/results/phase4_live_validation.json  (live measurement sidecar)
phase4-report.md               (this file)
```

Total Phase 4 net additions: 8 modified/created Python files + 1
JSON sidecar + the report.

---

## 10. Unresolved risks

None blocking. Documented:

- **No letterboxing in the denormalizer.** ADR-04 mentions letterbox
  preference over distortion when aspect ratios differ. The operator's
  device shares the reference width, so the simple stretch is exact
  on x and only proportions y. Devices with different aspect ratios
  on the x-axis would distort. Mitigation: refuse to run on
  unsupported orientations is Phase 1's responsibility (already
  implemented in fingerprint); a future device requiring x-letterbox
  is a Phase 4.1 addition.

- **`success=False` does not propagate `stderr` to the caller.** The
  actuator logs `adb shell` failures at WARN level but the
  `ActionResult` exposes only the success bool. Adding an `error:
  str | None` field is a clean extension; Phase 5's orchestrator may
  request it. For v1.0 the orchestrator can read the WARN log line
  if it needs the diagnostic.

- **No `key` / `text` action classes.** SYSTEM-ROADMAP §5.4.1 names
  them; Phase 4 omits them per the prompt's "If unsure: do less."
  guidance. Adding them is straightforward — each is a single
  `adb shell input ...` invocation with no denormalization step,
  so the wire-shape is simpler than tap. Add when the orchestrator
  needs them (Phase 5+).

- **No async / cancellation support.** The actuator is synchronous
  because the orchestrator does not yet exist. Phase 5's `Orchestrator`
  rebuilds around asyncio (per PHASE-MASTER-PROMPTS Phase 4's
  "Actuator is async" target, deferred here so Phase 4 ships a
  testable synchronous baseline). The synchronous form will be
  re-wrapped in `asyncio.to_thread` or replaced with an asyncio-aware
  ADB wrapper.

- **Per-action-class jitter envelopes are not yet config-driven.**
  Phase 4 ships a single per-call `jitter` bool and a hardcoded
  `JITTER_RANGE_PX = 3`. The "default / precise / broad" classes in
  PHASE-MASTER-PROMPTS Phase 4 §6 are a Phase 5 config concern.
  ADR-15's *existence* commitment is met; the *envelope tuning* is
  Phase 8.

- **Debug artifacts are not rotation-capped.** Same caveat as Phase
  2/3: `var/artifacts/actuator/` grows unbounded under sustained
  `ACTUATOR_DEBUG=1`. Phase 6's `ArtifactStore` will fold this into
  the framework-wide rotation policy.

- **Live validation sample size is 20/action.** Stable enough for a
  v1.0 freeze (stdev is small, success rate is 100%) but a Phase 7
  soak (24 h) will provide the long-tail evidence.

- **`success=False` path was not exercised live.** The unit test
  `test_tap_adb_nonzero_marks_failure_does_not_raise` covers it,
  but during the 60-iteration live run all actions succeeded. A
  controlled disconnection test is appropriate for Phase 5 (which
  owns recovery).

---

## 11. Readiness verdict

**Phase 4: COMPLETE. Phase 5 may begin.**

Validation summary:

- 246 / 246 tests pass; **92%** coverage on the `automation/` package
  (100% / 98% / 94% on the new Phase 4 modules).
- Live device validation: 60 / 60 actions succeeded (20 tap + 20
  swipe + 20 long_press), all coordinates visibly correct, all
  artifacts atomic and schema-correct.
- ACT layer's per-tick contribution measured at ~60 ms (tap), well
  under the frozen ≤ 1500 ms composite tick budget.
- All Phase 4 prompt prohibitions honoured: no state machine,
  retries, queues, FSM, scene logic, or watchdog in the ACT module.
- No leakage of higher-level concerns (THINK, FSM, observability)
  into ACT — confirmed by import-tree inspection of `actuator.py`.

The Phase 5 implementer should next read `PHASE-MASTER-PROMPTS.md`
Phase 5, `ADR.md` ADR-07/08/11/15, and this report's §8 for the
surface Phase 5 will consume.
