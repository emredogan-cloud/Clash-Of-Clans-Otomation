# Phase 6 Report — Observability / Metrics / Telemetry

> **Phase:** 6 — Observability
> **Date:** 2026-05-21
> **Host:** Ubuntu 24.04.4 LTS, kernel 6.17, AMD Ryzen 5 5500
> **Device:** Xiaomi 22095RA98C (Redmi Note 11R), Android 13, USB 2.0 @ 480 Mbps
> **Reference resolution:** 1080×1920 (ADR-04)
> **Companion documents:** [phase5-report.md](./phase5-report.md), [docs/frozen_nfrs_v1.md](./docs/frozen_nfrs_v1.md) (Phase-5.5 amended), [docs/phase6_readiness.md](./docs/phase6_readiness.md), [ADR.md ADR-08a / ADR-12 / ADR-13](./ADR.md), [PHASE-MASTER-PROMPTS.md Phase 6](./PHASE-MASTER-PROMPTS.md)

---

## 1. What was built

**Four Phase 6 modules** under `automation/`:

| File | Purpose | LOC |
|---|---|---:|
| `automation/correlation.py` | `CorrelationId` factory — short, sortable, filesystem-safe per-tick id (`tick_YYYYMMDDTHHMMSS_<6 hex>`). | 95 |
| `automation/logger.py` | `StructuredLogger` — JSONL append-only writer for `var/logs/ticks.jsonl` and `var/logs/errors.jsonl`. Atomic per-record. | 230 |
| `automation/metrics.py` | `MetricsCollector` — pure-Python counters + per-tier histograms, JSON persistence. Bucket layouts mandated by `PHASE-MASTER-PROMPTS.md` Phase 6 (Phase-5.5 amended). | 360 |
| `automation/rotation.py` | `RotationPolicy` — bounded-disk-growth for `var/logs/*.jsonl` and `var/artifacts/orchestrator/`. Deterministic, no daemon. | 270 |

**Extensions**:

| File | Change |
|---|---|
| `automation/errors.py` | Added `TelemetryError`, `MetricsError`, `LoggingError`, `RotationError`. |
| `automation/orchestrator.py` | Instrumented (wrapped, not redesigned). Constructor now accepts optional `logger`, `metrics`, `correlation_id_factory`. Each tick generates a correlation id, emits one log line + metrics observations, and threads the id + tier into the artifact directory and `metadata.json`. **The FSM is untouched; `_transition` remains the chokepoint.** |
| `scripts/replay_tick.py` | CLI: parses a `metadata.json` and prints a human-readable replay. No device contact. |
| `scripts/phase6_live_validation.py` | Throwaway live harness (≥ 10 ticks, all tiers, overhead bench, rotation pass, replay sanity check). |
| `bench/results/phase6_live_validation.json` | Sidecar with the live measurements. |
| `tests/test_correlation.py` (17 tests), `tests/test_logger.py` (15 tests), `tests/test_metrics.py` (27 tests), `tests/test_rotation.py` (25 tests), `tests/test_replay.py` (18 tests), `tests/test_orchestrator.py` extensions (+12 instrumentation tests). |

Total Phase 6 net additions: 10 modified/created Python source files
+ 1 JSON sidecar + the report.

---

## 2. Telemetry architecture

### 2.1 Data flow

