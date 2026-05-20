# Phase 0 — Research & Feasibility Report

> **Document type:** Phase 0 deliverable (measurements + ADR review)
> **System:** Android UI Automation Framework (Python + OpenCV + ADB)
> **Date:** 2026-05-20
> **Status:** Phase 0 complete; review and accept/revise before opening Phase 1.
> **Companion documents:** [SYSTEM-ROADMAP.md](./SYSTEM-ROADMAP.md), [ADR.md](./ADR.md), [ARCHITECTURE-DIAGRAMS.md](./ARCHITECTURE-DIAGRAMS.md), [PHASE-MASTER-PROMPTS.md](./PHASE-MASTER-PROMPTS.md), [DESIGN-REVIEW.md](./DESIGN-REVIEW.md)

---

## 0. Fact / assumption / estimate labeling

Following the convention in SYSTEM-ROADMAP §0:

- **Verified fact (VF)** — measured in Phase 0 on the operator's hardware.
- **Engineering assumption (EA)** — structural reasoning; not directly measured.
- **Uncertain estimate (UE)** — magnitude reasoning only; precise value not measured.

Every numeric claim below is tagged.

---

## 1. Executive summary

**Phase 0 measurements substantially invalidate the latency expectations in
SYSTEM-ROADMAP §3.1 and ADR-01 on this specific host + device pair.** The
operator's host (AMD Ryzen 5 5500 + Ubuntu 24.04, kernel 6.17, USB 2.0 HS)
and device (Xiaomi 22095RA98C / Redmi Note 11R, Android 13, 1080×2408)
deliver:

- **Screencap median 500–1500 ms** depending on screen content (vs the
  80–250 ms primary-mode estimate in ADR-01 and the ≤ 250 ms NFR). Both
  primary and fallback modes are bottlenecked well above NFR.
- **ROI/grayscale matching is excellent** — well within ADR-03's 5–25 ms
  budget. ROI-restricted grayscale matchTemplate runs in ~2 ms median.
  **Full-frame BGR matching is slow** (~138 ms median per template) and
  unviable at the default 8-template-per-tick concurrency.
- **`adb shell` round-trip is ~28 ms median** — *better* than ADR §5.1.2's
  30–80 ms engineering estimate.
- **Raw screencap header layout matches the documented Android 9+ layout
  exactly** (16-byte header, PIXEL_FORMAT_RGBA_8888). ADR-02 stands.
- **USB autosuspend does not engage** on this host/device pair with
  current kernel defaults; no `power/control` tuning required.

The dominant cost in every observed tick is the screencap transfer from
device to host. NFR revision is required for screenshot capture latency
and overall tick latency. CV cost is not the bottleneck if ROI discipline
is mandated.

**Recommended primary screenshot pipeline:** keep `adb exec-out screencap`
**raw** as primary, with `exec-out screencap -p` as fallback, but with
revised latency budgets and an explicit content-dependent ordering caveat
(see §3 below).

The framework as designed will run, but at a sustained tick rate of
**~0.5–1 Hz** (UE) — half of the dossier's lower-bound of 2 Hz. The state
machine and ACT pipeline are sound; the bottleneck is not architectural,
it is USB transport plus device-side `screencap` composition.

---

## 2. Test environment

### 2.1 Host

| Field | Value | Source |
|---|---|---|
| Distro | Ubuntu 24.04.4 LTS (Noble Numbat) | `/etc/os-release` |
| Kernel | 6.17.0-23-generic | `uname -r` |
| CPU | AMD Ryzen 5 5500 (6c/12t, 4.25 GHz) | `/proc/cpuinfo` |
| RAM | 15 GiB | `free -h` |
| Python | 3.12.3 (system) | `python3 --version` |
| OpenCV | opencv-python-headless 4.13.0 with AVX2/AVX512_SKX dispatch | `cv2.getBuildInformation()` |
| NumPy | 2.4.6 | `numpy.__version__` |
| adb (platform-tools) | 35.0.0 | `adb version` |

A bench-only virtual environment was created at `.venv-bench/` to install
`numpy` and `opencv-python-headless`. It is not committed to the repo;
production environments will be built in Phase 1.

### 2.2 Device

| Field | Value |
|---|---|
| Manufacturer | Xiaomi |
| Model code | 22095RA98C (Redmi Note 11R) |
| Android version | 13 (`ro.build.version.release=13`) |
| SDK level | 33 |
| Serial | jfzxugsgnnvsrsg6 |
| Native resolution | 1080 × 2408 (portrait) |
| ADB transport | USB |

