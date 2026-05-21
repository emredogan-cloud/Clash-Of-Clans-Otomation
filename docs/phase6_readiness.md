# Phase 6 Readiness Gate

> **Document type:** Phase 5.5 final gate review
> **Date:** 2026-05-21
> **Posture:** Adversarial. The goal is to find reasons *not* to proceed.
> **Companion documents:** [phase5-report.md](../phase5-report.md), [docs/phase55_consistency_patch.md](./phase55_consistency_patch.md), [docs/frozen_nfrs_v1.md](./frozen_nfrs_v1.md), [PHASE-MASTER-PROMPTS.md](../PHASE-MASTER-PROMPTS.md), [ADR.md](../ADR.md)

---

## Verdict

**Phase-6 Ready? YES**, with two minor open items that are
documented but not blocking. See §3.

This document is the gating contract between Phase 5.5 and Phase 6.
A Phase 6 implementer should not begin work unless this document
remains in its `YES` state.

---

## 1. Gate checklist

| # | Item | Status | Evidence |
|--:|---|---|---|
| 1 | NFRs synchronized with Phase 5 measurements | ✅ DONE | `docs/frozen_nfrs_v1.md` §1.1 amended (tier-split tick latency); §1.2 OLD/Phase-0/Phase-5.5 history; §1.4 amendment history block; §1.3 implementer guidance updated |
| 2 | ADRs synchronized | ✅ DONE | `ADR.md` ADR-08a added (validation-cost consequence); ADR-08 status-noted, unchanged otherwise |
| 3 | Roadmap synchronized | ✅ DONE | `SYSTEM-ROADMAP.md` §1 executive summary clarifies tier-split; §3.1 table extended with Phase-5.5 column; §5.5 has validation-cost note; §11.1 has VALIDATING-timeout footnote |
| 4 | Architecture diagrams synchronized | ✅ DONE | `ARCHITECTURE-DIAGRAMS.md` §3 latency-budget table extended with validated-tick + retry rows |
| 5 | DESIGN-REVIEW reflects Phase 5 discoveries | ✅ DONE | `DESIGN-REVIEW.md` §10 added (5 new discovery items, MITIGATE/ACCEPT/DEFER positioned); §7 backlog rows #20–23 added |
| 6 | Consistency audit complete | ✅ DONE | `docs/phase55_consistency_patch.md`; 10 of 12 issues resolved in this PR; 2 deferred (cosmetic; Phase 6 / Phase 7) |
| 7 | Phase 6 prompt patched | ✅ DONE | `PHASE-MASTER-PROMPTS.md` Phase 6 — Phase-5.5 amendment block added; reading list extended; bucket-layout rationale spelled out |
| 8 | No document still claims `tick_latency ≤ 1500 ms` as a *blanket* NFR | ✅ DONE | every relevant doc either shows the tier-split table or annotates with a status note pointing at it |
| 9 | Phase 5 test suite green for Phase 6 to instrument | ✅ DONE | 333/333 tests pass; 93% package coverage; `phase5-report.md` §6 |
| 10 | `Orchestrator._transition` chokepoint exists for Phase 6 to wrap | ✅ DONE | `automation/orchestrator.py` — every transition goes through one method |
| 11 | `TickResult` carries the latency surfaces Phase 6 needs | ✅ DONE | `tick_latency_ms`, `capture_latency_ms`, `match_latency_ms`, `action_latency_ms`; `to_debug_dict` is JSON-safe |
| 12 | Per-state label exists for `tick_duration_seconds_bucket{state}` | ✅ DONE | `TickResult.state_before` / `state_after`; states are `IDLE`/`SEARCHING`/`ACTING`/`VALIDATING`/`FAILED` per `automation/state.py` |
| 13 | Validation rate observable | ✅ DONE | `metadata.json` artifact carries `retries_used: int` per tick; Phase 6's histogram can derive validation-cycle counts from it |
| 14 | No runtime code modified in Phase 5.5 | ✅ DONE | `automation/`, `tests/`, `scripts/`, `bench/` untouched; only `docs/`, `ADR.md`, `SYSTEM-ROADMAP.md`, `DESIGN-REVIEW.md`, `ARCHITECTURE-DIAGRAMS.md`, `PHASE-MASTER-PROMPTS.md`, and `phase5-report.md` (added in Phase 5) touched |

**Fourteen of fourteen gate items are green.**

---

## 2. What was actually frozen / amended

The numbers a Phase 6 implementer (and later phases) MUST plan against:

| NFR | v1.0 frozen (Phase 5.5) |
|---|---|
| Tick latency, search-only (median) | ≤ 1500 ms |
| Tick latency, search-only (p95) | ≤ 1800 ms |
| Tick latency, validated, no retry (median) | ≤ 2200 ms |
| Tick latency, validated, no retry (p95) | ≤ 2500 ms |
| Tick latency, validated + retry (median) | ≤ 3000 ms |
| Tick latency, validated + retry (p95) | ≤ 3300 ms |
| Screenshot capture (median, `sensor.mode = "raw"`) | ≤ 1000 ms (unchanged) |
| Per-template match cost (median, ROI grayscale) | ≤ 5 ms (unchanged) |
| Per-template match cost (median, full-frame grayscale) | ≤ 50 ms (unchanged) |
| Sustained tick rate, search-only | 0.5–1 Hz |
| Sustained tick rate, validated | 0.3–0.5 Hz |
| Per-action latency (tap median) | proposed ≤ 100 ms (not yet frozen; awaits ADR amendment) |