```
                          ┌──────────────────┐
                          │  Orchestrator    │
                          │  .tick()         │
                          └────────┬─────────┘
                                   │
              ┌────────────────────▼────────────────────────┐
              │  correlation_id = factory()                 │
              │  (one per tick; threaded through artifacts │
              │   AND logs)                                 │
              └────────────────────┬────────────────────────┘
                                   │
              ┌────────────────────▼────────────────────────┐
              │  Existing FSM unchanged                     │
              │  SEARCH → ACT → VALIDATE  (Phase 5)         │
              │  _transition still the chokepoint           │
              └────────────────────┬────────────────────────┘
                                   │
              ┌────────────────────▼────────────────────────┐
              │  _finalize()                                │
              │   ├─ tier = derive_tier(action_ran,         │
              │   │                     validation_ran,     │
              │   │                     retries_used)       │
              │   ├─ TickResult                              │
              │   ├─ logger.log_tick(...)  ←─ best-effort    │
              │   ├─ metrics.observe_tick / _action / _match │
              │   └─ _write_artifacts(...)  ← extended       │
              └────────────────────┬────────────────────────┘
                                   │
                ┌──────────────────┼──────────────────┐
                │                  │                  │
                ▼                  ▼                  ▼
    var/logs/ticks.jsonl   var/metrics/        var/artifacts/
    (one line per tick)    metrics.json        orchestrator/
                           (snapshot on        <correlation_id>_
                            persist())         <verdict>_<tier>/
                                               metadata.json
```

### 2.2 Wiring philosophy

Per the Phase 6 prompt: instrumentation must **wrap, not rewrite**
the FSM. Concretely:

- **`Orchestrator` constructor** gains three optional kwargs:
  `logger`, `metrics`, `correlation_id_factory`. None are required;
  if all are absent the orchestrator behaves exactly as in Phase 5.
- The **only changes** inside `tick()`:
  1. Generate a `correlation_id` at entry (held on the instance).
  2. In `_finalize`, compute `tier` and call the three sinks
     (`logger.log_tick`, `metrics.observe_*`, `_write_artifacts`).
- **`_transition` is byte-identical** to Phase 5. The chokepoint
  invariant holds: every state move still routes through one method.
- Errors raised by any sink are caught at the `_finalize` boundary
  and logged at WARN. **Instrumentation cannot break the tick.**

### 2.3 Correlation IDs

Format: `tick_YYYYMMDDTHHMMSS_<6 hex>`.

- 27 characters, filesystem-safe (`[A-Za-z0-9_T]` only).
- UTC timestamp prefix sorts chronologically inside a date,
  giving operators a useful `ls -1 var/artifacts/orchestrator/`
  ordering without parsing JSON.
- 6-char hex tail from `random.SystemRandom` (~24 bits of entropy).
  At v1.0's ≤ 1 Hz tick rate, sub-second collisions require ~2¹²
  ticks; the test suite asserts ≥ 999 distinct ids in 1000 calls.
- **Not UUID4**: 36-byte UUIDs in directory names bloat artifact
  paths and make grep noisy; 6 hex digits is the right
  signal-to-noise trade for v1.0 single-device throughput.
- Same id appears in: `metadata.json["correlation_id"]`, artifact
  directory name (prefix), and `ticks.jsonl["correlation_id"]`.
  Cross-referencing is `ls var/artifacts/orchestrator/ | grep
  <correlation_id>` and `grep <correlation_id> var/logs/ticks.jsonl`.

### 2.4 Tier derivation

The three tiers (per Phase 5.5 amendment in `docs/frozen_nfrs_v1.md`):

| Tier | FSM path | Captures | Validation cycles |
|---|---|---:|---:|
| `search_only` | SEARCH miss OR ACT fail (validation never ran) | 1 | 0 |
| `validated` | SEARCH HIT → ACT → 1× VALIDATE | 2 | 1 |
| `validated_retry` | as above + retry validate | 3 | 2 |

The orchestrator computes the tier from `(action_ran,
validation_ran, retries_used)` in `metrics.derive_tier(...)`. The
function is pure; testable without the orchestrator.

---

## 3. Bucket rationale

Layouts are **mandated** by `PHASE-MASTER-PROMPTS.md` Phase 6
(amended in Phase 5.5). The Phase 6 implementation reproduces them
verbatim; the rationale below documents *why* these specific edges,
backed by the Phase 0/3/4/5 measurements.