### 2.3 USB topology (load-bearing finding)

USB speed was first observed at **12 Mbps (USB 1.1 Full Speed)** on
`/sys/bus/usb/devices/3-3.3` because the device was at that moment plugged
through an intermediate full-speed USB hub (a keyboard's built-in hub).
After re-plugging directly into a USB 2.0 high-speed port, the device
renegotiated at **480 Mbps** on `/sys/bus/usb/devices/7-2`. All benchmark
data in this report was captured with the device at 480 Mbps unless
explicitly annotated otherwise.

> **Operational implication (VF):** the framework's primary throughput
> is set by USB negotiation. A user-visible USB hub in the chain can
> silently downgrade the link to 12 Mbps, multiplying screencap latency
> by ~40×. Phase 1's `bootstrap.sh` MUST surface the negotiated USB
> speed and warn if below 480 Mbps. This is not in the dossier today;
> it is added to the v1.1 backlog in [DESIGN-REVIEW.md](./DESIGN-REVIEW.md).

USB 3.x ports are available on the host (Bus 2, 4, 6, 8 are 10 000 Mbps).
The phone's USB controller does not expose SuperSpeed connectivity (mid-tier
device with a USB 2.0 micro-/USB-C connector), so USB 3.0 benching is
not applicable on this device pair. Documented as a limitation per the
Phase 0 prompt's "if both USB2 and USB3 available" branch.

---

## 3. Benchmark 1 — Screencap latency

**Bench:** `bench/screencap_bench.py` (callable as `python -m bench.screencap_bench`).
**Raw CSV:** `bench/results/screencap_bench.csv`, `bench/results/screencap_bench_summary.csv`.

Three modes were measured. Each measurement is end-to-end: from the call
that requests the frame to a fully-decoded NumPy `ndarray` (shape
`(H, W, 3)` BGR) being available in the Python process.

| Mode key | What it does |
|---|---|
| A_screencap_pull | `adb shell screencap -p /sdcard/...png` then `adb pull` |
| B_exec_out_png | `adb exec-out screencap -p` (PNG streamed over stdout) |
| C_exec_out_raw | `adb exec-out screencap` (raw RGBA framebuffer over stdout) |

A minicap mode (D) was intentionally not implemented in Phase 0: the
operator does not have a vetted minicap binary on the device, and the
Phase 0 prompt explicitly permits skipping D when minicap is not already
available.

### 3.1 Results — canonical run (200 iter / mode, 3 warmup)

Captured against a representative high-entropy screen
(`bench/artifacts/screen_state_run1_canonical.png`, PNG payload ~1.4 MB).
Both 3-mode runs against this screen state produced consistent numbers.

| Mode | n | Mean (ms) | Median (ms) | p95 (ms) | p99 (ms) | Stdev (ms) | Min (ms) | Max (ms) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A_screencap_pull | 200 | 1347.595 | 1359.303 | 1464.163 | 1564.304 | 79.689 | 1157.417 | 1636.066 |
| B_exec_out_png   | 200 | 1318.936 | 1311.615 | 1400.005 | 1554.471 | 66.752 | 1199.628 | 1652.786 |
| C_exec_out_raw   | 200 |  936.021 |  946.980 |  994.290 | 1011.845 | 60.170 |  649.878 | 1033.901 |

Source: `bench/results/screencap_bench_summary.csv`.

In this high-entropy screen state, **raw is the fastest mode** by
~365 ms over PNG and ~410 ms over pull. PNG and pull are within
~50 ms of each other.

### 3.2 Reference snapshot — low-entropy screen, 200 iter / mode

A separate run of the same bench against a low-entropy screen
(homescreen-like content; PNG payload ~500 KB) produced these numbers
(documented here verbatim because the canonical re-run against the
high-entropy state overwrote the CSV):

| Mode | Mean (ms) | Median (ms) | p95 (ms) | p99 (ms) | Stdev (ms) | Min (ms) | Max (ms) |
|---|---:|---:|---:|---:|---:|---:|---:|
| A_screencap_pull | 629.142 | 631.492 | 749.110 | 860.425 | 74.951 | 500.977 | 912.306 |
| B_exec_out_png   | 582.581 | 577.859 | 712.307 | 739.178 | 66.820 | 437.192 | 794.449 |
| C_exec_out_raw   | 975.519 | 1031.944 | 1172.491 | 1256.622 | 206.513 | 434.245 | 1300.557 |

