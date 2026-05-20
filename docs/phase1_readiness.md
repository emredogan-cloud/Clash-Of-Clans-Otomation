# Phase 1 Readiness Gate

> **Document type:** Phase 0.5 final gate review
> **Date:** 2026-05-20
> **Posture:** Adversarial. The goal is to find reasons *not* to proceed.
> **Companion documents:** [phase-0-report.md](../phase-0-report.md), [docs/phase0_consistency_audit.md](./phase0_consistency_audit.md), [docs/frozen_nfrs_v1.md](./frozen_nfrs_v1.md), [PHASE-MASTER-PROMPTS.md](../PHASE-MASTER-PROMPTS.md), [ADR.md](../ADR.md)

---

## Verdict

**Phase-1 Ready? YES**, with two caveats that are documented but
not blocking. See §3.

This document is the gating contract between Phase 0.5 and Phase 1.
A Phase 1 implementer should not begin work unless this document
remains in its `YES` state.

---

## 1. Gate checklist

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | ADRs synchronized with Phase 0 measurements | ✅ DONE | `ADR.md` ADR-01a added; ADR-01/02/03/04/16 status-noted |
| 2 | NFRs frozen at measured values | ✅ DONE | `docs/frozen_nfrs_v1.md`; `SYSTEM-ROADMAP.md §3.1` updated |
| 3 | Consistency audit complete | ✅ DONE | `docs/phase0_consistency_audit.md`; 17 of 22 issues resolved in this PR; 5 deferred to their respective later phases |
| 4 | DESIGN-REVIEW.md reflects Phase 0 discoveries | ✅ DONE | `DESIGN-REVIEW.md §9` added; §2.1/§2.2/§2.3 updated; §7 backlog tagged |
| 5 | ARCHITECTURE-DIAGRAMS.md latency budget reflects measured numbers | ✅ DONE | `ARCHITECTURE-DIAGRAMS.md §3` OLD/NEW table; §7 SENSE pipeline notes added |
| 6 | Phase 1 prompt revised | ✅ DONE | `PHASE-MASTER-PROMPTS.md` Phase 1 — USB link-speed bootstrap step added; reading list extended |
| 7 | USB link-speed validation specified | ✅ DONE | `SYSTEM-ROADMAP.md §5.1.7`, `ADR-01a §Decision (5)`, `PHASE-MASTER-PROMPTS.md` Phase 1 §2 |
| 8 | No document still claims pre-Phase-0 latency NFRs as primary | ✅ DONE | every relevant doc shows OLD vs NEW or annotates with a status note pointing at the frozen NFRs |
| 9 | Phase 0 bench harness present in repo for re-run | ✅ DONE | `bench/screencap_bench.py`, `bench/match_bench.py`, `bench/adb_overhead_bench.py`, `bench/raw_header_probe.py` |
| 10 | Raw screencap header layout verified for the operator's device | ✅ DONE | `phase-0-report.md §6`; `bench/artifacts/raw_header.txt` |
| 11 | USB autosuspend behavior verified | ✅ DONE | `bench/artifacts/usb_autosuspend.txt`; no remediation required for this host |
| 12 | Reading order for a Phase 1 implementer is explicit | ✅ DONE | `docs/phase0_consistency_audit.md §8` |

All twelve gate items are green.

---

## 2. What was actually frozen

The numbers a Phase 1 implementer (and later phases) MUST plan against:

| NFR | v1.0 target |
|---|---|
| Tick latency (median) | ≤ 1500 ms |
| Tick latency (p95) | ≤ 2000 ms |
| Screenshot capture (median, `sensor.mode = "raw"`) | ≤ 1000 ms |
| Screenshot capture (p95, `sensor.mode = "raw"`) | ≤ 1100 ms |
| Per-template match (median, ROI gray) | ≤ 5 ms |
| Per-template match (median, ROI BGR) | ≤ 10 ms |
| Per-template match (median, full-frame gray) | ≤ 50 ms |
| Per-template match (full-frame BGR) | opt-in only, no NFR target |
| Active templates per tick (default) | ≤ 8 (ROI-required) |
| Sustained tick rate (default) | 0.5–1 Hz |
| USB link speed at bootstrap | ≥ 480 Mbps |

Source of truth: [docs/frozen_nfrs_v1.md](./frozen_nfrs_v1.md).

---

## 3. Remaining risks (not blocking, documented)