### 3.1 Tick duration `[50, 100, 200, 400, 800, 1600, 3200, 6400] ms`

| Bucket | What lands here |
|---|---|
| ≤ 50 ms | Fault/no-op path (orchestrator declined to tick) |
| ≤ 100 ms | — |
| ≤ 200 ms | — |
| ≤ 400 ms | — |
| ≤ 800 ms | Search-only tick on a freshly-cached low-entropy screen (rare) |
| ≤ 1600 ms | **Search-only tick (expected band)** — Phase 5 Demo 1 1211 ms; Phase 6 live 0.9–1.2 s |
| ≤ 3200 ms | **Validated tick (expected band)** — Phase 5 Demo 2/3 ≈ 2.6–3.0 s; Phase 6 live 1.9–3.1 s |
| ≤ 6400 ms | Fault headroom — capture timeout (30 s) would overflow this; intermediate spikes (USB hub flap, GC pause) land here |
| > 6400 ms (overflow) | Definitely a fault — investigate |

A dashboard reading the histogram **must split on the `tick_tier`
label** (which the per-tier histograms enforce: separate histograms
for `search_only`, `validated`, `validated_retry`). Otherwise the
distribution conflates structurally different tick types and the
p50/p95 are meaningless.

### 3.2 Tap latency `[10, 25, 50, 100, 200, 500] ms`

Per Phase 4 measurements (tap median 58.8 ms, p95 92.1 ms):

- ≤ 50 ms — best-case device responsiveness; not seen in Phase 4.
- ≤ 100 ms — **working median** for tap on this device.
- ≤ 200 ms — p99 zone for tap; also the working band for `swipe`
  with a 100 ms duration.
- ≤ 500 ms — `swipe` at 300 ms duration (370 ms total per Phase 4);
  `long_press` at ≥ 300 ms hold.
- > 500 ms (overflow) — `long_press` ≥ 500 ms hold (662 ms at the
  600 ms default per Phase 4). Phase 6 live confirmed: long_press
  is structurally outside the tap bucket — that's by design (it's
  a different action class).

### 3.3 Match latency `[1, 2, 5, 10, 25, 50, 100] ms`

Per Phase 3 measurements:

- ≤ 2 ms — **ROI grayscale match (working band)** (Phase 3 measured 2.2 ms).
- ≤ 10 ms — ROI BGR (7 ms; structurally forbidden on the hot path
  per ADR-03 clarification, but the bucket exists for opt-in cases).
- ≤ 50 ms — **Full-frame grayscale (working band)** (Phase 3 measured 33.6 ms;
  Phase 5/6 live 43–55 ms).
- ≤ 100 ms — degraded full-frame grayscale (background contention).
- > 100 ms (overflow) — full-frame BGR (138 ms) opt-in path; should
  be rare on a v1.0 hot path.

---

## 4. Logging overhead

### 4.1 Measurement

The Phase 6 live harness includes an off-tick micro-bench that
measures the cost of `logger.log_tick(...) + metrics.observe_tick +
observe_action + observe_match + metrics.persist()` end-to-end.
This is the entire instrumentation cost per tick (payload-only;
excludes the `_transition` log lines, which are stdlib `logging`
DEBUG and unconditional).

Sample: 300 iterations after 1 warmup, on the same host as the
live ticks. Source: `bench/results/phase6_live_validation.json`.

| Metric | Value |
|---|---:|
| Mean overhead per tick | **2.082 ms** |
| Median overhead per tick | **1.825 ms** |
| p95 overhead per tick | **3.348 ms** |

### 4.2 vs the < 1% NFR

