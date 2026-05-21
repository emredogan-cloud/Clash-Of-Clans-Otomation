# Frozen NFRs — v1.0

> **Document type:** v1.0 non-functional requirement freeze
> **Phase:** 0.5 — Reality Sync; amended in Phase 5.5 (tick-latency tier split, 2026-05-21)
> **Date:** 2026-05-20 (Phase 0.5) · 2026-05-21 (Phase 5.5 amendment)
> **Authority:** this document, together with the Phase 0 measurements at `phase-0-report.md` and the Phase 5 measurements at `phase5-report.md`, is the source of truth for v1.0 NFRs. Where this document conflicts with [SYSTEM-ROADMAP.md §3](../SYSTEM-ROADMAP.md#3-non-functional-requirements), this document wins until a future ADR amends.
> **Companion documents:** [phase-0-report.md](../phase-0-report.md), [phase5-report.md](../phase5-report.md), [docs/phase0_consistency_audit.md](./phase0_consistency_audit.md), [docs/phase55_consistency_patch.md](./phase55_consistency_patch.md), [ADR.md ADR-01a](../ADR.md#adr-01a--screenshot-pipeline-phase-0-reality-content-dependent-ordering-usb-link-speed-prerequisite), [ADR.md ADR-08a](../ADR.md#adr-08a--validation-cost-consequence-of-the-fsm-design-phase-55)

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

> **Amended 2026-05-21 (Phase 5.5):** the single-tier tick-latency
> row was tier-split into three. The original single-tier value
> (≤ 1500 ms median / ≤ 2000 ms p95) was set by Phase 0 against an
> implicit *tick = SENSE + THINK + ACT* model. The Phase 5
> orchestrator's tick *also includes a validation cycle* — a full
> recapture + rematch — which is structurally another ~990 ms on
> this hardware. The new tiers separate ticks by FSM path. See
> [ADR-08a](../ADR.md#adr-08a--validation-cost-consequence-of-the-fsm-design-phase-55)
> for the architectural rationale, [phase5-report.md §4](../phase5-report.md)
> for the source measurements, and §1.2 / §1.4 below for the
> OLD/Phase-5/NEW history.

| NFR | v1.0 target | Category | Source |
|---|---|---|---|
| **Tick latency, search-only (median)** — FSM path `IDLE → SEARCHING → FAILED` (no HIT, no action, no validate) | ≤ 1500 ms | HARDWARE-BOUND | phase-0-report §3, phase5-report §4 (VF; Demo 1 measured 1211 ms) |
| **Tick latency, search-only (p95)** | ≤ 1800 ms | HARDWARE-BOUND | phase-0-report §3 (VF + UE) |
| **Tick latency, validated, no retry (median)** — FSM path `IDLE → SEARCHING → ACTING → VALIDATING → IDLE` with first validate succeeding | ≤ 2200 ms | HARDWARE-BOUND | `2 × screencap + 2 × match + action` arithmetic on Phase 0/3/4 medians (UE; not directly observed live — Phase 5's Demo 3 used the retry path) |
| **Tick latency, validated, no retry (p95)** | ≤ 2500 ms | HARDWARE-BOUND | UE |
| **Tick latency, validated with retry (median)** — FSM path includes one validate-retry cycle | ≤ 3000 ms | HARDWARE-BOUND | phase5-report §4 (VF; Demo 3 measured 2584 ms, Demo 2 measured 2956 ms) |
| **Tick latency, validated with retry (p95)** | ≤ 3300 ms | HARDWARE-BOUND | phase5-report §4 (VF) |
| Screenshot capture (median) | ≤ 1000 ms (`sensor.mode = "raw"`); ≤ 1500 ms across modes | HARDWARE-BOUND | phase-0-report §3 (VF) |
| Screenshot capture (p95) | ≤ 1100 ms (`sensor.mode = "raw"`) | HARDWARE-BOUND | phase-0-report §3.1 (VF) |
| Per-template match cost (median, ROI grayscale) | ≤ 5 ms | FRAMEWORK-BOUND | phase-0-report §4 (VF, measured 2.2 ms) |
| Per-template match cost (median, ROI BGR) | ≤ 10 ms | FRAMEWORK-BOUND | phase-0-report §4 (VF, measured 7.0 ms) |
| Per-template match cost (median, full-frame grayscale) | ≤ 50 ms | FRAMEWORK-BOUND | phase-0-report §4 (VF, measured 33.6 ms; Phase 5 live 43–50 ms) |
| Per-template match cost (median, full-frame BGR) | **opt-in only** — no NFR target. Triggers manifest-load WARN. | FRAMEWORK-BOUND | phase-0-report §4 (VF, measured 137.9 ms) |
| Active template count per tick (default) | ≤ 8, ROI-required | FRAMEWORK-BOUND | composition of §3.1 row + cv2 budget (VF + UE) |
| Active template count per tick (cap, with explicit opt-in) | ≤ 20 | FRAMEWORK-BOUND | unchanged from pre-Phase-0 (UE) |
| Sustained tick rate (search-only, default) | 0.5–1 Hz | HARDWARE-BOUND | screencap floor (VF) |
| Sustained tick rate (validated, default) | 0.3–0.5 Hz | HARDWARE-BOUND | composition of validated tick-latency rows (VF + UE) |
| Sustained tick rate (achievable with minicap, future) | 3–10 Hz | UNCERTAIN (Phase 8+) | ADR-01a (UE, deferred) |

### 1.2 OLD vs Phase-0 vs NEW (Phase-5.5) comparison

The history of every load-bearing performance NFR. OLD is the
pre-Phase-0 engineering estimate (in `SYSTEM-ROADMAP.md §3.1` as
the "OLD" column). Phase-0 is the value frozen at the end of
Phase 0.5. NEW is the value frozen after Phase 5.5.

| NFR | OLD (pre-Phase-0) | Phase-0 frozen (2026-05-20) | **NEW (Phase-5.5 frozen, 2026-05-21)** | Why the latest change |
|---|---|---|---|---|
| Tick latency (median, generic "tick") | ≤ 500 ms | ≤ 1500 ms | **tier-split:** ≤ 1500 ms search-only / ≤ 2200 ms validated / ≤ 3000 ms validated+retry | Phase-0 estimate assumed `tick = SENSE+THINK+ACT`. Phase-5's orchestrator's tick *also* includes a full validation cycle (recapture + rematch), structurally adding ~990 ms; the retry adds another ~990 ms. See ADR-08a. |
| Tick latency (p95, generic "tick") | ≤ 900 ms | ≤ 2000 ms | **tier-split:** ≤ 1800 ms search-only / ≤ 2500 ms validated / ≤ 3300 ms validated+retry | same |
| Screenshot capture (median) | ≤ 250 ms | ≤ 1000 ms (raw) / ≤ 1500 ms (across modes) | unchanged | USB transport floor + device-side composition cost; ADR-01a |
| Per-template match cost (median) | ≤ 25 ms (1080×1920, full screen) | tier-split: ≤ 5/10/50 ms by variant | unchanged | ADR-03 clarification; ROI discipline mandatory |
| Sustained tick rate (default) | 2–5 Hz | 0.5–1 Hz (single tier) | **tier-split:** 0.5–1 Hz search-only / 0.3–0.5 Hz validated | composition of the new validated-tick latency tier; each validation cycle ~990 ms doubles tick cost vs search-only |
| Concurrent template matches per tick (default) | ≤ 8 | ≤ 8, ROI-required | unchanged | full-frame BGR ×8 = 1.1 s, exceeds MATCHING-state timeout |

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
- **(Phase 5.5 addition)** Reducing the validation cycle's cost.
  Cheaper validation is the single largest available win after the
  screencap floor itself. Candidate strategies (none implemented in
  v1.0; tracked for v1.1):
  - In-frame diff. The post-action observation tick's natural
    capture already contains the validation evidence. Move
    validation off the critical path of the action's own tick.
  - Region-only re-capture. ADB does not expose region capture, but
    a partial decode of the raw payload could short-circuit the
    re-match (out of v1.0 scope; would conflict with ADR-02).
  - No-validation action classes. A future `Action.requires_validation:
    bool` annotation lets a `key`/`text` action skip the validation
    cycle when the action's effect is structurally unobservable in
    the screen template alone.

### 1.4 Amendment history

| When | What | Why |
|---|---|---|
| 2026-05-20 (Phase 0.5) | Tick latency frozen at ≤ 1500 ms median / ≤ 2000 ms p95 (single tier); screenshot, match-cost, tick-rate NFRs frozen | Phase 0 measurements invalidated pre-Phase-0 estimates; original NFRs assumed 80–250 ms screencap which did not hold on USB 2.0 |
| 2026-05-21 (Phase 5.5) | Tick latency tier-split into search-only / validated / validated+retry; sustained-tick-rate tier-split; implementer guidance §1.3 updated with validation-cost reduction strategies | Phase 5's orchestrator's tick includes a full validation cycle (recapture + rematch); single-tier NFR is violated on every validated tick |

Per §8 ("How to amend"), every change to this document must be
backed by a measurement. Phase 5.5 references `phase5-report.md`
§4 (the live 3-demo measurement set) as the source of truth. No
NFR has been *loosened to hide* an implementation problem; the new
tiers reflect the structural cost of the validation cycle, which
is what the FSM (per ADR-08 + ADR-08a) requires.

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
| Tick latency, search-only | end-to-end soak; Phase 5 single-tick live (Demo 1) confirms ≤ 1500 ms; Phase 7 soak confirms p95 | Phase 5 ✓ + Phase 7 |
| Tick latency, validated (no retry) | Phase 5 single-tick live did not exercise this path directly (Demo 3 used the retry); arithmetic floor confirmed; Phase 7 soak measures p50/p95 | Phase 7 |
| Tick latency, validated + retry | Phase 5 live confirms p50 ≈ 2.6 s, p95 ≈ 3.0 s (Demo 2/3); Phase 7 soak confirms at scale | Phase 5 ✓ + Phase 7 |
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
- **Per-action latency NFRs** (individual `tap` / `swipe` /
  `long_press`). **Measured in Phase 4** (`phase4-report.md` §4):
  tap 58.8 ms median; swipe 369.9 ms median at 300 ms duration;
  long_press 662.0 ms median at 600 ms hold. **Proposed-but-not-yet-frozen**
  values in `phase4-report.md` §7.1 (≤ 100 ms tap median, ≤ 150 ms
  tap p95, ≤ 120 ms swipe framework overhead, ≤ 100 ms long_press
  framework overhead). Freezing requires an ADR amendment per §8.
- **Cheaper-validation strategies.** §1.3 lists three candidate
  strategies (in-frame diff, region-only re-capture, no-validation
  action classes). None are implemented in v1.0; tracked as v1.1
  backlog (DESIGN-REVIEW §7). Whichever strategy wins will move
  the validated-tick NFR tier closer to the search-only tier.
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