Source: [`docs/frozen_nfrs_v1.md`](./frozen_nfrs_v1.md) §1.1 (amended
2026-05-21).

---

## 3. Caveats (not blocking)

### 3.1 Per-action latency NFRs proposed but not yet frozen

`phase4-report.md` §7.1 proposes per-action latency NFRs (≤ 100 ms
tap median, ≤ 120 ms swipe framework overhead, ≤ 100 ms long_press
framework overhead). These are **proposed**, not frozen. Phase 6's
metrics will produce the long-tail evidence needed to justify
freezing them via an ADR amendment. Until then they are advisory.

**Why not blocking:** Phase 6 instrumentation does not depend on
these being frozen. It only needs the per-action histogram buckets
(already specified in `PHASE-MASTER-PROMPTS.md` Phase 6 §3,
amended by Phase 5.5).

### 3.2 Validated-tick-no-retry NFR is arithmetic, not directly measured

The new `≤ 2200 ms validated, no retry` tier in
`docs/frozen_nfrs_v1.md` §1.1 was not directly observed in Phase 5
— Demo 3 used the retry path. The number is the arithmetic floor
(940 + 50 + 60 + 940 + 50 = 2040 ms) with a 50% headroom (2200 ms).

**Why not blocking:** Phase 7 soak will produce direct measurements
of this tier. The arithmetic floor is structurally correct (capture
+ match are measured per Phase 0/3; action is measured per Phase 4);
the only uncertainty is the small handler-overhead between them,
which Phase 7 quantifies.

### 3.3 Open items in the consistency audit

Two items in `docs/phase55_consistency_patch.md` are tagged OPEN:

- **§2.11** — ARCHITECTURE-DIAGRAMS §4 state diagram "Phase coverage"
  caption (deferred to Phase 6 — caption added when the FSM
  expansion lands).
- **§2.12** — PHASE-MASTER-PROMPTS Phase 5 vs delivered scope
  (deferred to Phase 7 — reconciled when the recovery cascade
  lands).

**Why not blocking:** both are cosmetic and do not affect Phase 6
implementer's task surface.

---

## 4. Unresolved unknowns

Things Phase 5.5 cannot resolve and that Phase 6+ measurements will:

1. **Real-world retry rate.** Phase 5 Demo 3 hit the retry path on
   one capture — caused by a mid-animation transition frame. We do
   not yet know how often this fires on operator scripts. Phase 6
   metrics + Phase 7 soak will quantify.
2. **Distribution of validated vs search-only ticks.** A script
   that taps on every tick will pay the validated-tick cost on
   every tick; a script that mostly waits will pay only the
   search-only cost. The blended throughput depends on the script.
   Phase 6 metrics + Phase 7 soak will reveal the actual blend.
3. **Whether one of the v1.1 cheaper-validation strategies is
   worth implementing in v1.0.** If Phase 7 soak shows validated
   ticks dominate operator throughput and the tick rate falls
   below 0.3 Hz, deferred-validation (DESIGN-REVIEW §7 row #20)
   may need to be pulled into v1.0. Not a Phase 6 concern.
4. **Phase 6 logging overhead.** The Phase 6 prompt specifies
   "logging overhead < 1% of per-tick time at default verbosity".
   At a validated tick of ~2.5 s, 1% is 25 ms — generous. Phase 6's
   own profiling will confirm.

---

## 5. What this gate does *not* certify

For honesty:

- **NOT certified:** that Phase 6 will hit its own NFRs without
  effort. The buckets are sized right but the implementer still
  has to wire the histogram observations, handle metric atomic
  writes, build the replay CLI.
- **NOT certified:** that the validated-tick NFR is achievable on
  long-run operator scripts. Phase 7's 24-hour soak is the
  authority on that, not this gate.
- **NOT certified:** the bucket layouts will fit *every future*
  device. The current buckets are calibrated to the Phase 5
  operator hardware. A different host or device may need a
  different layout — that is a Phase 8 conversation.
- **NOT certified:** Phase 6 will land before Phase 7. Ordering
  is the operator's call.

---

## 6. Reading order for a Phase 6 implementer

In order:

1. `PHASE-MASTER-PROMPTS.md` Phase 6 — the master prompt with the
   Phase-5.5 amendment block at the top.
2. `phase5-report.md` — to understand what the orchestrator
   actually produces and what surfaces (TickResult, metadata.json,
   `_transition` chokepoint) are available to instrument.
3. `docs/frozen_nfrs_v1.md` §1.1 — the tier-split NFRs.
4. `ADR.md` ADR-12 (observability), ADR-13 (configuration),
   **ADR-08a (validation-cost consequence)**.
5. `ARCHITECTURE-DIAGRAMS.md` §1 (subsystem overview), §3 (latency
   budget), §4 (state diagram).
6. `SYSTEM-ROADMAP.md` §5.6 (observability), §3.1 (NFRs).
7. This file (gate contract).

---

## End of Phase 6 readiness gate