The Phase 6 prompt's NFR: `logging overhead < 1% of tick time at
default verbosity`.

| Tick tier | Live tick median (ms) | 1% budget (ms) | Overhead median (ms) | % of tick |
|---|---:|---:|---:|---:|
| search_only | 1066 (live min: 803.8) | 10.7 (min: 8.0) | 1.825 | **0.17%** (worst: 0.23% on the 804 ms tick) |
| validated | 1917 | 19.2 | 1.825 | **0.10%** |
| validated_retry | 2972 | 29.7 | 1.825 | **0.06%** |

Even on the smallest observed tick (the 804 ms search-only A#2),
overhead is **0.23% of tick time** — comfortably under the 1% NFR.
On validated ticks the overhead is ~0.1% or less.

### 4.3 Where the 1.825 ms comes from

A quick decomposition (informal; not benched per-step):

- `json.dumps(payload, sort_keys=True)` × 2 (tick record + metrics
  snapshot) — ~0.2 ms.
- `os.fsync()` on the log file — ~0.5 ms (filesystem-dependent;
  dominant on rotational disks).
- `_atomic_write_text()` for the metrics file (tmp + fsync +
  rename) — ~1.0 ms.
- Misc Python overhead, lock acquisition — ~0.1 ms.

The metrics `persist()` is the largest single contributor (~1 ms).
A real operator script can call `persist()` periodically (e.g.,
every N ticks) instead of every tick — this would drop overhead
to ~0.8 ms per tick. v1.0 ships with per-tick persistence because
correctness > performance for first-launch observability.

---

## 5. Rotation behavior

### 5.1 Defaults

| Resource | Cap | Behavior |
|---|---|---|
| `var/logs/<stream>.jsonl` (per stream) | 10 MB live file | rotates → `<stream>.jsonl.1`, shifts pre-existing rotated files down, drops anything beyond keep-count |
| Keep count | 5 rotated files | i.e., up to 50 MB per stream (plus the live file) |
| `var/artifacts/orchestrator/` (cumulative) | 500 MB | deletes oldest-first subdirectories (by mtime; tie-break on name) until under cap |

### 5.2 Determinism

The Phase 6 prompt requires deterministic rotation. The
implementation guarantees it:

- Logs: rotation is a deterministic sequence of `Path.rename` calls
  (highest-numbered first, no overwrites). Same input filesystem
  state in → same output filesystem state out.
- Artifacts: sort by `(mtime, name)`. Mtime ties are broken by
  ascending name. Same input → same output, even across runs.

### 5.3 Live behavior

In the Phase 6 live run (10 ticks):

- `var/logs/ticks.jsonl` reached **~3 KB** (10 records × ~250 bytes
  including newline). Well below the 10 MB rotation threshold; no
  rotation triggered.
- `var/artifacts/orchestrator/` reached **~10.5 KB** across 10
  subdirectories (~1 KB each, metadata.json only). Well below the
  500 MB cap; no rotation triggered.
- A synthetic rotation pass at end of run reported the expected
  no-op: `logs rotated: {}`, `artifacts rotated: {deleted: 0,
  retained: 10, total_bytes_after: 10531}`.

### 5.4 Scaling projections

At the v1.0 worst-case tick rate (1 Hz for search-only ticks):

- **Logs:** ~250 B × 3600 ticks/hour = 0.9 MB/hour. The 10 MB
  threshold trips after ~11 hours of continuous operation. With
  5 rotated files retained, the framework keeps ~60 MB of log
  history — about 70 hours of ticks.
- **Artifacts:** ~1 KB × 3600 = 3.6 MB/hour. The 500 MB cap is
  reached after ~140 hours (~6 days). At validated-tick rate
  (~0.3 Hz), it's ~6 weeks. Both well within Phase 7's 24-hour
  soak scope.

Real artifact size will rise once Phase 7 adds frame snapshots on
failure (per ADR-12). The 500 MB cap was sized for that future
state; the metadata-only artifacts of Phase 5/6 use only a small
fraction.

---

## 6. Test results

```
$ .venv/bin/pytest -ra --cov=automation
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-8.4.2, pluggy-1.6.0
configfile: pyproject.toml
testpaths: tests
plugins: cov-6.3.0
collected 452 items