### 3.1 PNG-vs-raw default may not match the operator's actual workload

Phase 0 measured two screen-content snapshots. The relative ordering
of PNG and raw modes reversed between them. The default
`sensor.mode = "raw"` is the safer choice (content-deterministic)
but may be slower than `"png"` for the operator's actual hot screens.

**Mitigation:** the Phase 2 sensor implementation must log per-capture
latency at INFO so the operator can see whether `"png"` would be
faster on their content. Phase 8 will run a representative session
and decide.

**Severity:** LOW. Picking the wrong default mode costs 100–700 ms
per capture in worst case. Not a correctness issue.

### 3.2 Device-side `screencap` composition cost is inferred, not measured

The ~620 ms device-side composition cost is derived from the gap
between raw screencap latency and the USB transport floor. Not
directly measured. A targeted experiment (`adb shell time screencap >
/sdcard/x.raw`) would pin it down.

**Mitigation:** captured as v1.1 backlog row #10 (`DESIGN-REVIEW.md §7`).
Does not block Phase 1.

**Severity:** LOW. The composition cost is what it is; knowing the
exact number doesn't change the v1.0 architecture.

---

## 4. Remaining unknowns (will be measured by their respective phases)

The following are listed in `docs/phase0_consistency_audit.md` as
"tracked → later" and remain unresolved at the end of Phase 0.5.
None block Phase 1.

- **Resampling cost** (ADR-04's 3–8 ms claim) — Phase 2 microbench.
- **RGBA→BGR conversion cost** (`cv2.cvtColor` vs indexing) — Phase 2 microbench.
- **Action engine per-class latency** (tap, swipe, long_press, key) — Phase 4 measures.
- **Detection accuracy NFRs** (§8 of SYSTEM-ROADMAP) — Phase 3 / Phase 8.
- **Long-run stability** (24 h soak, MTBF, MTTR) — Phase 7 / Phase 8.
- **CPU steady-state under realistic template set** — Phase 6 soak.
- **Logging overhead at default verbosity** — Phase 6.

Each of these has a phase that owns it and a microbench that will
measure it. None require a Phase 0.5 amendment.

---

## 5. Things explicitly out of scope for Phase 0.5

For the record, to prevent re-debate:

- **Implementing the bootstrap script.** That is Phase 1's work. Phase 0.5 only specifies the requirement.
- **Measuring USB 3.x.** The operator's device does not expose a USB 3.x port. Documented as a hardware limitation.
- **Benchmarking minicap.** No vetted minicap binary on the operator's device. Deferred per ADR-01.
- **Re-running Phase 0 benches against a different host/device.** Phase 0 is operator-specific by design; future operators repeat Phase 0 on their own hardware.
- **Revising Phase 2–8 prompts.** Each phase reviews its own prompt at the start of its execution. Phase 0.5 scopes only to Phase 1.

---

## 6. What an implementer should do before clicking "begin Phase 1"

In order:

1. Read `phase-0-report.md` end to end.
2. Read `docs/frozen_nfrs_v1.md` end to end.
3. Read `ADR.md` ADR-01a end to end.
4. Read `SYSTEM-ROADMAP.md` §3.1 (the updated table), §5.1 (full), §5.2 (full).
5. Read `DESIGN-REVIEW.md` §9 (the Phase 0 discoveries section).
6. Read `PHASE-MASTER-PROMPTS.md` Phase 1 — the entire updated prompt.
7. Verify on your terminal:
   - Python ≥ 3.11.
   - adb ≥ 34.
   - `adb get-state` prints `device`.
   - The device's USB link speed is ≥ 480 Mbps (`cat /sys/bus/usb/devices/<path>/speed` where `<path>` is resolved by matching `serial`).
8. If all of the above are green, begin Phase 1.

If any of step 7 fails, fix the operator-side issue before starting
Phase 1. Phase 1's `bootstrap.sh` will surface the same checks, but
finding the issue before writing code is cheaper than finding it
after.

---

## 7. Sign-off

This gate is unanimously **GREEN**. The repository is internally
consistent. Measured reality is the source of truth. ADR history is
preserved. NFRs are frozen.

Phase 1 may begin.

> *If a future maintainer is reading this and a Phase 1 implementation
> is already in flight that contradicts a frozen NFR or an ADR
> revision, **stop**. File a new ADR. Re-open this gate.*

---

## End of Phase 1 readiness gate