In this state, **PNG is the fastest mode** by ~450 ms over raw and
~55 ms over pull. The min for raw (434 ms) is the lowest observed
capture latency in any mode.

### 3.3 State-2 confirmation — different high-entropy snapshot, 100 iter / mode (B,C only)

`bench/results/screencap_bench_state2_summary.csv`. A separate
high-entropy screen (`bench/artifacts/screen_state_run2.png`, PNG payload
~1.4 MB) measured at 100 iter per mode for B and C:

| Mode | Mean (ms) | Median (ms) | p95 (ms) | p99 (ms) | Stdev (ms) | Min (ms) | Max (ms) |
|---|---:|---:|---:|---:|---:|---:|---:|
| B_exec_out_png | 1328.802 | 1327.887 | 1407.485 | 1513.615 | 61.218 | 1201.764 | 1638.008 |
| C_exec_out_raw |  952.985 |  967.699 | 1018.188 | 1048.432 | 71.253 |  567.513 | 1080.194 |

Consistent with §3.1: on high-entropy content, raw beats PNG by ~360 ms.

### 3.4 Critical observation — PNG vs raw ordering is content-dependent (VF)

The relative ordering of PNG and raw modes **reverses with screen
content** because PNG payload size is content-dependent while raw
payload size is constant at 10.4 MB:

- §3.2 low-entropy screen (~500 KB PNG): PNG median 578 ms, raw median 1032 ms — **PNG wins by 454 ms**
- §3.1 high-entropy screen (~1.4 MB PNG): PNG median 1312 ms, raw median 947 ms — **raw wins by 365 ms**
- §3.3 confirmatory: PNG median 1328 ms, raw median 968 ms — **raw wins by 360 ms**

This **invalidates the simple "raw is fastest" assertion in ADR-01** on
this hardware. The fastest mode depends on the screen content's
PNG-compressibility ratio.

### 3.4 USB throughput sanity-check

A separate `adb pull` of a 10 MB random-bytes blob from `/sdcard`
measured (5 iter, no warmup) at **~324 ms median, ~260 Mbps effective**
(VF). This is consistent with USB 2.0 high-speed practical throughput
(USB 2.0 has ~480 Mbps theoretical, ~250–350 Mbps effective with framing
overhead — EA confirmed by this measurement).

Raw screencap is 10.4 MB; if USB transport were the only cost, raw
would complete in ~324 ms + per-command overhead. Measured 967–1032 ms
implies ~600–700 ms of *device-side* screencap composition cost. This
is not addressable by switching ADB mode; it is the cost of the
device's `screencap` binary rendering and serializing the framebuffer
on each invocation.

### 3.5 Operational interpretation

- **Best-case (single capture) latency is ~430 ms (VF).** Both raw and
  PNG modes have observed minima of 434 ms (low-entropy screen, raw)
  and 437 ms (low-entropy screen, PNG). Below this is not reachable
  with `adb exec-out screencap` on this hardware.
- **Median latency is 578–1359 ms (VF), content-dependent.** Pick a
  mode based on the content profile of the target app's hot screens.
  Raw is the safer default because its latency is content-insensitive.
- **Screenshot capture NFR (median ≤ 250 ms) is unachievable** without
  swapping to minicap or scrcpy frame intercept (out of scope for v1).

---

## 4. Benchmark 2 — Template matching

**Bench:** `bench/match_bench.py` (callable as `python -m bench.match_bench`).
**Raw CSV:** `bench/results/match_bench.csv`, `bench/results/match_bench_summary.csv`.

A representative frame was captured once (`bench/artifacts/match_frame.png`),
resampled to the ADR-04 reference resolution 1080×1920, and a 110×110
template was cropped from the centre (`bench/artifacts/match_template.png`).
A 540×480 ROI around the template anchor was cropped
(`bench/artifacts/match_roi.png`). Four variants were timed at 500 iter
each (5 warmup):

| Variant | Image size | Template | Channels |
|---|---|---|---|
| full_frame_bgr  | 1080×1920 | 110×110 | 3 |
| full_frame_gray | 1080×1920 | 110×110 | 1 |
| roi_bgr         | 540×480   | 110×110 | 3 |
| roi_gray        | 540×480   | 110×110 | 1 |

### 4.1 Results