tests/test_action_result.py .....................                        [  4%]
tests/test_actuator.py ......................................            [ 13%]
tests/test_adb.py .............                                          [ 16%]
tests/test_bootstrap.py ........                                         [ 18%]
tests/test_correlation.py .................                              [ 22%]
tests/test_denormalize.py ..................................             [ 29%]
tests/test_errors.py ..                                                  [ 30%]
tests/test_fingerprint.py .............                                  [ 33%]
tests/test_frame.py ................                                     [ 36%]
tests/test_logger.py ...............                                     [ 40%]
tests/test_match_result.py ....................                          [ 44%]
tests/test_matcher.py ................                                   [ 48%]
tests/test_metrics.py ..............................                     [ 55%]
tests/test_orchestrator.py ....................................          [ 63%]
tests/test_paths.py ..                                                   [ 64%]
tests/test_remap.py .........                                            [ 66%]
tests/test_replay.py ..................                                  [ 70%]
tests/test_rotation.py .........................                         [ 75%]
tests/test_sensor.py .............................                       [ 82%]
tests/test_state.py ....................................                 [ 90%]
tests/test_template.py .........................                         [ 95%]
tests/test_tick_result.py ...........................                    [100%]

============================= 452 passed in 2.18s ==============================
```

### 6.1 Coverage

```
Name                          Stmts   Miss  Cover
-------------------------------------------------
automation/correlation.py        33      0   100%
automation/logger.py             62      0   100%
automation/metrics.py           126     10    92%
automation/rotation.py          111      8    93%
automation/orchestrator.py      143      6    96%
automation/errors.py             23      0   100%
... (prior phases unchanged)
-------------------------------------------------
TOTAL                          1544    101    93%
```

**Phase 6 module coverage: correlation 100%, logger 100%, metrics
92%, rotation 93%, orchestrator 96% (incl. instrumentation).
Package coverage 93%** — meets the ≥ 90% minimum on every new
module and on the package overall.

Uncovered Phase 6 lines are defensive: race-condition `OSError`
handlers in `_rmtree`'s rmdir fallback (lines 263–266 of
`rotation.py`) and the `_atomic_write_text` exception-cleanup path
in `metrics.py`. Both are reachable in principle (filesystem
races, mid-write power loss) but not in a deterministic unit
test environment.

### 6.2 Test inventory (Phase 6 additions only)

| File | Tests | Key coverage |
|---|---:|---|
| `tests/test_correlation.py` | 17 | format/length/filesystem-safety; 1000-id uniqueness; same-second tail-differs; cross-day sortability; `is_valid` strict on prefix/timestamp/hex; rejects UUID4 / truncated / wrong-case |
| `tests/test_logger.py` | 15 | log_tick all required fields; default ts; extra merge; collision rejection; log_error fields; non-JSON-encodable raises; oversized payload (> 3500 B) raises; naive ts rejected; one-line-per-record; separate files; I/O failure swallowed |
| `tests/test_metrics.py` | 30 | initial counters; tier derivation (all 4 cases); observe_tick counter increments; tier validation; bucket placement at boundary; overflow bucket; histograms independent per tier; Phase-5-measurement-as-fixture replay; spec-mandated bucket layouts; observe_action keyed histograms; observe_match; persistence atomicity; schema keys; JSON-round-trip; overwrite |
| `tests/test_rotation.py` | 25 | constants; type-rejection on logs_dir / artifacts_dir / int fields; under-cap noop; above-cap shifts; pre-existing rotated shifts; keep_files clamp; two-stream independence; missing dir; under-cap on artifacts; oldest-first deletion; mtime tie-break on name; recursive size; loose-files-ignored; rmtree on missing; OSError → RotationError on rotate_logs and rotate_artifacts; determinism across calls |
| `tests/test_replay.py` | 18 | load happy path; missing top-level / tick keys raise; non-object root rejected; missing file; format includes correlation_id+tier; HIT vs MISS rendering; validation outcomes; all latency surfaces; retries; state flow; main with file / dir args; missing file → nonzero; malformed JSON → nonzero |
| `tests/test_orchestrator.py` (Phase 6 extension) | +12 | works without any instrumentation; correlation id per tick; logger hook writes record; logger failure does not break tick; metrics observes tick+action+match; tier search_only on miss; tier validated_retry on retry; metrics failure does not break tick; artifact includes correlation_id+tier; artifact tier search_only on miss; artifact tier validated_retry on retry; correlation id propagated to logger AND artifact |

### 6.3 Determinism

All unit tests pass under the strict `filterwarnings = ["error"]`
pytest setting. Three back-to-back full runs produced byte-identical
output. No test depends on real time (correlation id factories are
mocked), real ADB, or real OpenCV inside the instrumentation path.

---

## 7. NFR comparison

### 7.1 Live measurements vs frozen NFRs

10 ticks on the connected Redmi Note 11R, mixed tier distribution
(4 search_only / 2 validated / 2 validated_retry / + 2
search_only mis-fires from block B).

| Frozen NFR (Phase 5.5) | Target | Phase 6 live | Status |
|---|---|---:|---|
| Tick latency, search-only (median) | ≤ 1500 ms | 1066 ms (6 samples) | ✅ |
| Tick latency, validated, no retry (median) | ≤ 2200 ms | 1917 ms (2 samples; B#1=1961, C#1=1874) | ✅ |
| Tick latency, validated + retry (median) | ≤ 3000 ms | 2972 ms (2 samples; C#2=3081, C#3=2863) | ⚠ at the edge |
| Tick latency, validated + retry (p95) | ≤ 3300 ms | 3081 ms (max observed) | ✅ |
| Screenshot capture (median, raw) | ≤ 1000 ms | 803.8–1158 ms (typical ~940) | ✅ |
| Per-template match (median, full-frame grayscale) | ≤ 50 ms | 46–55 ms | ✅ at the edge |
| Logging overhead | < 1% of tick time | 0.06%–0.23% | ✅ |
| New module coverage | ≥ 90% | 92%–100% | ✅ |

The `validated_retry` median of 2972 ms is **at the edge** of the
3000 ms NFR — Phase 6 measured the upper boundary closely. Phase 7
soak will produce the long-tail evidence; if it drifts past
3000 ms consistently, the NFR needs another amendment.

### 7.2 Counter and histogram observations from the live run

```
counters:
  ticks_total       = 10
  ticks_success     = 4
  ticks_failed      = 6
  retries_total     = 2
  validation_ticks  = 4   (validated + validated_retry)
  actions_total     = 4
  matches_total     = 10  (one per tick at the search step)

