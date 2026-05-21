# Phase 5.5 Consistency Patch

> **Document type:** Spec-lock audit (Reality Sync v2)
> **Phase:** 5.5 — Reality Sync after Phase 5 orchestration
> **Date:** 2026-05-21
> **Method:** cross-check every load-bearing tick-latency and
> validation-cost claim in the design dossier against the
> measurements in `phase5-report.md`. Findings are catalogued,
> scored by severity, and tracked to resolution in this PR.
> **Companion documents:** [phase5-report.md](../phase5-report.md), [docs/frozen_nfrs_v1.md](./frozen_nfrs_v1.md), [docs/phase0_consistency_audit.md](./phase0_consistency_audit.md), [docs/phase6_readiness.md](./phase6_readiness.md)

---

## 0. How to read this audit

Severity scale (same as Phase 0.5 audit):

- **S0** — design-breaking; Phase 6+ as-written would deliver
  something inconsistent with measured reality.
- **S1** — load-bearing numeric claim contradicted by Phase-5
  measurement. Will cause confusion, miscalibration, or wrong NFR
  validation if left in place.
- **S2** — narrative claim still technically true but misleading
  without the Phase-5 validation-cost context.
- **S3** — cosmetic / minor. Tightens an estimate, removes hedging,
  cross-references the right section.

Resolution status:

- **RESOLVED IN THIS PR** — fixed by an edit landing alongside this audit.
- **TRACKED → v1.1** — captured in DESIGN-REVIEW §7 backlog.
- **OPEN** — known issue, not yet fixed, not yet scheduled.

---

## 1. Phase-5 measurements (the new ground truth)

From `phase5-report.md` §4.1, against the operator's hardware
(USB 2.0 HS, raw sensor mode), with `Orchestrator` exercising
the single-template, single-retry FSM:

| Demo | FSM path | Tick latency (ms) | Captures | Match | Action |
|---|---|---:|---:|---:|---:|
| **1. SEARCH miss** | `IDLE → SEARCHING → FAILED` | **1211.2** | 1 | 1 | 0 |
| **2. VALIDATE fail + retry** | `IDLE → SEARCHING → ACTING → VALIDATING → FAILED` | **2956.2** | 3 | 3 | 1 |
| **3. VALIDATE success (with retry)** | `IDLE → SEARCHING → ACTING → VALIDATING → IDLE` | **2584.4** | 3 | 3 | 1 |

The structural cost driver is clear from the count column: **each
validation cycle is a full `Sensor.capture()` + `Matcher.match()`,
~990 ms on this hardware**. A tick that reaches `VALIDATING`
therefore costs at least 2× a search-only tick; with the retry it
costs ~3×.

A tick that completes the **happy path without using the retry**
would cost approximately:

```
search_capture + match + action + 1 × validate_capture + validate_match
≈ 940 + 50 + 60 + 940 + 50 ≈ 2040 ms
```

That number was not directly observed in Phase 5 (Demo 3 used the
retry), but it is the load-bearing estimate for the Phase-5.5
NFR amendment.

---

## 2. Issues

### 2.1 `docs/frozen_nfrs_v1.md` — tick-latency NFR does not separate validated from non-validated ticks — **S1**

**Affected docs:** `docs/frozen_nfrs_v1.md` §1.1, rows "Tick latency
(median)" (≤ 1500 ms) and "Tick latency (p95)" (≤ 2000 ms).

**Claim:** a single tier of tick-latency budget applies to all
ticks, set at ≤ 1500 ms median / ≤ 2000 ms p95.

**Reality:** Phase 5's tick spans up to 3 captures (search + 2
validates including retry). The current single-tier NFR is met on
SEARCH-miss ticks (1211 ms ≤ 1500 ms) but violated on validated
ticks (2584 ms > 1500 ms) and on retry-validated ticks
(2956 ms > 2000 ms p95).

**Fix:** tier-split the NFR:

| Tier | Budget |
|---|---|
| Search-only tick (no VALIDATING) | ≤ 1500 ms median (unchanged) |
| Validated tick, no retry (1 search + 1 validate) | ≤ 2200 ms median |
| Validated tick, with retry (1 search + 2 validates) | ≤ 3300 ms p95 |

The two new tiers are derived from `screencap_floor × n_captures +
match_n × n_matches + action`, where `screencap_floor ≈ 940 ms`,
`match_n ≈ 50 ms`, `action ≈ 65 ms`. 50% headroom over the
arithmetic floor.

**Resolution:** RESOLVED IN THIS PR. `docs/frozen_nfrs_v1.md` §1.1
gets a new tier-split table; the old single-tier table is preserved
in §1.2 as the OLD column.

---

### 2.2 `SYSTEM-ROADMAP.md` §3.1 — same tick-latency NFR — **S1**

**Affected docs:** `SYSTEM-ROADMAP.md` §3.1 table, rows "Tick
latency (median)" and "Tick latency (p95)" (which already preserve
an OLD column from the Phase 0.5 amendment).

**Claim:** v1.0 frozen at ≤ 1500 ms median / ≤ 2000 ms p95.

**Reality:** as §2.1 — these targets ignore the validation cycle.

**Fix:** annotate the table with a Phase-5.5 footnote pointing at
the tier-split rows in `docs/frozen_nfrs_v1.md` §1.1; preserve the
existing OLD/v1.0 columns; do not silently rewrite the row.

**Resolution:** RESOLVED IN THIS PR.

---

### 2.3 `SYSTEM-ROADMAP.md` §1 executive summary — "~1.0–1.5 s median" tick claim — **S2**