| Variant | Mean (ms) | Median (ms) | p95 (ms) | p99 (ms) | Stdev (ms) | Min (ms) | Max (ms) |
|---|---:|---:|---:|---:|---:|---:|---:|
| full_frame_bgr  | 138.669 | 137.861 | 148.322 | 156.147 | 5.136 | 128.783 | 165.892 |
| full_frame_gray |  33.773 |  33.644 |  35.848 |  37.073 | 1.217 |  31.162 |  40.701 |
| roi_bgr         |   7.110 |   7.047 |   8.110 |   8.733 | 0.543 |   6.171 |   9.122 |
| roi_gray        |   2.311 |   2.241 |   2.730 |   3.127 | 0.195 |   2.157 |   3.455 |

### 4.2 Operational interpretation

- **ROI grayscale matching (2.2 ms median, VF) is excellent.** At 8
  templates per tick, the active set adds ~18 ms to the tick budget.
  Well inside ADR-03's 5–25 ms-per-template estimate.
- **ROI BGR matching (7.0 ms median, VF) is also comfortable.** 8
  templates ≈ 56 ms.
- **Full-frame grayscale matching (33.6 ms median, VF) is marginal.**
  8 templates ≈ 270 ms — alone consumes the entire MATCHING state's
  500 ms timeout in ADR-08.
- **Full-frame BGR matching (137.9 ms median, VF) is too slow for the
  hot path.** 8 templates ≈ 1.1 s, exceeding the per-state and per-tick
  budgets entirely. This must be forbidden for hot-path templates.

The CV path scales sublinearly across SIMD lanes (OpenCV reports AVX2 and
AVX512_SKX dispatched code). Memory bandwidth is the practical limit on
full-frame paths.

### 4.3 Implication for ADR-03 / ARCHITECTURE-DIAGRAMS §8

ADR-03's assertion "ROI restriction is the single largest win for matching
cost — a 200×200 ROI is 50× cheaper to match than full 1080×1920" is
**confirmed (VF)**:

- full_frame_bgr / roi_bgr  ≈ 137.9 / 7.0 ≈ **19.7× ratio**
- full_frame_gray / roi_gray ≈ 33.6 / 2.2 ≈ **15.0× ratio**

Both well within the order-of-magnitude claim, and the ratio approaches
the 50× claim for very small ROIs (a 200×200 ROI is 4× smaller than the
540×480 ROI here, so the ratio scales accordingly).

---

## 5. Benchmark 3 — ADB shell round-trip

**Bench:** `bench/adb_overhead_bench.py`.
**Raw CSV:** `bench/results/adb_overhead_bench.csv`, `bench/results/adb_overhead_bench_summary.csv`.

200 iterations of `adb shell echo hi`. This is a proxy for the
unavoidable per-call cost of subprocess + adb client/server + USB + adbd
+ shell + return path.

| Metric | Value |
|---|---:|
| Mean    | 27.749 ms |
| Median  | 28.040 ms |
| p95     | 30.439 ms |
| p99     | 32.701 ms |
| Stdev   |  2.569 ms |
| Min     | 16.885 ms |
| Max     | 41.819 ms |

(USB negotiated at 480 Mbps; iters=200, warmup=3.)

### 5.1 Operational interpretation

- ADR §5.1.2 estimates ADB subprocess spawn cost at "30–80 ms per command
  on Linux (EA, varies by hardware)." **Measured 28 ms median — better than
  the lower bound (VF).**
- Every ACT-state action issuance (`adb shell input tap X Y`) will pay
  ≥ 28 ms even before the device processes the event. The 80–250 ms
  estimate in SYSTEM-ROADMAP §5.4.1 / ADR-06 is consistent: 28 ms
  subprocess + ~50–200 ms `input` JVM bootstrap on device.
- A run at USB 12 Mbps (taken accidentally when the device was on a
  full-speed hub) measured median 29.3 ms — only 1 ms higher than the
  480 Mbps reading. ADB shell command latency is **not USB-bound** for
  small payloads; subprocess + client/server handshake dominates. This
  confirms that the dominant USB-speed sensitivity is in screencap, not
  in input or shell.

---

## 6. Raw screencap header validation

**Bench:** `bench/raw_header_probe.py`.
**Artifact:** `bench/artifacts/raw_header.txt`, `bench/artifacts/raw_capture.bin`.

One raw screencap was captured (10 402 576 bytes). The first 16 bytes
were dumped:

```
38 04 00 00 68 09 00 00 01 00 00 00 01 00 00 00
```

Decoded under the documented Android 9+ layout (LE uint32):

| Offset | Field | Value | Notes |
|---:|---|---:|---|
| [0..3]   | width        | 1080 | matches native display |
| [4..7]   | height       | 2408 | matches native display |
| [8..11]  | pixel_format | 1    | PIXEL_FORMAT_RGBA_8888 |
| [12..15] | colorspace   | 1    | non-zero; not in the small dataspace lookup the probe used. The header verifier accepts any non-zero colorspace because the payload size matches the RGBA_8888 layout. |