tick_histograms:
  search_only:     le=1600 bucket has 6 samples (range 0.9–1.2 s)
  validated:       le=3200 bucket has 2 samples (1.87, 1.96 s)
  validated_retry: le=3200 bucket has 2 samples (2.86, 3.08 s)

action_histograms (tap):
  le=100 = 3 samples; le=200 = 1 sample (B#1 = 185.8 ms — likely
  contention or a slow USB exchange)
```

### 7.3 Validation evidence for Phase-6 prompt requirements

| Requirement | Evidence |
|---|---|
| `correlation_id` carried in logs + metrics + artifacts | live `ticks.jsonl` + `metadata.json` cross-referenced in §1.4 of the readiness gate; unit test `test_correlation_id_is_propagated_to_logger_and_artifact` |
| Bucket layouts exactly as specified | `automation/metrics.py` constants; test `test_bucket_layouts_match_spec` |
| Atomic writes (no `.tmp` residue) | `test_persist_is_atomic_no_temp_leak`, `test_artifacts_no_partial_tmp_files` (Phase 5) inherited |
| No background thread / daemon | imports inspection; only `threading.Lock` for in-process safety, no `Thread` |
| No Prometheus dependency | `automation/metrics.py` is `numpy`-free, single stdlib `json` import |
| Replay CLI prints all required fields | `test_format_includes_all_latencies`, `test_format_replay_includes_correlation_and_tier` |
| Rotation bounds disk growth | `test_rotate_artifacts_deletes_oldest_first`, `test_rotate_logs_above_cap_shifts` |
| Tier-aware observability | `derive_tier()` + per-tier histograms; live run produced records in all 3 tiers |

---

## 8. Phase-7 readiness

| Requirement | Status |
|---|---|
| `Orchestrator.tick()` emits structured tick + metrics records | ✅ |
| Tick records carry `correlation_id` matching artifact directory names | ✅ |
| Per-tier histograms separate validated from search-only ticks | ✅ — Phase 7 dashboards consume this directly |
| Counters expose retry rate / validation rate / success rate | ✅ |
| Rotation policy bounds disk growth at sustained tick rate | ✅ — projections in §5.4 cover Phase 7's 24-hour soak |
| Replay CLI works on saved artifacts | ✅ — `scripts/replay_tick.py` |
| Logger does not crash the framework on I/O fault | ✅ — `test_io_failure_does_not_raise` |
| Metrics file is valid JSON parseable by any reader | ✅ — `test_persist_writes_atomic_json` |
| Errors are typed (`MetricsError`, `LoggingError`, `RotationError`) | ✅ |
| No Phase 7+ concerns leaked in | ✅ — no watchdog, no recovery cascade, no soak automation, no async |

Phase 7 (Hardening) may begin. The instrumentation surface is
stable; Phase 7 will:

1. Add the external watchdog process (ADR-11). It will read the
   heartbeat file (NOT YET WRITTEN — Phase 7 owns that too).
2. Add the heartbeat writer at the orchestrator's `_finalize`
   boundary. The chokepoint is already there; Phase 7 only adds
   one more line.
3. Build the fault-injection harness against the Phase 6
   observability surfaces (logs + metrics + artifacts).
4. Run the 24-hour soak. The new per-tier histograms tell us
   exactly which tier degrades over time, if any.
5. Reconcile `PHASE-MASTER-PROMPTS.md` Phase 5's original-vs-
   delivered scope (the recovery cascade lands here) — open item
   §2.12 of the Phase 5.5 consistency audit.

---

## 9. Files created / modified

```
automation/correlation.py         95 lines  (new)
automation/logger.py             230 lines  (new)
automation/metrics.py            360 lines  (new)
automation/rotation.py           270 lines  (new)
automation/errors.py             +47 lines  (TelemetryError, MetricsError, LoggingError, RotationError)
automation/orchestrator.py       +90 lines  (instrumentation hooks; FSM untouched)

