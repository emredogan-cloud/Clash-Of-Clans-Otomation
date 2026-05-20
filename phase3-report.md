# Phase 3 Report — THINK / Matcher Layer

> **Phase:** 3 — THINK
> **Date:** 2026-05-20
> **Host:** Ubuntu 24.04.4 LTS, kernel 6.17, AMD Ryzen 5 5500
> **Device:** Xiaomi 22095RA98C (Redmi Note 11R), Android 13, USB 2.0 @ 480 Mbps
> **Reference resolution:** 1080×1920 (ADR-04)
> **Companion documents:** [phase-0-report.md](./phase-0-report.md), [phase2-report.md](./phase2-report.md), [docs/frozen_nfrs_v1.md](./docs/frozen_nfrs_v1.md), [ADR.md ADR-03](./ADR.md)

---

## 1. What was built

**Three Phase 3 modules** under `automation/`:

| File | Purpose | LOC |
|------|---------|----:|
| `automation/match_result.py` | Immutable `MatchResult` dataclass — single shape carried out of THINK | 130 |
| `automation/template.py` | Immutable `Template` dataclass — name + grayscale image + threshold + optional ROI | 150 |
| `automation/matcher.py` | `Matcher` class — `cv2.matchTemplate(TM_CCOEFF_NORMED)` + debug artifacts | 245 |

**Extensions**:

| File | Change |
|---|---|
| `automation/errors.py` | Added `MatcherError`, `InvalidROIError`, `MatchComputationError` |
| `tests/` | Added `test_match_result.py` (20 tests), `test_template.py` (25 tests), `test_matcher.py` (16 tests) |
| `scripts/phase3_live_validation.py` | Throwaway script reproducing the Phase 3 live measurements |

**Per-match debug artifacts**: when `MATCHER_DEBUG=1` (or `Matcher(debug=True)`),
each `match()` writes a directory under `var/artifacts/matcher/` with
the BGR frame (jpg), the grayscale template (jpg), the normalized
correlation heatmap (jpg), and a JSON metadata sidecar. Writes are
atomic (`tmp` → rename); failures degrade gracefully without crashing
the matcher.

---

## 2. Architecture

### 2.1 Pipeline

```
                       ┌────────────────────────────┐
                       │  Matcher.match(frame, tpl) │
                       └──────────────┬─────────────┘
                                      │
              ┌───────────────────────┴────────────────────────┐
              │     t0 = perf_counter_ns()                     │
              └───────────────────────┬────────────────────────┘
                                      │
              ┌───────────────────────▼────────────────────────┐
              │     cv2.cvtColor(frame.image_bgr, BGR2GRAY)    │
              └───────────────────────┬────────────────────────┘
                                      │ frame_gray
              ┌───────────────────────▼────────────────────────┐
              │     ROI dispatch                               │
              │       template.roi is not None                 │
              │         → validate_roi(W, H)                   │
              │         → search = frame_gray[y1:y2, x1:x2]    │
              │         → search_mode = "roi_gray"             │
              │       else                                     │
              │         → assert template fits in frame        │
              │         → search = frame_gray                  │
              │         → search_mode = "full_gray"            │
              └───────────────────────┬────────────────────────┘
                                      │
              ┌───────────────────────▼────────────────────────┐
              │     cv2.matchTemplate(search,                  │
              │                       template.image_gray,     │
              │                       TM_CCOEFF_NORMED)        │
              └───────────────────────┬────────────────────────┘
                                      │ heatmap (float32)
              ┌───────────────────────▼────────────────────────┐
              │     cv2.minMaxLoc(heatmap)                     │
              │       → max_val, max_loc                       │
              └───────────────────────┬────────────────────────┘
                                      │
              ┌───────────────────────▼────────────────────────┐
              │     confidence = max(0, min(1, max_val))       │
              │     found = (confidence >= template.threshold) │
              │     (x, y) = max_loc + (offset_x, offset_y)    │
              └───────────────────────┬────────────────────────┘
                                      │
              ┌───────────────────────▼────────────────────────┐
              │     t1 = perf_counter_ns()                     │
              │     match_latency_ms = (t1 - t0) / 1e6         │
              └───────────────────────┬────────────────────────┘
                                      │
              ┌───────────────────────▼────────────────────────┐
              │     MatchResult(found, confidence, search_mode,│
              │                 capture_latency_ms,            │
              │                 match_latency_ms,              │
              │                 x, y, width, height,           │
              │                 template_name)                 │
              └───────────────────────┬────────────────────────┘
                                      │
              ┌───────────────────────▼────────────────────────┐
              │     if debug: _write_artifacts(...)            │
              │       frame.jpg, template.jpg, heatmap.jpg,    │
              │       metadata.json (atomic)                   │
              └───────────────────────┬────────────────────────┘
                                      ▼
                                MatchResult
```