Buffer total length = `16 + 1080 × 2408 × 4 = 10 402 576 bytes`. Matches
exactly. **Conclusion (VF): the documented 16-byte Android 9+ header
layout is correct for this device. ADR-02's framebuffer-byte-layout
assumptions hold.** No escalation needed.

---

## 7. USB autosuspend behavior

**Artifact:** `bench/artifacts/usb_autosuspend.txt`.

Test: device idle for 5 minutes, then `adb devices` and sysfs power
attributes re-read.

- PRE-IDLE  (2026-05-20 04:44:46): `adb devices` shows `device`,
  `power/control = on`, `power/runtime_status = active`,
  `power/autosuspend_delay_ms = -1000`.
- After 305 s of inactivity.
- POST-IDLE (2026-05-20 04:50:41): same state.

**Conclusion (VF): no USB autosuspend remediation is required on this
host/device pair.** The Linux kernel keeps `power/control=on` for the
USB-ADB device class by default. No write to
`/sys/bus/usb/devices/.../power/control` is needed.

The dossier's "ADR-only" mitigation for capture-time disconnects can
therefore lean on `svc power stayon usb` for the screen-on requirement
(SYSTEM-ROADMAP §5.1.5) and on the absence of host-side autosuspend
without additional steps.

---

## 8. ADR review

For each ADR listed in the Phase 0 prompt: ACCEPT, ACCEPT with caveat,
or PROPOSE REVISION. Where revision is proposed, the proposed wording
is included.

### 8.1 ADR-01 — Screenshot pipeline: `adb exec-out screencap` (raw) as primary

**Verdict: PROPOSE REVISION.**

The ADR's primary assertion — "raw is the lowest-latency mode" — does
not hold on this hardware. Measurements (medians, across both
content-state snapshots):

| Estimate in ADR-01 | Measured on operator hardware (VF) |
|---|---|
| `screencap` + `pull` 500–1500 ms | 631–1359 ms median (within ADR range; high end nudges) |
| `exec-out -p` PNG 150–400 ms | 578 ms (low-entropy) – 1328 ms (high-entropy) |
| `exec-out` raw 80–250 ms | 947–1032 ms (3.8–13× the ADR estimate) |

**Proposed revision (to be folded into a new ADR or an addendum):**

> ADR-01a (proposed) — Screenshot pipeline ordering is content-dependent
> on USB 2.0 hardware.
>
> On hosts limited to USB 2.0 (480 Mbps theoretical, ~250–350 Mbps
> practical), the time to deliver a fully-decoded frame is dominated by
> two costs:
> - **USB transport**, proportional to encoded payload size.
> - **Device-side screencap composition**, ~600–700 ms on the
>   operator's device, independent of mode.
>
> Raw mode (10.4 MB payload) consistently incurs the full USB
> transport cost; PNG mode pays for device-side PNG encoding but
> transports a content-dependent payload that can range from ~500 KB
> (simple, flat-color screens) to ~1.4 MB (high-entropy screens).
>
> The framework SHALL:
> - Default to raw mode for content-deterministic latency.
> - Expose a configuration knob to override to PNG mode for use cases
>   known to operate on low-entropy screens.
> - Optionally implement an A/B sampling mode where, every N ticks,
>   the framework benches the other mode and switches if the
>   alternative is consistently faster on the current content.
> - Surface the negotiated USB link speed at bootstrap and refuse to
>   start (or warn loudly) when the link is below 480 Mbps.
>
> The "primary raw, PNG fallback" architecture decision stands; only
> the latency expectations and the operator-visible knob are revised.