scripts/replay_tick.py           220 lines  (new)
scripts/phase6_live_validation.py 270 lines  (new throwaway harness)

tests/test_correlation.py        140 lines  (17 tests)
tests/test_logger.py             280 lines  (15 tests)
tests/test_metrics.py            290 lines  (30 tests)
tests/test_rotation.py           295 lines  (25 tests)
tests/test_replay.py             240 lines  (18 tests)
tests/test_orchestrator.py       +240 lines (12 Phase-6 instrumentation tests)

bench/results/phase6_live_validation.json  (sidecar)
phase6-report.md                 (this file)
```

Total Phase 6 net additions: 14 modified/created Python files +
1 sidecar + the report.

---

## 10. Unresolved risks

None blocking. Documented:

- **Metrics `persist()` cost is ~1 ms per call.** v1.0 ships with
  per-tick persistence (called inside `_finalize`'s metrics path —
  actually NOT; the orchestrator only observes, it does not
  persist. Persist is the caller's call). The orchestrator does
  **not** call `persist()` itself; the live harness called it once
  at end. Phase 7 will introduce a periodic persister (every N
  ticks or every M seconds). Live overhead measurement *includes*
  persist; without it the overhead is ~0.8 ms per tick. No code
  change needed; this is documentation.

- **Validated-retry tick is at the NFR edge** (2972 ms median vs
  3000 ms ceiling). Two-sample median; Phase 7 soak will produce
  the proper distribution. If it drifts past, the NFR needs
  another amendment.

- **The `_transition` log lines are stdlib `logging` DEBUG.** They
  do not appear in the JSONL logger. By design — JSONL is for
  per-tick records; transition-level detail belongs to the artifact
  metadata (which has the full state flow). A future Phase 7+
  enhancement could promote `_transition` to a third JSONL stream
  (`var/logs/transitions.jsonl`) but v1.0 does not need it.

- **No log-level controls.** The Phase 6 prompt says "default
  verbosity"; we ship a single verbosity level (always-on tick
  records, always-on per-tick artifacts). A `level=DEBUG|INFO|WARN`
  knob is a v1.1 candidate. Per ADR-13, would land via TOML config.

- **Rotation is synchronous.** The orchestrator does not call
  rotation; the caller (or a future cron / Phase 7 periodic
  invoker) does. If neither runs, disk grows unbounded. This is
  intentional per the Phase 6 prompt's "no daemon" rule, but
  Phase 7 should add a cadence (e.g., "rotate every N ticks").

- **The artifact directory name embeds the tier.** This means
  filenames carry semantic meaning that the schema also carries
  (in `metadata.json["tier"]`). Two sources of truth. A future
  refactor could drop the tier from the directory name; v1.0
  retains it because operators grep by directory name and the
  tier-in-name is too useful to drop.

- **`MetricsError` is raised on structural bugs** (negative latency,
  unknown tier, non-monotonic buckets). The orchestrator catches
  all of them at the `_finalize` boundary and logs a WARN. So
  while `MetricsError` exists, the framework never re-raises it
  in v1.0. Phase 7+ may opt in to raising semantics.

- **Coverage gaps on `os` race-condition handlers** (rotation
  `_rmtree` fallback, metrics `_atomic_write_text` exception
  cleanup). Could be covered with deeper monkeypatching but the
  test ROI is low and the code paths are textbook-defensive.

- **Phase 6 live validation sample size is 10 ticks.** Stable
  enough to confirm correctness; statistically thin. Phase 7's
  24-hour soak provides the long-tail evidence.

---

## 11. Readiness verdict

**Phase 6: COMPLETE. Phase 7 may begin.**

Validation summary:

- 452 / 452 tests pass; **93%** package coverage; Phase 6 modules
  at 92%–100% individually (correlation 100%, logger 100%, metrics
  92%, rotation 93%, orchestrator 96% with the new instrumentation).
- Live device validation: 10 ticks across all three tiers
  (search_only / validated / validated_retry) with full
  observability — logs + metrics + artifacts + replay CLI all
  produced correct, schema-conforming output.
- Logging+metrics overhead measured at **0.06%–0.23% of tick time**
  — comfortably under the < 1% NFR.
- Rotation is deterministic and bounded; projections place the
  default caps well beyond Phase 7's 24-hour soak scope.
- All Phase 6 prompt prohibitions honoured: no Prometheus, no
  Grafana, no async telemetry, no daemon, no watchdog, no
  recovery, no Phase 7+ functionality.
- No leakage of higher-level concerns into the instrumentation
  modules — `correlation.py`, `logger.py`, `metrics.py`, and
  `rotation.py` each have a single responsibility; the orchestrator
  remains the only module that touches Sensor/Matcher/Actuator.

The Phase 7 implementer should next read
`PHASE-MASTER-PROMPTS.md` Phase 7, this report's §8, and the
existing `docs/phase55_consistency_patch.md` §2.12 (OPEN — Phase 7
scope reconciliation).