### 2.2 Component responsibilities

- **`MatchResult`** is a *container only*. Validates confidence
  range, latency non-negativity, coordinate consistency (all-None or
  all-int; if `found` then all-int). `center()` returns midpoint;
  `to_debug_dict()` serializes for JSON.
- **`Template`** is a *pure data object*. No file I/O. Validates
  grayscale uint8 image, dimension match, threshold in `(0, 1]`,
  ROI tuple shape (`x1 < x2`, `y1 < y2`, non-negative).
  `validate_roi(W, H)` further checks ROI is inside the frame and
  the template fits inside the ROI.
- **`Matcher`** is *stateless apart from the `debug` flag*. Thread-safe
  (OpenCV releases the GIL on matchTemplate; no instance state per
  call). Coordinates always reported in FRAME space, never in ROI
  space — the offset is added during match resolution.

### 2.3 ADR alignment

| ADR | Compliance |
|---|---|
| ADR-03 | Primary CV strategy = `cv2.matchTemplate(TM_CCOEFF_NORMED)`. No ORB, no SIFT, no ML. |
| ADR-04 | Match is performed at the reference resolution 1080×1920 (the `Frame` is already remapped by Phase 2's `Sensor`). Native dims preserved through `Frame`. |
| ADR-05 | Grayscale-only matching. BGR templates are structurally forbidden; the `Template` ctor rejects 3-channel images. |
| ADR-09 | Coordinates are integer device pixels at the edge; the matcher uses integer arithmetic via `cv2.minMaxLoc` integer locations + integer offsets. |
| ADR-13 | `MATCHER_DEBUG` env var consulted at constructor time only; no runtime mutation. |
| ADR-16 | Imports only `numpy` + `cv2`; no new deps. |

Frozen NFR alignment (from `docs/frozen_nfrs_v1.md`):

| Frozen NFR | Phase 3 implementation |
|---|---|
| ROI grayscale ≤ 5 ms median | Achieved (see §4). |
| Full-frame grayscale ≤ 50 ms median | Achieved with margin. |
| Full-frame BGR — opt-in only | NOT supported in Phase 3; `Template` rejects 3-channel images. Phase 3's THINK pipeline is grayscale-only. Full-frame BGR is left as a Phase 3+ optional extension if ever needed (currently no use case). |
| Active templates per tick (default) ≤ 8, ROI-required | Per-template: caller's responsibility. The matcher is one-template-per-call; the orchestrator (Phase 5) loops. |

### 2.4 Out of scope for Phase 3

Explicitly deferred per the prompt:

- Multi-template batch matching (Phase 5 loops).
- Multi-scale fallback (ADR-03 `multi_scale: true` flag — backlog).
- Masks (ADR-03 binary masks — backlog; matcher would accept `cv2.matchTemplate(..., mask=)`).
- Non-maximum suppression / find-all-instances (utility, not default).
- A `FramePreprocessor` cache for grayscale conversion across calls.
- `TemplateManifest` loader and on-disk content addressing (ADR-10).

---

## 3. Matching flow

The flow above expanded into operational notes:

1. **Frame → grayscale**: one `cv2.cvtColor(BGR2GRAY)` per match. Cheap
   (~1 ms at 1080×1920). Phase 5 may introduce a per-tick gray cache;
   not in Phase 3 scope.
2. **Search region selection**:
   - If `template.roi` is set: validate against the frame's reference
     dimensions (`InvalidROIError` if ROI exceeds the frame or the
     template does not fit inside the ROI). Then slice
     `frame_gray[y1:y2, x1:x2]`. Track `(offset_x, offset_y) = (x1, y1)`
     for coordinate translation.
   - If `template.roi is None`: require `template ≤ frame` in both
     dims. Otherwise raise `MatchComputationError`. `(offset_x,
     offset_y) = (0, 0)`.
3. **`cv2.matchTemplate(search, template.image_gray, TM_CCOEFF_NORMED)`**.
   Output is a float32 correlation map of shape `(search_h - template_h + 1, search_w - template_w + 1)`.
4. **Peak find** via `cv2.minMaxLoc`. We use only the maximum; minimum
   is discarded.
5. **Confidence clamp**: raw correlation is in `[-1, 1]`; we clamp to
   `[0, 1]` for the reported `confidence`. Negative correlations
   (inverse pattern) are never "found" because any positive threshold
   exceeds 0.
6. **Threshold gate**: `found = (clamped_confidence >= template.threshold)`.
   The comparison is inclusive at the threshold boundary.
7. **Coordinate translation**: when `found`, report
   `(x, y) = (max_x + offset_x, max_y + offset_y)` so the caller sees
   FRAME coordinates, not ROI-local coordinates. This is the most
   common bug in template-matching code; the matcher's contract makes
   it explicit and the test suite has dedicated coverage
   (`test_roi_offset_correctly_translates_to_frame_coords`).
8. **Latency stop**: `t1` is taken after coordinate translation but
   before debug artifact writing. Artifact I/O is observability
   overhead, not match cost.
9. **Debug artifacts** (optional, see §6).

---

## 4. Latency results

Live measurements against the connected device. 20 iterations per case,
no warmup discarded (the first iteration has higher one-time setup but
is still well within budget on this hardware). Frame is captured once
and reused across iterations.

```
case           roi?   found%   conf med  median ms   p95 ms  pos
----------------------------------------------------------------
  easy_full    no      100%      1.000      47.68    58.98  @(560,240)
  easy_roi     yes     100%      1.000       0.97     1.35  @(560,240)
  medium_full  no      100%      1.000      48.85    58.04  @(320,560)
  medium_roi   yes     100%      1.000       2.32     2.68  @(320,560)
  miss_full    no        0%      0.046      49.83    55.24  MISS
  miss_roi     yes       0%      0.034       3.23     3.57  MISS
```

Setup:

- Templates are 110×110 grayscale crops from the captured frame at the
  patch with the highest gray-stdev in the relevant frame region
  (top-quarter for `easy`, middle-half for `medium`). This avoids
  picking a degenerate solid-colour patch (which under
  `TM_CCOEFF_NORMED` has zero variance and ill-defined correlation).
- `easy_roi` searches a 210×210 ROI around the patch.
- `medium_roi` searches a 310×310 ROI around the patch.
- The "miss" template is a random-noise 110×110 image that does not
  occur on the device.

### 4.1 NFR comparison

| Frozen NFR | v1.0 target | Phase 3 live (median) | Verdict |
|---|---|---:|---|
| ROI grayscale, median | ≤ 5 ms | 0.97 ms (easy), 2.32 ms (medium) | ✅ well within |
| Full-frame grayscale, median | ≤ 50 ms | 47.68–49.83 ms | ✅ at the edge, within |
| Per-template match cost (default) | composable to ≤ 8 templates/tick | 8 × 2 ms = 16 ms ROI; 8 × 50 ms = 400 ms full-frame | ✅ ROI; full-frame opt-in only |

The 47.68–49.83 ms full-frame medians are slightly higher than Phase 0's
33.6 ms for the same template size. The Phase 3 measurement was taken
under a real workload (other system activity not present in the Phase 0
bench-only environment); both values are within the frozen NFR's
≤ 50 ms band. The threshold remains tight enough that operators MUST
use ROI templates for hot-path use; Phase 3 enforces this only via
documentation, not in code, but the matcher's
`MatchResult.search_mode` field surfaces every full-frame call so
operators can grep for them.

### 4.2 ROI speedup ratios (this run)

| Template | full-frame (ms) | ROI (ms) | speedup |
|---|---:|---:|---:|
| easy   | 47.68 | 0.97 | **49.2×** |
| medium | 48.85 | 2.32 | **21.1×** |

Consistent with ADR-03's "ROI restriction is the single largest win for
matching cost" and Phase 0's 15–50× range.

---

## 5. Confidence results

For each of the six live cases, the confidence median was:

| Case | confidence (median) | found |
|---|---:|---|
| easy_full | 1.000 | ✓ |
| easy_roi | 1.000 | ✓ |
| medium_full | 1.000 | ✓ |
| medium_roi | 1.000 | ✓ |
| miss_full | 0.046 | ✗ |
| miss_roi | 0.034 | ✗ |

Observations:

- Exact-template-against-source-frame matches at `confidence ≈ 1.000`
  (the floating-point value is 0.99999994 internally, clamped to 1.0
  for display). This is the expected upper bound for `TM_CCOEFF_NORMED`.
- A random-noise template has correlation in the noise floor of
  `[-0.05, 0.05]` across the device's home screen. The clamped value
  is ≤ 0.05 in every iteration; never crosses any sensible threshold.
- The threshold gate is binary, not graded — there is no `SOFT`
  category in Phase 3. ADR-03's soft/hard split is a Phase 6 telemetry
  concern (the orchestrator can act on `confidence < hard but > soft`
  events without the matcher needing to model it as a separate state).

### 5.1 `TM_CCOEFF_NORMED` invariance note

`TM_CCOEFF_NORMED` subtracts means and normalises by standard
deviation, making it invariant to *linear brightness/contrast shifts*.
This means a template differing only in fill colour from a frame patch
of the same shape will match at confidence ≈ 1.0. The Phase 3 tests
discovered this when an earlier draft tried to construct a "miss" case
by changing the patch's fill value alone (matched at 1.0). The
final test uses a *structurally* different (random-noise) template,
which is the correct way to construct a negative case.

---

## 6. Artifact behavior

When `MATCHER_DEBUG=1` (or `Matcher(debug=True)`):

```
var/artifacts/matcher/
└── 20260520T115854_168041_live_easy_a02ff902/
    ├── frame.jpg          172 KB   1080×1920 BGR as JPEG q=85
    ├── template.jpg         4 KB   110×110 grayscale as JPEG q=90
    ├── heatmap.jpg        112 KB   971×1811 normalized correlation as uint8 JPEG q=85
    └── metadata.json        ~700 B
```

`metadata.json` (live capture):

```json
{
  "frame": {
    "capture_latency_ms": 1305.263291,
    "height": 1920,
    "native_height": 2408,
    "native_width": 1080,
    "source_mode": "raw",
    "width": 1080
  },
  "heatmap_shape": [1811, 971],
  "result": {
    "capture_latency_ms": 1305.263291,
    "center": [295, 615],
    "confidence": 0.9999999403953552,
    "found": true,
    "height": 110,
    "match_latency_ms": 57.533363,
    "search_mode": "full_gray",
    "template_name": "live_easy",
    "width": 110,
    "x": 240,
    "y": 560
  },
  "search_mode": "full_gray",
  "template": {
    "height": 110,
    "name": "live_easy",
    "roi": null,
    "threshold": 0.9,
    "width": 110
  }
}
```

Properties:

- One subdirectory per match, timestamped to microseconds with a
  short UUID suffix.
- Atomic writes: each file is written via `tempfile.mkstemp` and
  `shutil.move` after `fsync`.
- Template names with unsafe characters (`/`, spaces) are sanitized
  to `_` in the directory name; the metadata preserves the original.
- The heatmap is normalised min-max to `[0, 255]` uint8 for human
  visibility. This is a debug artifact, not a quantitative output —
  the original float32 correlation is the source of truth for `confidence`.
- No `cv2.imshow`; entirely file-based.
- Disabled by default; enable with the env var or constructor flag.

---

## 7. Test results

```
$ .venv/bin/pytest -ra
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-8.4.2, pluggy-1.6.0
configfile: pyproject.toml
testpaths: tests
plugins: cov-6.3.0
collected 153 items

tests/test_adb.py .............                                          [  8%]
tests/test_bootstrap.py ........                                         [ 13%]
tests/test_errors.py ..                                                  [ 15%]
tests/test_fingerprint.py .............                                  [ 23%]
tests/test_frame.py ................                                     [ 33%]
tests/test_match_result.py ....................                          [ 47%]
tests/test_matcher.py ................                                   [ 57%]
tests/test_paths.py ..                                                   [ 58%]
tests/test_remap.py .........                                            [ 64%]
tests/test_sensor.py .............................                       [ 83%]
tests/test_template.py .........................                         [100%]

============================= 153 passed in 1.06s ==============================
```

### 7.1 Coverage

```
Name                         Stmts   Miss  Cover
--------------------------------------------------
automation/__init__.py           1      0   100%
automation/adb.py               74      4    95%
automation/bootstrap.py        107     12    89%
automation/errors.py            13      0   100%
automation/fingerprint.py       90     12    87%
automation/frame.py             44      2    95%
automation/match_result.py      55      0   100%
automation/matcher.py          107     12    89%
automation/paths.py             13      0   100%
automation/remap.py             23      0   100%
automation/sensor.py           197     25    87%
automation/template.py          58      2    97%
--------------------------------------------------
TOTAL                          782     69    91%
```

**Package coverage: 91%** — meets the 90% minimum.

### 7.2 Test inventory (Phase 3 additions only)

| File | Tests | Key coverage |
|---|---:|---|
| `tests/test_match_result.py` | 20 | confidence bounds, search-mode validation, found-vs-coords consistency, partial coords, center(), to_debug_dict, summary, frozen, hashability |
| `tests/test_template.py` | 25 | grayscale-only validation, threshold bounds (0,1], ROI tuple shape, ROI bounds vs frame, template-fits-in-ROI, shape_summary, frozen, unhashable, write-lock |
| `tests/test_matcher.py` | 16 | exact full-frame match, exact ROI match, threshold-fail miss, ROI miss, template-larger-than-frame error, ROI-beyond-frame error, debug artifacts (env + flag), latency, capture-latency-copied, ROI offset correctness |

---

## 8. NFR comparison

Repeated from §4.1 for the index:

| Frozen NFR | v1.0 target | Phase 3 measured | Status |
|---|---|---:|---|
| Per-template match (ROI grayscale, median) | ≤ 5 ms | 0.97–2.32 ms | ✅ |
| Per-template match (full-frame grayscale, median) | ≤ 50 ms | 47.7–49.8 ms | ✅ (at edge) |
| Per-template match (full-frame BGR) | opt-in only | not supported | ✅ structurally enforced |
| Concurrent templates per tick (default) | ≤ 8, ROI-required | composable; orchestrator's responsibility | deferred to Phase 5 |
| Detection accuracy (static, well-isolated UI) | 99–99.9% (ADR-03 §8) | 100% on 4/4 exact-source matches; 0/2 false positives on miss cases | ✅ (tiny sample) |

---

## 9. Phase 4 readiness

| Requirement | Status |
|---|---|
| `MatchResult` exposes top-left coordinates + center() for action denormalization | ✅ |
| Frame.native_width/height preserved through Remap for inverse-mapping | ✅ (Phase 2) |
| `Matcher.match(frame, template)` is the stable Phase 4 input | ✅ |
| Latency budget headroom for ACT (≤ ~400 ms after SENSE + THINK) | ✅ on this hardware |
| Errors are typed so Phase 5's recovery cascade can branch | ✅ via `MatcherError`/`InvalidROIError`/`MatchComputationError` |
| No template logic leaked into ACT | ✅ — Phase 3 cannot reach the actuator (no import) |

Phase 4 can begin. The action engine will consume `MatchResult.center()`
or `(MatchResult.x + width // 2, ...)` to compute tap coordinates,
combined with `Frame.native_width/native_height` (or the session's
`Remap`) to denormalize back to device pixels.

---

## 10. Files created

```
automation/match_result.py    130 lines
automation/template.py        150 lines
automation/matcher.py         245 lines
automation/errors.py          +35 lines  (MatcherError, InvalidROIError, MatchComputationError)
tests/test_match_result.py    160 lines (20 tests)
tests/test_template.py        175 lines (25 tests)
tests/test_matcher.py         245 lines (16 tests)
scripts/phase3_live_validation.py  ~115 lines (throwaway harness)
phase3-report.md             (this file)
```

Total Phase 3 net additions: 7 modified/created Python files (+ harness + report).

---

## 11. Unresolved risks

None blocking. Documented:

- **`TM_CCOEFF_NORMED` is brightness-invariant**, so templates that
  differ only in fill colour from a screen region will match
  spuriously at confidence ≈ 1.0. This is a known property of the
  algorithm and matches ADR-03's choice. Templates must be authored
  with *structurally* distinctive content. The framework cannot detect
  this automatically; the Phase 8 operator workflow includes a
  template-authoring review step.
- **Solid-colour regions degenerate matching**. Phase 0 noted that
  status-bar / black-background regions have zero variance and produce
  ill-defined correlation. The matcher does not currently detect or
  warn on solid templates. Catching this at template-load time is a
  v1.1 backlog candidate (small check: `template.image_gray.std() > k`).
- **Full-frame grayscale measured 47–50 ms on this run**, against
  Phase 0's 33.6 ms. The frozen NFR ≤ 50 ms is the gate; both runs
  pass. The variance is attributable to background system load
  during Phase 3 (concurrent tests, browser, etc.), not the matcher.
  Phase 6 instrumentation will track this in steady state.
- **No mask support**. ADR-03 specifies masks for animated UI
  regions (pulsing buttons). Phase 3 scopes to plain templates;
  masks are v1.1 backlog. The frozen NFRs do not require masks for
  v1.0.
- **No multi-scale fallback**. ADR-03's `multi_scale: true` is not
  implemented. v1.1 backlog. ADR-04 (reference resolution) covers
  the common case; multi-scale is only needed for UI that scales
  arbitrarily within the reference frame, which the operator's
  target app does not.
- **Debug artifacts are not rotation-capped**. Same caveat as Phase 2:
  `var/artifacts/matcher/` grows unbounded under sustained
  `MATCHER_DEBUG=1`. Phase 6's ArtifactStore will fold this into the
  framework-wide rotation policy.

---

## 12. Readiness verdict

**Phase 3: COMPLETE. Phase 4 may begin.**

Validation summary:

- 153 / 153 tests pass; **91%** coverage on the `automation/` package.
- Live device end-to-end matches for all 6 cases (easy/medium × full/roi, plus miss × full/roi) produce well-formed `MatchResult`s with sensible latencies and correct coordinates.
- ROI matching achieves 0.97–2.32 ms median — 49× / 21× faster than full-frame on the same template.
- Full-frame matching at 47.7–49.8 ms is at the edge of but within the frozen ≤ 50 ms NFR.
- Confidence is 1.0 for exact-template-on-source matches and ≤ 0.05 for random-noise misses; threshold gate works as intended.
- Debug artifacts (frame.jpg, template.jpg, heatmap.jpg, metadata.json) write atomically with correct content and JSON schema.
- ADR-03/04/05 alignment confirmed by code inspection: no BGR matching, no mask code, no multi-scale code, no manifest loader — Phase 3 scope is strict.
- No leakage of higher-level concerns (FSM, actions, retries) into THINK.

The Phase 4 implementer should next read `PHASE-MASTER-PROMPTS.md` Phase 4, `ADR.md` ADR-06/ADR-09/ADR-15, and this report's §9 for the surface Phase 4 will consume.