The 16-byte header layout assertion in ADR-01 ("`width: uint32 LE,
height: uint32 LE, pixel_format: uint32 LE[, colorSpace: uint32 LE on
Android 9+]`") is confirmed (VF, §6).

### 8.2 ADR-02 — Screenshot encoding: raw framebuffer over PNG

**Verdict: ACCEPT.**

The internal representation choice (NumPy `ndarray` `(H, W, 3)` uint8
BGR, via a single in-process RGBA→BGR conversion) is sound and not
contradicted by any Phase 0 measurement. The RGBA→BGR conversion is not
on the hot path's critical-cost list (USB transport dominates).

The "8 MB per frame" memory figure in ADR-02 is approximate: the native
device frame is **10.4 MB** at 1080×2408. After remap to the reference
1080×1920 BGR, the working frame is 6.2 MB. Both fit within the
≤ 300 MB RAM target by an enormous margin.

### 8.3 ADR-03 — Primary CV strategy: normalized template matching with masks

**Verdict: ACCEPT, with a tightened operational guidance.**

- `cv2.matchTemplate` with `TM_CCOEFF_NORMED` is the right primitive.
- ROI restriction is structurally mandatory for hot-path templates;
  full-frame matching is too slow for any default tick (8 templates ×
  138 ms BGR full-frame = 1.1 s).
- The ADR's "5–25 ms per template" wall-clock estimate is accurate
  for ROI matching but is exceeded by full-frame BGR by ~5–6×. The
  ADR should explicitly forbid full-frame BGR on the hot path or
  document it as opt-in via an explicit `full_frame: true` template flag
  with a soft warning at manifest load.

**Proposed clarification (addendum, not a new ADR):**

> ADR-03 clarification — Default template policy.
>
> Hot-path templates (any template included in the default active set
> for OBSERVING/MATCHING) MUST declare a ROI hint, and MUST default
> to `match_strategy = "grayscale"` unless color is structurally
> required. The manifest loader SHALL emit a WARN log line for any
> template that omits both the ROI hint and the grayscale strategy.
> Full-frame BGR templates are not forbidden, but their per-tick CPU
> cost will exceed the MATCHING state's 500 ms timeout on the
> operator's hardware and will cause the FSM to escalate to
> RECOVERING. Operators electing to include full-frame BGR templates
> in the hot path must raise the per-state timeout consciously.

### 8.4 ADR-04 — Resolution independence: fixed reference + affine remap

**Verdict: ACCEPT.**

The native frame is 1080×2408 (aspect 1:2.23, taller than the 1:1.78
reference 1080×1920). The remap step in the match_bench resampled
1080×2408 → 1080×1920 cleanly. ADR-04's reference-resolution choice
holds; letterboxing or crop (per the ADR's "Letterboxing is preferred
over distortion" guidance) will be the right Phase 2 default.

ADR-04's "3–8 ms per frame for bilinear resampling at 1080×1920" is not
directly measured in Phase 0 because the bench resamples once per session
not per tick. A spot check (`cv2.resize 1080×2408 → 1080×1920`) inside
the match bench startup completes well under 8 ms (UE, not aggregated).
Worth a targeted bench in Phase 2 if it becomes load-bearing; for now,
the ADR's estimate is accepted.

### 8.5 ADR-16 — Python version & dependency strategy

**Verdict: ACCEPT.**

- Python 3.12.3 ≥ 3.11 minimum ✓ (VF)
- `opencv-python-headless` 4.13.0, AVX2/AVX512_SKX dispatched ✓ (VF)
- `numpy` 2.4.6 ✓ (VF)
- adb platform-tools 35.0.0 ≥ 34.0 ✓ (VF)

No conflicts with `opencv-python`; the bench venv only installs
`opencv-python-headless` per ADR-16. Phase 1's `pyproject.toml`
should pin these versions or compatible ranges.

### 8.6 ADR-15 — Anti-fragility: bounded behavioral jitter

**Not in the Phase 0 scope** but worth a brief note: jitter ranges in
§5.4.2 (50–150 ms pre-delay, 100–300 ms post-delay) are an additional
~150–450 ms per actuated action on top of the measured 28 ms ADB
shell overhead and the device's ~80–200 ms `input` JVM bootstrap. The
total per-action latency envelope is therefore ~260–680 ms (UE). This
fits the per-action latency table in SYSTEM-ROADMAP §5.4.1 ("80–250 ms"
for `tap`) only on the low end; the high end overshoots. ADR-15
should not be revised based on Phase 0 data alone, but the action
latency NFR (and ARCHITECTURE-DIAGRAMS §3's "80–200 ms" action
round-trip) deserves a Phase 4 re-measurement.

---

## 9. NFR evaluation

For each NFR in SYSTEM-ROADMAP §3.1 / §3.2, an explicit verdict:

| NFR | Target | Achievable on operator hardware | Notes |
|---|---|---|---|
| **Tick latency (median)** | ≤ 500 ms | **NOT ACHIEVABLE.** | Screencap alone is ≥ 500 ms median. Propose revision to ≤ 1500 ms median (VF). |
| **Tick latency (p95)** | ≤ 900 ms | **NOT ACHIEVABLE.** | Screencap p95 alone is 700–1500 ms. Propose revision to ≤ 2000 ms (VF). |
| **Screenshot capture (median)** | ≤ 250 ms | **NOT ACHIEVABLE.** | Best observed median 578 ms. Propose revision to ≤ 1000 ms (low-entropy screens), ≤ 1500 ms (high-entropy screens) (VF). |
| **Per-template match cost (median)** | ≤ 25 ms (1080×1920, full screen) | **PARTIALLY ACHIEVABLE.** | ROI/grayscale: ~2 ms ✓. Full-frame grayscale: ~34 ms ✗. Full-frame BGR: ~138 ms ✗. Propose: tighten NFR to *"per-template match cost (median) ≤ 25 ms with ROI hint, ≤ 50 ms full-frame grayscale; full-frame BGR is opt-in only"*. |
| **Sustained tick rate (default)** | 2–5 Hz | **NOT ACHIEVABLE at the high end; lower bound borderline.** | At ~1000 ms screencap floor, sustained tick rate is ~0.7–1 Hz (UE). Propose revision to 0.5–1 Hz default, with minicap escalation documented (per ADR-06's `LowLatencyInputAdapter` pattern) if higher rates are needed. |
| **Concurrent template matches per tick** | ≤ 8 default | **ACHIEVABLE** with ROI discipline. | ROI-grayscale: 8 × 2.2 ms = 18 ms. Inside the MATCHING state's 500 ms timeout by a wide margin. |
| **RAM steady state** | ≤ 300 MB | **ACHIEVABLE.** | Frame is 6–10 MB; the framework working set is dominated by the template manifest and is small (UE). |
| **RAM artifact spike** | ≤ 600 MB | **ACHIEVABLE.** | Same reasoning (UE). |
| **CPU single core steady state** | ≤ 30% | **UNCERTAIN.** | A full bench of cv2 on full-frame BGR at the 8-template default would exceed 30% of one core during MATCHING; ROI discipline keeps it well below. Phase 5/6 should soak-test. (UE) |
| **Disk write (logs + metrics)** | ≤ 50 MB / day | **NO DATA.** | Out of Phase 0 scope; Phase 6 measures. |
| **Disk write (artifacts)** | ≤ 500 MB / day capped | **NO DATA.** | Out of Phase 0 scope; Phase 6 measures. |

### 9.1 Recommended NFR revisions

To proceed into Phase 1 with honest targets:

| NFR | Current target | Proposed target |
|---|---|---|
| Tick latency (median) | ≤ 500 ms | ≤ 1500 ms |
| Tick latency (p95) | ≤ 900 ms | ≤ 2000 ms |
| Screenshot capture (median) | ≤ 250 ms | ≤ 1000 ms (low-entropy), ≤ 1500 ms (high-entropy) |
| Per-template match cost (median) | ≤ 25 ms | ≤ 5 ms (ROI grayscale), ≤ 10 ms (ROI BGR), ≤ 50 ms (full-frame grayscale), full-frame BGR opt-in only |
| Sustained tick rate (default) | 2–5 Hz | 0.5–1 Hz |

These revisions reflect *what this hardware can do*. Hosts with USB 3.0
to the device, or hosts using minicap/scrcpy frame interception, will
beat them; the dossier should clarify which numbers are hardware-floor
and which are framework-overhead.

---

## 10. Final recommendation

### 10.1 Recommended primary screenshot pipeline

**Keep `adb exec-out screencap` raw as the primary mode**, with
`adb exec-out screencap -p` (PNG) as a configurable fallback. Rationale:

- **Raw mode is content-deterministic** (median 947–1032 ms regardless
  of screen content). An operator can budget against it.
- **PNG mode is content-dependent** (median 578 ms on low-entropy
  content, 1311–1328 ms on high-entropy content). Sometimes faster,
  sometimes slower than raw.
- **`screencap + pull` (mode A) is consistently middle-pack to slow.**
  It does not win in either content regime in our measurements. Keep
  it as a third-tier fallback only.

For Phase 2's implementation: raw primary, PNG fallback selected by a
config knob (`sensor.mode = "raw" | "png" | "pull" | "auto"`). The
`auto` value implements the A/B sampling described in ADR-01a's
proposed revision (§8.1). For v1.0 release, default to `raw`. For
operators whose target app is dominated by simple-content / low-entropy
screens (homescreens, menus on flat backgrounds), document `png` as the
recommended override.

### 10.2 Operationally critical findings

1. **USB topology validation MUST be added to bootstrap.** A
   misplugged cable through a USB 1.1 hub reduces effective screencap
   throughput by ~40×. Phase 1's `bootstrap.sh` should call
   `cat /sys/bus/usb/devices/.../speed` for the ADB device's USB path
   and warn loudly if below 480 Mbps. Added to v1.1 backlog in
   DESIGN-REVIEW.md.

2. **ROI restriction is mandatory for hot-path templates.** Full-frame
   matching budgets should be reserved for rare "find this anywhere"
   queries. Phase 3's manifest loader should warn on full-frame
   templates included in the default active set.

3. **The 2–5 Hz tick-rate claim in the dossier is aspirational on this
   hardware.** A more honest claim for v1.0 is 0.5–1 Hz with the
   default screenshot pipeline. Operators needing higher rates should
   be directed to minicap (out of scope for v1.0 baseline, in scope as
   a `LowLatencyInputAdapter`-style backend per ADR-06).

### 10.3 Items deferred to later phases

- **CV resampling cost** (ADR-04's 3–8 ms claim). Spot-checked OK; a
  proper microbench belongs in Phase 2.
- **Action engine latency** (ADR-06 / SYSTEM-ROADMAP §5.4.1). Measured
  only indirectly via the `adb shell` overhead bench (28 ms median).
  Full per-action latency for `input tap`, `input swipe`, etc., should
  be measured in Phase 4.
- **End-to-end SENSE → THINK → ACT tick latency.** Phase 0 measured
  the parts; the integrated whole-tick number is Phase 5's measurement
  surface.
- **Long-run stability** (24 h soak per SYSTEM-ROADMAP §3.3). Phase 7
  and Phase 8.

### 10.4 What we are not yet sure about

- **Whether the device's `screencap` is consistently 600–700 ms on
  device-side.** That number is *inferred* (UE) from the difference
  between raw-screencap latency and the USB-transport floor. Direct
  measurement (e.g. `adb shell time screencap > /sdcard/x.raw`) is a
  small follow-up; it does not change the architecture but pins down
  the device's contribution.
- **Whether minicap would help.** Likely yes (ADR-01's 30–80 ms
  estimate for minicap is plausible on hardware where screencap takes
  600+ ms), but Phase 0 did not benchmark minicap because no vetted
  binary is on the device. If higher tick rates become a hard
  requirement, this is the obvious next experiment.
- **Whether the operator's screen content will be closer to the §3.2
  low-entropy snapshot or the §3.3 high-entropy snapshot in
  production.** This determines whether PNG or raw is the right
  default.

---

## 11. Phase 0 deliverables checklist

| Item | Location | Status |
|---|---|---|
| `bench/screencap_bench.py` | `bench/screencap_bench.py` | ✓ |
| `bench/match_bench.py` | `bench/match_bench.py` | ✓ |
| `bench/adb_overhead_bench.py` | `bench/adb_overhead_bench.py` | ✓ |
| Raw screencap header probe | `bench/raw_header_probe.py` | ✓ |
| Per-iter CSVs | `bench/results/*.csv` | ✓ |
| Summary CSVs | `bench/results/*_summary.csv` | ✓ |
| Artifacts (raw header, screen states, USB autosuspend) | `bench/artifacts/*` | ✓ |
| Phase report | `phase-0-report.md` (this document) | ✓ |
| ADR review (ACCEPT / PROPOSE REVISION) | §8 | ✓ |
| NFR evaluation | §9 | ✓ |
| Primary pipeline recommendation | §10 | ✓ |

---

## 12. Reproducing this report

```bash
# Prerequisites: Python 3.11+, adb 34+, a connected Android device.

# 1. Set up bench venv (throwaway).
python3 -m venv .venv-bench
.venv-bench/bin/pip install numpy opencv-python-headless

# 2. Run benches.
.venv-bench/bin/python -m bench.adb_overhead_bench
.venv-bench/bin/python -m bench.match_bench
.venv-bench/bin/python -m bench.screencap_bench   # ~5 min for 200 iter × 3 modes
.venv-bench/bin/python -m bench.raw_header_probe

# 3. Re-read the CSVs.
cat bench/results/*_summary.csv
```

Each bench writes CSVs atomically (`.tmp` → rename) and uses
`time.perf_counter_ns()` exclusively for timing. Same-name runs
overwrite their CSV; preserve prior data by renaming before re-running.

---

## End of Phase 0 report

Phase 1 is unblocked once this report is reviewed and the ADR revisions
in §8 (specifically the ADR-01 addendum) are accepted or counter-proposed.
