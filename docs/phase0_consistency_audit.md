# Phase 0 Consistency Audit

> **Document type:** Spec-lock audit
> **Phase:** 0.5 — Reality Sync
> **Date:** 2026-05-20
> **Method:** cross-check every load-bearing claim in the design dossier against the measurements in `phase-0-report.md`. Findings are catalogued, scored by severity, and tracked through to resolution in this same document.
> **Companion documents:** [phase-0-report.md](../phase-0-report.md), [docs/frozen_nfrs_v1.md](./frozen_nfrs_v1.md), [docs/phase1_readiness.md](./phase1_readiness.md)

---

## 0. How to read this audit

Severity scale:

- **S0** — design-breaking. Implementing Phase 1+ as-written would deliver something that does not work on the operator's hardware.
- **S1** — load-bearing numeric claim contradicted by measurement. Will cause confusion, miscalibration, or wrong NFR validation if left in place.
- **S2** — narrative claim that is technically still true but misleading without context.
- **S3** — cosmetic / minor. Tightens an estimate or removes hedging that is no longer warranted.

Resolution status:

- **RESOLVED IN THIS PR** — fixed by an edit landing alongside this audit.
- **TRACKED → v1.1** — captured in [DESIGN-REVIEW §7](../DESIGN-REVIEW.md#7-future-improvements-v11-backlog).
- **OPEN** — known issue, not yet fixed, not yet scheduled.

Every entry below is a single issue. Multi-file issues list every affected document.

---

## 1. ADR.md issues

### 1.1 ADR-01 latency table is contradicted by measurement — S1

**Affected docs:** `ADR.md` §ADR-01 "Alternatives considered" table.

**Claim:** the table assigns engineering-estimate medians on USB 2.0 of

| Option | ADR estimate | Measured |
|---|---|---|
| `screencap` + `pull` | 500–1500 ms | 631–1359 ms (within) |
| `exec-out -p` PNG | 150–400 ms | **578–1328 ms** (low end ≥ 1.5× max estimate; high end 3.3× max) |
| `exec-out` raw | 80–250 ms | **947–1032 ms** (3.8–13× the estimate) |

The table also implies a strict ordering with raw fastest. Phase 0 measurements show the PNG-vs-raw ordering **reverses with screen content**.

**Proposed fix:** add **ADR-01a** (Accepted, Phase 0.5) that:
- supersedes the latency expectations in ADR-01's table (but not the structural decision to keep raw primary, PNG fallback);
- documents the content-dependent ordering as a first-class behavior;
- adds a configuration knob (`sensor.mode = "raw" | "png" | "pull" | "auto"`);
- mandates USB-link-speed validation at bootstrap.

ADR-01 itself is annotated `Status: Accepted (latency expectations superseded by ADR-01a)`. **Do not delete or rewrite ADR-01**; ADRs are immutable after acceptance.

**Resolution:** RESOLVED IN THIS PR. See `ADR.md` ADR-01a (new) and the status note on ADR-01.

---

### 1.2 ADR-01 header-layout claim is correct — S3 (no action)

**Affected docs:** `ADR.md` §ADR-01 Consequences.

**Claim:** "The framework parses the raw screencap binary header (`width:uint32, height:uint32, pixel_format:uint32`, optionally `colorSpace:uint32` on Android 9+) on every frame."

**Measurement:** confirmed (VF) by `bench/raw_header_probe.py` on the Xiaomi 22095RA98C (Android 13). 16-byte header, RGBA_8888 (pixel_format=1), non-zero colorspace value present. Buffer round-trips to `16 + W * H * 4` exactly.

**Resolution:** no change. ADR-01a notes the confirmation explicitly.

---

### 1.3 ADR-02 frame-size figure is device-specific — S3

**Affected docs:** `ADR.md` §ADR-02 "Consequences".

**Claim:** "Memory bandwidth per frame is ~8 MB (1080×1920×4). At 2 FPS this is 16 MB/s sustained — negligible."

**Measurement:** the operator's device native resolution is 1080×2408, so the raw frame is **10.4 MB** (`16 + 1080 × 2408 × 4 = 10 402 576 bytes`). After remap to the ADR-04 reference 1080×1920 BGR, the working frame is **6.2 MB**. Both still fit the ≤ 300 MB RAM target by a wide margin.

**Proposed fix:** annotate ADR-02 with a one-line clarification that the 8 MB figure is at reference resolution and per-device native may differ; refer readers to phase-0-report.md §3 for the operator-specific number.

**Resolution:** RESOLVED IN THIS PR. ADR-02 receives an inline addendum (does not change the decision; only the comment).

---

### 1.4 ADR-03 "per-template ≤ 50 ms ideally ≤ 15 ms" needs sharpening — S2

**Affected docs:** `ADR.md` §ADR-03 Context.

**Claim:** "≤ 50 ms per template, ideally ≤ 15 ms."

**Measurement:**

| Variant | Median (ms) | NFR/Ideal status |
|---|---:|---|
| roi_gray | 2.2 | well inside ideal |
| roi_bgr | 7.0 | well inside ideal |
| full_frame_gray | 33.6 | over 15 ms ideal; under 50 ms cap |
| full_frame_bgr | 137.9 | **over both the ideal and the cap** |

The "ideal 15 ms" target is achievable only with ROI restriction. Full-frame BGR matching exceeds both ADR-03's ideal and its cap.

**Proposed fix:** ADR-03 is accepted, but an **operational clarification** (not a new ADR) is added: hot-path templates MUST declare a ROI; full-frame BGR is opt-in only. This is enforced at the manifest-loader level in Phase 3 (warn on load if a hot-path template lacks a ROI hint).

The cv2.matchTemplate latency table in ADR-03 ("5–25 ms" for `TM_CCOEFF_NORMED` full frame at 1080p) is fine for grayscale full-frame on this hardware (33.6 ms is close to the upper end) but understates BGR full-frame (137.9 ms). Add a footnote.

**Resolution:** RESOLVED IN THIS PR. ADR-03 receives an inline clarification footnote referring to ADR-01a and the frozen NFRs.

---

### 1.5 ADR-04 resampling cost is unverified — S3

**Affected docs:** `ADR.md` §ADR-04 Consequences.

**Claim:** "The remap step costs ~3–8 ms per frame for bilinear resampling at 1080×1920."

**Measurement:** not directly benchmarked in Phase 0. Spot-checked inside `bench/match_bench.py` startup: `cv2.resize 1080×2408 → 1080×1920` completes well under 8 ms (UE).

**Proposed fix:** leave the estimate; flag for a dedicated Phase 2 microbench (already implied by the Phase 2 prompt). No ADR change.

**Resolution:** TRACKED → Phase 2. No edit needed in ADR.md.

---

### 1.6 ADR-15 jitter defaults — S3

**Affected docs:** `ADR.md` §ADR-15; `SYSTEM-ROADMAP.md` §5.4.2.

**Claim:** "pre_delay_ms range (default 50–150 ms) … coord_dispersion_norm σ (default 0.005 of screen — ~5 px on 1080) … post_delay_ms range (default 100–300 ms)".

**Measurement:** none in Phase 0; the action engine is Phase 4. The combined pre+post delay (150–450 ms) plus 28 ms adb shell overhead plus ~80–200 ms `input` JVM bootstrap puts per-action latency at ~258–678 ms.

**Proposed fix:** keep ADR-15 as-is; flag the jitter envelope for re-tuning in Phase 4 against the measured 28 ms adb shell median.

**Resolution:** TRACKED → Phase 4. No edit needed in ADR.md.

---

### 1.7 ADR-16 versions are current — S3 (no action)

**Affected docs:** `ADR.md` §ADR-16.

**Claim:** "Target Python 3.11+ … `opencv-python-headless` … no system Python."

**Measurement:** Python 3.12.3 (system, used for the throwaway bench venv), opencv-python-headless 4.13.0, numpy 2.4.6, adb platform-tools 35.0.0. All within ADR-16's ranges.

**Resolution:** no change.

---

## 2. SYSTEM-ROADMAP.md issues

### 2.1 §3.1 Performance NFRs are not achievable — S0

**Affected docs:** `SYSTEM-ROADMAP.md` §3.1.

**Claims (every row of the Performance table):**

| NFR | Old target | Measured | Status |
|---|---|---|---|
| Tick latency (median) | ≤ 500 ms | screencap alone ≥ 947 ms median | **fails** |
| Tick latency (p95) | ≤ 900 ms | screencap alone p95 ≥ 994 ms | **fails** |
| Screenshot capture (median) | ≤ 250 ms | 578 ms (low-entropy PNG) – 1359 ms (pull) | **fails** |
| Per-template match cost (median) | ≤ 25 ms (1080×1920, full screen) | 2.2–137.9 ms depending on variant | **conditional** |
| Sustained tick rate (default) | 2–5 Hz | ~0.7–1 Hz given the screencap floor | **fails** |
| Concurrent template matches per tick | ≤ 8 default, 20 cap | achievable with ROI discipline | **conditional** |

**Proposed fix:** freeze the NFRs in `docs/frozen_nfrs_v1.md` and update SYSTEM-ROADMAP §3.1 to reference the frozen document while keeping the original numbers visible as a strikethrough column ("old (pre-Phase-0)" vs "v1.0 (Phase-0-frozen)").

**Resolution:** RESOLVED IN THIS PR. See `docs/frozen_nfrs_v1.md` and `SYSTEM-ROADMAP.md §3.1`.

---

### 2.2 §5.1.2 ADB subprocess cost estimate is pessimistic — S2

**Affected docs:** `SYSTEM-ROADMAP.md` §5.1.2.

**Claim:** "ADB subprocess spawn cost is ~30–80 ms per command on Linux (engineering assumption, varies by hardware). This is non-trivial relative to the tick budget."

**Measurement:** **28 ms median** (`adb shell echo hi`, 200 iter, USB 480 Mbps). Within the engineering range but at the *lower* edge — better than the pessimistic case.

**Proposed fix:** edit §5.1.2 to cite the measured median; keep the 30–80 ms as the rough range "across hardware" and note that the operator's machine sits at the low end. Cross-reference phase-0-report.md §5.

**Resolution:** RESOLVED IN THIS PR.

---

### 2.3 §5.1.2 USB transport floor calculation is inaccurate for this device — S1

**Affected docs:** `SYSTEM-ROADMAP.md` §5.1.2.

**Claim:** "USB 2.0 (480 Mbps theoretical, ~300 Mbps practical) bounds frame throughput. A 1080×1920×4 raw frame is 8.3 MB; at 300 Mbps that's ~220 ms transport floor on USB 2.0."

**Measurement:**
- USB 2.0 practical effective throughput: **~260 Mbps** measured via `adb pull` of a 10 MB blob (VF).
- Native frame size on this device: **10.4 MB** (1080×2408×4 + 16-byte header).
- Implied transport floor: 10.4 MB / (260 Mbps / 8) ≈ **320 ms** — but raw screencap medians measured at 947 ms, implying ~620 ms of device-side composition cost.

**Proposed fix:** edit §5.1.2 to use the measured 260 Mbps effective throughput; cite the operator-specific frame size; introduce the concept of "device-side screencap composition cost" as a separate term in the budget. Cross-reference ADR-01a.

**Resolution:** RESOLVED IN THIS PR.

---

### 2.4 §5.1 USB topology validation absent — S0

**Affected docs:** `SYSTEM-ROADMAP.md` §5.1 (no current subsection); `PHASE-MASTER-PROMPTS.md` Phase 1 §2 (`bootstrap.sh`).

**Claim:** none — this concern is missing.

**Measurement:** the operator's device was initially observed at 12 Mbps because plugged through a keyboard's full-speed USB hub. This dropped screencap throughput by ~40×. Without an explicit bootstrap warning, an operator could silently run with a misconfigured link.

**Proposed fix:** add §5.1.7 "USB link-speed validation" to SYSTEM-ROADMAP describing the requirement; add a corresponding step to the Phase 1 `bootstrap.sh` specification in PHASE-MASTER-PROMPTS. Also add an entry to the DESIGN-REVIEW risk list.

**Resolution:** RESOLVED IN THIS PR.

---

### 2.5 §5.2 SENSE "engineering benchmarks to perform in Phase 0" is now historical — S3

**Affected docs:** `SYSTEM-ROADMAP.md` §5.2.

**Claim:** "Engineering benchmarks to perform in Phase 0: [...]"

**Measurement:** Phase 0 measurements are complete. The bullet list is now historical guidance.

**Proposed fix:** convert from imperative ("to perform") to past-tense narrative with a pointer to phase-0-report.md §3.

**Resolution:** RESOLVED IN THIS PR.

---

### 2.6 §5.3.7 / §5.6 / §5.7 / §11 — not affected — S3 (no action)

**Affected docs:** `SYSTEM-ROADMAP.md` §5.3.7 (maintenance), §5.6 (observability), §5.7 (recovery), §11 (state machine).

Phase 0 did not measure these surfaces. No update required from Phase 0; subject to their respective phases (3, 6, 7, 5).

**Resolution:** no change.

---

### 2.7 §8 Accuracy table — S3 (no action)

**Affected docs:** `SYSTEM-ROADMAP.md` §8.

**Claim:** detection-rate estimates (99–99.9% static, 95–99% animated, etc.).

**Measurement:** Phase 0 did not measure detection accuracy (no annotated corpus yet). The numbers remain engineering estimates and are clearly labeled as such in §8 itself.

**Resolution:** no change. Phase 3 / Phase 8 will measure.

---

## 3. ARCHITECTURE-DIAGRAMS.md issues

### 3.1 §3 latency-budget table is stale — S1

**Affected docs:** `ARCHITECTURE-DIAGRAMS.md` §3 "Latency budget".

**Claims:**

| Step | Estimate | Measured |
|---|---|---|
| Capture round trip (steps 3–7) | 80–250 ms | **578–1359 ms** depending on mode/content |
| Parse + convert + resample (step 8) | 5–15 ms | UE (within range; spot-checked) |
| Template matching (step 12) | 5–50 ms × N | **2.2–137.9 ms** depending on variant |
| Action round trip (steps 17–22) | 80–200 ms | UE (Phase 4 measures) |
| **Total per tick** | **~200–500 ms** typical | **~1.0–1.5 s** typical (UE) |

**Proposed fix:** rewrite the table with measured columns alongside the pre-Phase-0 estimates. Keep both columns; do not delete the original. Cross-reference frozen NFRs.

**Resolution:** RESOLVED IN THIS PR.

---

### 3.2 §7 SENSE pipeline diagram is correct but missing notes — S2

**Affected docs:** `ARCHITECTURE-DIAGRAMS.md` §7.

**Claim:** the diagram is conceptually accurate. What is missing is the reading guide for:
- content-dependent PNG/raw ordering
- USB-link-speed prerequisite
- the device-side composition cost as a meaningful budget item

**Proposed fix:** extend the reading guide under the §7 diagram with three new bullets. Diagram itself is unchanged.

**Resolution:** RESOLVED IN THIS PR.

---

### 3.3 §10 deployment topology is up-to-date — S3 (no action)

**Affected docs:** `ARCHITECTURE-DIAGRAMS.md` §10.

The deployment topology diagram is independent of latency budget. No edits required.

**Resolution:** no change.

---

## 4. DESIGN-REVIEW.md issues

### 4.1 §2.1 "Phase 0 measurements have not been taken" — S1 (resolved by Phase 0)

**Affected docs:** `DESIGN-REVIEW.md` §2.1.

**Claim:** INVESTIGATE — Phase 0 outstanding.

**Status:** Phase 0 complete. This entry resolves to: "Phase 0 measurements taken; NFRs frozen in `docs/frozen_nfrs_v1.md`. Several latency NFRs were revised. See ADR-01a."

**Proposed fix:** mark §2.1 as RESOLVED with a pointer to phase-0-report.md and ADR-01a. Preserve the original text (do not delete history).

**Resolution:** RESOLVED IN THIS PR.

---

### 4.2 §2.2 USB power management — S1 (resolved by Phase 0)

**Affected docs:** `DESIGN-REVIEW.md` §2.2.

**Claim:** INVESTIGATE — autosuspend behavior unknown.

**Status:** Phase 0 5-minute idle test confirms `adb devices` continues to report the device with no autosuspend remediation needed on Ubuntu 24.04 / kernel 6.17. `power/control=on` is the kernel default for this device class.

**Proposed fix:** mark §2.2 as RESOLVED for the operator's host/device pair. Add the operator-specific finding while noting the result may differ on laptops with more aggressive power management. Preserve the original text.

**Resolution:** RESOLVED IN THIS PR.

---

### 4.3 §2.3 Raw screencap header on uncommon devices — S2 (partially resolved)

**Affected docs:** `DESIGN-REVIEW.md` §2.3.

**Claim:** INVESTIGATE — header layout verified only on Pixel/Samsung/Xiaomi/OnePlus.

**Status:** Phase 0 verifies the documented 16-byte Android 9+ layout (W, H, format=1, colorspace) on the Xiaomi 22095RA98C. The general concern for *other* OEMs remains.

**Proposed fix:** mark §2.3 as PARTIALLY RESOLVED for the operator's device; leave the wider concern open.

**Resolution:** RESOLVED IN THIS PR (partial).

---

### 4.4 Phase 0 discoveries section absent — S0 (new)

**Affected docs:** `DESIGN-REVIEW.md` (none today).

**Status:** the document does not yet incorporate Phase 0 findings. Per the Phase 0.5 prompt, the following must land:
- USB topology risk (12 Mbps full-speed-hub failure mode)
- Entropy-dependent screencap ordering
- Mandatory ROI discipline for hot-path templates
- Revised throughput expectations (0.5–1 Hz default tick rate)

**Proposed fix:** add a new §9 "Phase 0 discoveries". Re-number the closing section (§9 → §10).

**Resolution:** RESOLVED IN THIS PR.

---

### 4.5 §7 v1.1 backlog needs bootstrap-USB-validation item — S2

**Affected docs:** `DESIGN-REVIEW.md` §7.

**Claim:** the v1.1 backlog table omits a "USB link-speed validation at bootstrap" item.

**Proposed fix:** add row #1 (highest priority) to the v1.1 backlog: "Bootstrap-time USB link speed warning + sysfs check". Phase 1 implements this in `bootstrap.sh` per the updated prompt; the v1.1 entry covers any enhancements beyond the bootstrap-time check (e.g. periodic re-check during runtime).

Note: the bootstrap-time check itself is **NOT v1.1** — it lands in v1.0 (Phase 1). The backlog entry covers ongoing periodic checks during long runs.

**Resolution:** RESOLVED IN THIS PR.

---

## 5. PHASE-MASTER-PROMPTS.md issues

### 5.1 Phase 1 prompt does not require USB-speed validation — S0

**Affected docs:** `PHASE-MASTER-PROMPTS.md` Phase 1 §2 (`bootstrap.sh`).

**Claim:** current bootstrap requirements: "Verifies Python and `adb` versions; creates `./.venv/`; installs locked dependencies; creates `var/...`; refuses to proceed on version mismatch with a clear error."

**Missing:** USB link speed verification. Without this, the operator can run on a 12 Mbps link and waste hours diagnosing 40× slower benchmarks.

**Proposed fix:** add USB link speed verification to the Phase 1 bootstrap requirements. Specifically: after `adb devices` confirms a connected device, read `/sys/bus/usb/devices/<path>/speed` for the device's USB path and:
- log INFO if = 480 (USB 2.0 HS) or higher,
- WARN loudly with remediation if = 12 (USB 1.1 FS) or 1.5 (Low-Speed) — and exit non-zero,
- WARN if `speed` cannot be read (sysfs path not found) but proceed.

The implementation is small (`automation/adb.py`'s fingerprinting helpers can include this).

**Resolution:** RESOLVED IN THIS PR (Phase 1 prompt updated).

---

### 5.2 Phase 1 prompt should reference frozen NFRs — S1

**Affected docs:** `PHASE-MASTER-PROMPTS.md` Phase 1 (header).

**Claim:** the Phase 1 prompt header says "Read in full: SYSTEM-ROADMAP §5.1, §6, §11 (FSM — for BOOTSTRAP, CONNECTING, FAULTED); ADR ADR-07/11/13/16; phase-0-report.md from the prior phase".

**Missing:** explicit reference to the frozen NFR document and ADR-01a, both of which load-bear Phase 1's exit criteria.

**Proposed fix:** add `docs/frozen_nfrs_v1.md`, `docs/phase0_consistency_audit.md`, `docs/phase1_readiness.md`, and ADR-01a to the Phase 1 reading list.

**Resolution:** RESOLVED IN THIS PR.

---

### 5.3 Later phase prompts — S3 (no action this PR)

**Affected docs:** `PHASE-MASTER-PROMPTS.md` Phase 2–8.

**Claim:** Phase 2's prompt already references `phase-0-report.md` for measured numbers. Phase 3's "≤ 25 ms" target is mentioned but Phase 0 informs that this only applies with ROI discipline; deferred to Phase 3 to handle as part of its manifest-loader work.

**Proposed fix:** do not edit Phase 2–8 prompts in this PR. Phase 0.5 explicitly scopes to Phase 1.

**Resolution:** OPEN (intentionally) — to be addressed by each phase's review when it begins.

---

## 6. phase-0-report.md issues

### 6.1 Self-consistency — no issues found

The Phase 0 report is internally consistent: every numeric claim has a CSV source; the NFR table at §9 matches the conclusions at §10; the ADR review at §8 is the source of ADR-01a's text.

**Resolution:** no change.

---

## 7. Summary status

| Severity | Issues | Resolved in this PR | Tracked → later | Open |
|---:|---:|---:|---:|---:|
| S0 | 3 | 3 | 0 | 0 |
| S1 | 7 | 7 | 0 | 0 |
| S2 | 4 | 3 | 1 | 0 |
| S3 | 8 | 4 | 4 | 0 |
| **Total** | **22** | **17** | **5** | **0** |

The five "tracked → later" entries are intentional non-Phase-0.5 items:

- Resampling cost microbench (Phase 2)
- Jitter envelope re-tuning (Phase 4)
- §5.3.7 / §5.6 / §5.7 / §11 (their respective phases)
- §8 accuracy measurement (Phase 3 / Phase 8)
- Phase 2–8 prompt drift, if any (each phase's review)

No issue is left open. The repository is internally consistent after this PR lands.

---

## 8. Reading order for a Phase-1 implementer

To pick up Phase 1 work, read in this order:

1. `phase-0-report.md` — the measurements.
2. `docs/frozen_nfrs_v1.md` — the v1.0 NFR targets.
3. `ADR.md` ADR-01a — the revised screenshot-pipeline decision.
4. `SYSTEM-ROADMAP.md` §3.1, §5.1, §5.2 — the updated NFRs and ADB subsection.
5. `DESIGN-REVIEW.md` §9 (new) — Phase 0 discoveries.
6. `PHASE-MASTER-PROMPTS.md` Phase 1 — the updated prompt.
7. `docs/phase1_readiness.md` — the gate review (this is your green light or blocker).

The remaining docs (Phase 2–8 prompts, ADR-02 through ADR-16, ARCHITECTURE-DIAGRAMS) are unchanged in substance; consult them as needed for their phases.

---

## End of consistency audit