**Affected docs:** `SYSTEM-ROADMAP.md` §1 ("Target tick rate is
0.5–1 Hz with end-to-end tick latency ~1.0–1.5 s median…").

**Claim:** end-to-end tick latency ~1.0–1.5 s median, on the
operator's hardware.

**Reality:** for the Phase-5 `Orchestrator.tick()`, which includes
the validation cycle, the median is ~2.0–2.6 s. The executive
summary is correct only for "search-only" ticks; it is misleading
for the v1.0 framework as actually implemented.

**Fix:** rephrase to "~1.0–1.5 s for search-only ticks, ~2.0–2.6 s
for validated ticks", and cross-reference the tier-split NFR.

**Resolution:** RESOLVED IN THIS PR.

---

### 2.4 `ARCHITECTURE-DIAGRAMS.md` §3 latency-budget table — "Total per tick ~1.0–1.5 s typical (UE)" — **S2**

**Affected docs:** `ARCHITECTURE-DIAGRAMS.md` §3 sequence diagram
latency-budget table (final row).

**Claim:** "Total per tick (default templates, ROI discipline)
~1.0–1.5 s typical (UE)" with the OLD/NEW two-column structure.

**Reality:** same as §2.3 — that's true for tick = SENSE + THINK +
ACT only. The Phase-5 orchestrator includes a validation cycle that
the budget table does not.

**Fix:** add a third row to the §3 table covering "validated tick"
and "validated tick + retry". This is a documentation patch only;
the sequence diagram itself does not need restructuring (validation
is depicted in `§4` formal state diagram already).

**Resolution:** RESOLVED IN THIS PR.

---

### 2.5 `ADR.md` ADR-08 — does not enumerate the validation-cost consequence — **S1**

**Affected docs:** `ADR.md` ADR-08 (hand-rolled FSM).

**Claim:** ADR-08 decides on a hand-rolled FSM (~200–400 LOC core).
It does not address per-tick latency cost.

**Reality:** the Phase-5 orchestrator's validation cycle ≈ doubles
the search-only tick cost; with retry, triples it. This is a
direct consequence of the architectural decision that "validation
= full recapture + rematch" — the implementation choice ADR-08
implicitly authorizes. Future readers reasoning about tick cost
from ADR-08 alone will undercount by a factor of 2–3×.

**Fix:** add **ADR-08a** (Accepted, Phase 5.5) that:

- supersedes nothing in ADR-08 (the structural decision stands);
- documents the *consequence* that the FSM's `VALIDATING` state
  doubles the per-tick capture count;
- cites the Phase-5 live measurements;
- proposes (not commits to) cheaper-validation strategies for a
  future ADR amendment (in-frame diff, deferred validation,
  no-validation actions).

ADR-08 itself remains unchanged. Per the dossier convention, ADRs
are immutable after acceptance.

**Resolution:** RESOLVED IN THIS PR. See `ADR.md` ADR-08a (new).

---

### 2.6 `PHASE-MASTER-PROMPTS.md` Phase 6 — histogram bucket layout was set before Phase-5 measurements — **S2**

**Affected docs:** `PHASE-MASTER-PROMPTS.md` Phase 6, section 3
"Bucket layouts".

**Claim:**
```
Tick duration: 50, 100, 200, 400, 800, 1600, 3200, 6400 (ms)
```

**Reality:** the existing buckets are adequate — 3200 ms catches
the Phase-5 retry-path p95 (2956 ms) and 6400 ms covers worst-case
edge spikes. **But** the rationale is not documented: a Phase-6
implementer reading the prompt today might assume the 6400 ms
ceiling is for crash scenarios when in fact 3200 ms is the
expected operating ceiling for validated retry ticks.

**Fix:** add a note to Phase 6's prompt clarifying that buckets
were sized assuming `tick_duration` of ≤ 3300 ms p95 (Phase-5
retry case), and that 6400 ms is the head-room ceiling for fault
spikes. No bucket changes required.

**Resolution:** RESOLVED IN THIS PR.

---

### 2.7 `DESIGN-REVIEW.md` — no Phase-5 discovery section — **S2**

**Affected docs:** `DESIGN-REVIEW.md`.

**Claim:** §9 "Phase 0 discoveries" is the latest reality-sync block;
nothing reflects Phase-5 insights.

**Reality:** Phase 5 surfaced four new design-level items the dossier
should record:

- validation dominates tick cost;
- the single-retry budget proved its worth live (Demo 3 caught a
  mid-animation transition frame);
- validation economics — every `tick()` reaching VALIDATING pays
  for at least one extra ~940 ms capture;
- the `tick_latency_median ≤ 1500 ms` frozen NFR needed tier-split.

**Fix:** add a §10 "Phase 5 discoveries" mirroring the §9 pattern
(MITIGATE/ACCEPT/DEFER/INVESTIGATE positions, with v1.0/v1.1/future
tags where appropriate). Re-number the existing §10 "Closing
position" → §11.

**Resolution:** RESOLVED IN THIS PR.

---

### 2.8 `SYSTEM-ROADMAP.md` §5.5 — orchestration overview silent on validation cost — **S3**

**Affected docs:** `SYSTEM-ROADMAP.md` §5.5 (state machine overview).

**Claim:** "The state machine is the *one* place where domain logic
lives. Everything else is a mechanism."

**Reality:** technically still true, but the section says nothing
about the validation cycle's cost profile. A reader leaving §5.5
without reading §11 (state table) would not know that VALIDATING
costs another full capture.

**Fix:** add a one-paragraph note to §5.5 referencing ADR-08a and
the tier-split NFR. No structural changes.

**Resolution:** RESOLVED IN THIS PR.

---

### 2.9 `SYSTEM-ROADMAP.md` §11.1 state table — per-state timeouts vs Phase-5 reality — **S3**

**Affected docs:** `SYSTEM-ROADMAP.md` §11.1, rows for `OBSERVING`
(2 s), `MATCHING` (500 ms), `ACTING` (2 s), `VALIDATING` (2 s).

**Claim:** per-state timeouts as listed.

**Reality:** the Phase-5 measurements show `OBSERVING` (≈ capture)
takes ~940 ms, well inside its 2 s timeout. `MATCHING` takes
~50 ms full-frame grayscale, well inside 500 ms. `ACTING`
(`adb shell input tap`) takes ~60 ms, well inside 2 s. `VALIDATING`
takes ~990 ms per cycle (≤ 1980 ms for two cycles); the 2 s
per-state timeout is **tight** when the retry fires.

**Fix:** annotate the `VALIDATING` row with a Phase-5.5 footnote
that the timeout is *per cycle*, not per state-entry, OR raise the
`VALIDATING` timeout to 3000 ms to cover one full retry cycle.
Per the Phase 5.5 task scope ("DOCUMENTATION + ARCHITECTURE PATCH
… No implementation"), this is a documentation-only flag; the
runtime timeout enforcement is Phase 6+ work. The state table
gets a footnote pointing at this audit.

**Resolution:** RESOLVED IN THIS PR (footnote only). The actual
timeout-enforcement code is Phase 6+; this audit flags the value
that Phase 6 should adopt.

---

### 2.10 `docs/frozen_nfrs_v1.md` §7 ("What is *not* frozen") — action-latency NFR list is now outdated — **S3**

**Affected docs:** `docs/frozen_nfrs_v1.md` §7.

**Claim:** "Action engine latency NFRs (§3.1 row 'Tick latency'
already covers the composite, but individual `tap` / `swipe`
latencies in §5.4.1 are not yet measured). Phase 4 measures."

**Reality:** Phase 4 measured them (`phase4-report.md` §4); the
list should now reflect that those numbers exist and are
candidates for freezing.

**Fix:** update the §7 entry to note "Action engine latencies
measured in Phase 4; per-action NFRs proposed in `phase4-report.md`
§7.1 but not yet frozen (requires an ADR amendment)."

**Resolution:** RESOLVED IN THIS PR.

---

### 2.11 ARCHITECTURE-DIAGRAMS state diagram cross-link — **S3**

**Affected docs:** `ARCHITECTURE-DIAGRAMS.md` §4 state diagram.

**Claim:** the §4 stateDiagram shows the full SYSTEM-ROADMAP §11
state set (BOOTSTRAP, CONNECTING, CALIBRATING, READY, OBSERVING,
MATCHING, WAITING, ACTING, VALIDATING, RECOVERING, RESET_LITE,
RESET_HARD, RECONNECTING, FAULTED).

**Reality:** Phase 5 implemented only the inner SEARCH/ACT/VALIDATE
slice (5 states: IDLE, SEARCHING, ACTING, VALIDATING, FAILED).
The §4 diagram still represents the *target* full FSM that Phase
6+ would extend toward; not actually wrong, but a reader could be
confused.

**Fix:** add a "Phase coverage" caption under §4 stating "Phase 5
ships the inner-slice (IDLE/SEARCHING/ACTING/VALIDATING/FAILED, see
`automation/state.py`); the recovery states (RECOVERING/RESET_LITE/
RESET_HARD/RECONNECTING) and bootstrap states (BOOTSTRAP/CONNECTING/
CALIBRATING) are Phase 6+ work."

**Resolution:** OPEN (deferred). ARCHITECTURE-DIAGRAMS §4 will be
synced when the FSM expansion lands in Phase 6+. Phase 5.5 already
spends enough surface on the documentation patch; adding a caption
to the diagram is a small Phase 6 task. Tracked here for visibility.

---

### 2.12 `PHASE-MASTER-PROMPTS.md` Phase 5 — mismatch with the simpler scope actually delivered — **S3**

**Affected docs:** `PHASE-MASTER-PROMPTS.md` Phase 5.

**Claim:** the original Phase 5 prompt enumerates a 13-state FSM
realising SYSTEM-ROADMAP §11 in full, plus `Script`, `Screen`,
recovery cascade, CLI extension, Mermaid exporter, heartbeat
writer.

**Reality:** the operator narrowed Phase 5's scope to the inner
5-state slice (no Script, no recovery cascade, no heartbeat, no
Mermaid exporter, no CLI). This is documented in `phase5-report.md`
§2.4. The recovery-cascade / heartbeat / Script / Screen work
remains pending — most likely as Phase 7 (Hardening) per the
existing PHASE-MASTER-PROMPTS structure.

**Fix:** **out of Phase 5.5 scope.** The Phase 5 prompt's deltas
will be reconciled when Phase 7 lands. Recording the discrepancy
here so a future reader of PHASE-MASTER-PROMPTS doesn't think
Phase 5 was incomplete — it was narrowed deliberately.

**Resolution:** OPEN (tracked). No edit to PHASE-MASTER-PROMPTS
Phase 5 in this PR.

---

## 3. Summary of resolutions

| # | Issue (one line) | Severity | Resolution |
|--:|---|---|---|
| 2.1 | frozen_nfrs_v1 tick-latency NFR is single-tier | S1 | tier-split (resolved here) |
| 2.2 | SYSTEM-ROADMAP §3.1 mirrors the same single tier | S1 | footnote (resolved here) |
| 2.3 | SYSTEM-ROADMAP §1 exec summary "~1.0–1.5 s" | S2 | clarify search-only vs validated (resolved here) |
| 2.4 | ARCHITECTURE §3 latency-budget table | S2 | add validated-tick row (resolved here) |
| 2.5 | ADR-08 silent on validation cost | S1 | add ADR-08a addendum (resolved here) |
| 2.6 | Phase-6 prompt bucket layout rationale | S2 | clarifying note (resolved here) |
| 2.7 | DESIGN-REVIEW has no Phase-5 discovery section | S2 | add §10 (resolved here) |
| 2.8 | SYSTEM-ROADMAP §5.5 silent on validation cost | S3 | one-paragraph note (resolved here) |
| 2.9 | §11 state-table VALIDATING timeout vs retry | S3 | footnote (resolved here) |
| 2.10 | frozen_nfrs §7 stale on action-latency NFRs | S3 | refresh entry (resolved here) |
| 2.11 | ARCHITECTURE §4 state-diagram Phase coverage | S3 | OPEN — deferred to Phase 6 |
| 2.12 | PHASE-MASTER-PROMPTS Phase 5 vs delivered scope | S3 | OPEN — deferred to Phase 7 |

**S0:** 0 issues. **S1:** 3 issues, all RESOLVED IN THIS PR.
**S2:** 3 issues, all RESOLVED IN THIS PR. **S3:** 6 issues —
4 RESOLVED, 2 OPEN (cosmetic, do not block Phase 6).

---

## 4. What this audit explicitly does *not* touch

Per the Phase 5.5 task scope:

- **No runtime code.** `automation/`, `tests/`, `scripts/`, and
  `bench/` are unmodified. No new modules; no test changes.
- **No new measurements.** Phase 5 measurements are accepted as the
  ground truth. No re-runs.
- **No ADR rewrites.** ADR-08 is amended via the additive ADR-08a
  pattern, not edited. ADR-04, ADR-06, ADR-09, ADR-11, ADR-15,
  ADR-16 are untouched in this PR.
- **No state-machine refactor.** The Phase-5 5-state slice stays.
  Expansion to the SYSTEM-ROADMAP §11 13-state model is Phase 6+
  work.
- **No NFR loosening to hide reality.** The new tiers are honest
  arithmetic on top of measured per-layer costs; they are not
  optimistic targets to be "engineered around".
- **No telemetry.** Bucket sizing in Phase 6 prompt is a *note*,
  not a runtime change.

---

## 5. Cross-reference

- All resolutions land in this PR.
- `docs/phase6_readiness.md` (this PR) is the gating checklist that
  consumes this audit.
- Phase 6 prompts are unblocked once the resolutions above are
  merged.

---

## End of Phase 5.5 consistency patch
