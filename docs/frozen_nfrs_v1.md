# Frozen NFRs — v1.0

> **Document type:** v1.0 non-functional requirement freeze
> **Phase:** 0.5 — Reality Sync
> **Date:** 2026-05-20
> **Authority:** this document, together with the Phase 0 measurements at `phase-0-report.md`, is the source of truth for v1.0 NFRs. Where this document conflicts with [SYSTEM-ROADMAP.md §3](../SYSTEM-ROADMAP.md#3-non-functional-requirements), this document wins until a future ADR amends.
> **Companion documents:** [phase-0-report.md](../phase-0-report.md), [docs/phase0_consistency_audit.md](./phase0_consistency_audit.md), [ADR.md ADR-01a](../ADR.md#adr-01a--screenshot-pipeline-phase-0-reality-content-dependent-ordering-usb-link-speed-prerequisite)

---

## 0. Purpose

The pre-Phase-0 NFRs in `SYSTEM-ROADMAP.md` §3.1 were engineering
estimates. Phase 0 measured them on the operator's hardware. Some
held; some did not. This document records the **frozen v1.0 targets**:
the numbers an implementer can build against, the numbers a soak test
can assert against, and the numbers the operator should expect.

Every row in the tables below is tagged with a binding category:

- **HARDWARE-BOUND** — the limit is set by the operator's host, device,
  cable, or USB link. The framework cannot move it without changing
  hardware (USB 3.x, faster host, etc.) or pipeline (minicap, scrcpy).
- **FRAMEWORK-BOUND** — the limit is set by the framework's own code.
  Implementation choices in Phases 1–7 determine compliance.
- **UNCERTAIN** — not enough measurement yet to bind. Phase N
  (specified per-row) will measure and either freeze or revise.

Every binding number is one of:

- **VF (verified fact)** — measured in Phase 0 on the operator's
  hardware.
- **EA (engineering assumption)** — structural reasoning; not directly
  measured.
- **UE (uncertain estimate)** — magnitude only; precise value not measured.

---

## 1. Performance

### 1.1 Frozen targets

| NFR | v1.0 target | Category | Source |
|---|---|---|---|
| Tick latency (median) | ≤ 1500 ms | HARDWARE-BOUND | screencap floor; phase-0-report §3 (VF) |
| Tick latency (p95) | ≤ 2000 ms | HARDWARE-BOUND | screencap p95 + match + action budget (VF + UE) |
| Screenshot capture (median) | ≤ 1000 ms (`sensor.mode = "raw"`); ≤ 1500 ms across modes | HARDWARE-BOUND | phase-0-report §3 (VF) |
| Screenshot capture (p95) | ≤ 1100 ms (`sensor.mode = "raw"`) | HARDWARE-BOUND | phase-0-report §3.1 (VF) |
| Per-template match cost (median, ROI grayscale) | ≤ 5 ms | FRAMEWORK-BOUND | phase-0-report §4 (VF, measured 2.2 ms) |
| Per-template match cost (median, ROI BGR) | ≤ 10 ms | FRAMEWORK-BOUND | phase-0-report §4 (VF, measured 7.0 ms) |
| Per-template match cost (median, full-frame grayscale) | ≤ 50 ms | FRAMEWORK-BOUND | phase-0-report §4 (VF, measured 33.6 ms) |
| Per-template match cost (median, full-frame BGR) | **opt-in only** — no NFR target. Triggers manifest-load WARN. | FRAMEWORK-BOUND | phase-0-report §4 (VF, measured 137.9 ms) |
| Active template count per tick (default) | ≤ 8, ROI-required | FRAMEWORK-BOUND | composition of §3.1 row + cv2 budget (VF + UE) |
| Active template count per tick (cap, with explicit opt-in) | ≤ 20 | FRAMEWORK-BOUND | unchanged from pre-Phase-0 (UE) |
| Sustained tick rate (default) | 0.5–1 Hz | HARDWARE-BOUND | screencap floor (VF) |
| Sustained tick rate (achievable with minicap, future) | 3–10 Hz | UNCERTAIN (Phase 8+) | ADR-01a (UE, deferred) |

### 1.2 OLD vs NEW comparison

| NFR | OLD (pre-Phase-0, SYSTEM-ROADMAP §3.1) | NEW (v1.0 frozen) | Δ | Why |
|---|---|---|---|---|
| Tick latency (median) | ≤ 500 ms | ≤ 1500 ms | **3×** worse | screencap floor measured at ~1 s; pre-Phase-0 estimate assumed 80–250 ms raw screencap which did not hold |
| Tick latency (p95) | ≤ 900 ms | ≤ 2000 ms | 2.2× worse | same |
| Screenshot capture (median) | ≤ 250 ms | ≤ 1000 ms (raw) / ≤ 1500 ms (across modes) | 4–6× worse | USB transport floor + device-side composition cost; ADR-01a |
| Per-template match cost (median) | ≤ 25 ms (1080×1920, full screen) | tier-split: ≤ 5/10/50 ms by variant | better for ROI, conditional for full-frame | ADR-03 clarification; ROI discipline mandatory |
| Sustained tick rate (default) | 2–5 Hz | 0.5–1 Hz | 4–10× worse | bounded by screencap floor |
| Concurrent template matches per tick (default) | ≤ 8 | ≤ 8, ROI-required | unchanged numerically; tightened on ROI | full-frame BGR ×8 = 1.1 s, exceeds MATCHING-state timeout |

### 1.3 Implementer guidance

The new targets are honest about hardware-bound limits. Implementers
should not "engineer their way to" the old 500 ms tick latency on this
hardware — it is structurally not achievable with `adb exec-out
screencap`. Acceptable directions to chase a faster tick:

- minicap (deferred per ADR-01; Phase 8 may revisit).
- scrcpy frame intercept (deferred; same).
- USB 3.x device on a USB 3.x host (operator-side, not framework-side).
- Pipelining (capture for tick N+1 while THINK runs on tick N) — only
  if the state machine can tolerate the extra latency window. v1.1
  consideration.

What is **always** in scope to chase, even on this hardware:

- Tightening per-template match cost via better ROIs and grayscale.
- Reducing per-tick subprocess overhead by collapsing multiple
  `adb shell input` invocations.
- Reducing post-action wait times where the action's effect is fast.

---

## 2. Resource usage

### 2.1 Frozen targets

| NFR | v1.0 target | Category | Source |
|---|---|---|---|
| RAM (steady state) | ≤ 300 MB | FRAMEWORK-BOUND | unchanged; raw frame is 6–10 MB, template manifest is small (EA) |
| RAM (artifact spike) | ≤ 600 MB | FRAMEWORK-BOUND | unchanged (EA) |
| CPU (single core, steady state) | ≤ 30% with ROI discipline; ≤ 60% if full-frame templates present | FRAMEWORK-BOUND | composition of §1.1 + tick rate (UE) |
| Disk write (logs + metrics) | ≤ 50 MB / day | FRAMEWORK-BOUND | unchanged (UE, Phase 6 measures) |
| Disk write (artifacts under load) | ≤ 500 MB / day, rotation-capped | FRAMEWORK-BOUND | unchanged (UE) |

### 2.2 OLD vs NEW comparison

| NFR | OLD | NEW | Δ | Why |
|---|---|---|---|---|
| RAM (steady state) | ≤ 300 MB | ≤ 300 MB | unchanged | Phase 0 did not invalidate |
| RAM (artifact spike) | ≤ 600 MB | ≤ 600 MB | unchanged | Phase 0 did not invalidate |
| CPU (single core, steady state) | ≤ 30% | ≤ 30% (with ROI discipline) | tightened on ROI | full-frame BGR ×8 templates would exceed 30% (UE) |
| Disk write (logs) | ≤ 50 MB / day | ≤ 50 MB / day | unchanged | Phase 6 measures |
| Disk write (artifacts) | ≤ 500 MB / day | ≤ 500 MB / day | unchanged | Phase 6 measures |

---

## 3. Reliability

No frozen revisions. The reliability NFRs in `SYSTEM-ROADMAP.md` §3.3
remain engineering estimates pending Phase 7 (24-hour soak) and
Phase 8 (7-day soak) measurements.

| NFR | v1.0 target | Category | Source |
|---|---|---|---|
| Mean time between unrecovered faults | ≥ 24 h | UNCERTAIN (Phase 8) | pre-Phase-0 estimate (UE) |
| Mean time to recovery (soft fault) | ≤ 10 s | UNCERTAIN (Phase 7) | pre-Phase-0 estimate (EA) |
| Mean time to recovery (hard fault) | ≤ 60 s | UNCERTAIN (Phase 7) | pre-Phase-0 estimate (EA) |
| Watchdog restart bound | ≤ 5 restarts / hour | FRAMEWORK-BOUND | unchanged (EA) |
| Heartbeat staleness threshold | 30 s | FRAMEWORK-BOUND | unchanged (EA) |

---

## 4. Maintainability, Observability, Portability, Extensibility

No frozen revisions. These NFRs in `SYSTEM-ROADMAP.md` §3.4–3.7 are
not bound by Phase 0 measurements and remain as written.

Two clarifications:

- **§3.6 ADB minimum version "platform-tools 34.0+"** — Phase 0 ran on
  platform-tools 35.0.0 (VF). Within range. No change.
- **§3.6 Python versions "3.11, 3.12"** — Phase 0 bench used 3.12.3
  (VF). No change.

---

## 5. New NFR — USB link speed (Phase 0.5 addition)

This NFR did not exist pre-Phase-0. Phase 0 surfaced the 12 Mbps
hub-failure mode (a USB hub between host and device can silently
downgrade the link by ~40×) and Phase 0.5 freezes a corresponding NFR.

| NFR | v1.0 target | Category | Source |
|---|---|---|---|
| USB link speed at bootstrap | ≥ 480 Mbps | HARDWARE-BOUND | phase-0-report §2.3 (VF) |
| Bootstrap behavior on link < 480 Mbps | log WARN + exit non-zero | FRAMEWORK-BOUND | ADR-01a §Decision (5) |
| Bootstrap behavior when sysfs `speed` unreadable | log WARN + proceed | FRAMEWORK-BOUND | ADR-01a §Decision (5) |

---

## 6. Verification plan

| Frozen target | How verified | When |
|---|---|---|
| Screencap latency (median, p95) | `bench/screencap_bench.py` (already implemented) | Phase 0 ✓ |
| Per-template match cost | `bench/match_bench.py` + Phase 3's manifest microbench | Phase 0 ✓ + Phase 3 |
| Tick latency | end-to-end soak in Phase 5 + Phase 7 | Phase 5, Phase 7 |
| Sustained tick rate | Phase 7 24-hour soak | Phase 7 |
| RAM / CPU steady-state | Phase 6 soak (1 hour) + Phase 7 (24 hour) | Phase 6, Phase 7 |
| Reliability targets | Phase 7 (controlled faults) + Phase 8 (real operation) | Phase 7, Phase 8 |
| USB link-speed bootstrap | Phase 1 bootstrap, unit test against fixture sysfs paths | Phase 1 |

Each frozen number above is a **regression boundary**: a future change
that violates it without an ADR amendment is a regression.

---

## 7. What is *not* frozen

Explicit non-freezes, to prevent re-debates:

- **Detection accuracy NFRs** (§8 of SYSTEM-ROADMAP). Phase 0 did not
  measure detection rates. These remain engineering estimates until
  Phase 3 / Phase 8.
- **Action engine latency NFRs** (§3.1 row "Tick latency" already
  covers the composite, but individual `tap` / `swipe` latencies in
  §5.4.1 are not yet measured). Phase 4 measures.
- **Multi-device scaling**. Out of scope for v1.0 by §2.2 of
  SYSTEM-ROADMAP. No NFRs frozen here.
- **Wireless-ADB latency**. Out of scope per §2.2. No NFRs frozen.

---

## 8. How to amend this document

NFRs in this document are immutable in v1.0. Amendments require:

1. A new ADR (or an extension of an existing ADR) recording the
   reason for the change.
2. New measurements (re-run the relevant bench harness from `bench/`
   under the amendment's conditions).
3. A PR titled "NFR amendment — v1.0.N" that updates this document,
   the corresponding SYSTEM-ROADMAP §3 row, and the consistency
   audit.

Do **not** silently lower a target. Lowering a target without
measurement is how regressions hide.

---

## End of frozen NFRs
