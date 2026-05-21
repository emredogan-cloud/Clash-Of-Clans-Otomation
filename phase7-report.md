# Phase 7 Report — Hardening / Recovery / Fault Tolerance

> **Phase:** 7 — Hardening
> **Date:** 2026-05-21
> **Host:** Ubuntu 24.04.4 LTS, kernel 6.17, AMD Ryzen 5 5500
> **Device:** Xiaomi 22095RA98C (Redmi Note 11R), Android 13, USB 2.0 @ 480 Mbps
> **Companion documents:** [phase5-report.md](./phase5-report.md), [phase6-report.md](./phase6-report.md), [phase65-report.md](./phase65-report.md), [docs/frozen_nfrs_v1.md](./docs/frozen_nfrs_v1.md), [ADR.md ADR-07 / ADR-08a / ADR-11 / ADR-12 / ADR-16](./ADR.md), [PHASE-MASTER-PROMPTS.md Phase 7](./PHASE-MASTER-PROMPTS.md)

---

## 1. What was built

**Three Phase 7 modules** under `automation/`:

| File | Purpose | LOC |
|---|---|---:|
| `automation/runtime_health.py` | Immutable `RuntimeHealth` snapshot. Per-subsystem `_ok` flags + `last_error` + `degraded` + tz-aware `ts`. Includes `healthy()` factory and a strict degraded ↔ unhealthy/error coupling. | 165 |
| `automation/recovery.py` | `RecoveryManager(orchestrator, adb, logger?)`. Best-effort, one-shot `recover(error, correlation_id?) → RuntimeHealth`. Steps: (1) force orchestrator back to IDLE via the existing `_transition` chokepoint; (2) re-check ADB device state via `adb.get_state()`. No reboots, no `kill-server`, no root. | 230 |
| `automation/watchdog.py` | `Watchdog(orchestrator, recovery?, timeout_budgets_ms?)`. `run_tick() → TickResult`. Wraps one orchestrator tick; measures elapsed time; post-hoc tier-budget check; composes a `RuntimeHealth`; invokes one-shot recovery on fault; writes `WATCHDOG_DEBUG` metadata. No threads, no daemon, no signals. | 415 |

**Extensions**:

| File | Change |
|---|---|
| `automation/errors.py` | Added `WatchdogError`, `TimeoutFault`, `RecoveryError`. |
| `tests/test_runtime_health.py` | 18 tests — validation, frozen, degraded coupling, debug-dict shape, summary. |
| `tests/test_recovery.py` | 16 tests — orchestrator reset paths (all four mid-FSM states + FAILED + IDLE), ADB re-check (`device` / non-`device` / error / generic), best-effort logging, never-raise contract. |
| `tests/test_watchdog.py` | 31 tests — construction validation, pass-through, exception containment per subsystem, post-hoc timeout, recovery wiring (success / swallow on recovery fault / one-attempt-only), correlation propagation, artifact schema. |
| `scripts/phase7_live_validation.py` | Live harness exercising the three required scenarios + an overhead bench. |
| `bench/results/phase7_live_validation.json` | Sidecar with live measurements. |

Total Phase 7 net additions: **8 modified/created Python files**
+ 1 JSON sidecar + the report.

---

## 2. Fault model

### 2.1 Faults the watchdog catches and contains

| Fault category | Triggered by | Detection | Subsystem flagged unhealthy |
|---|---|---|---|
| Capture failure | `CaptureError`, `FrameDecodeError`, `UnsupportedPixelFormatError`, anything matching `Sensor` | exception from `tick()` | `sensor_ok=False` |
| Match failure | `MatcherError`, `InvalidROIError`, `MatchComputationError` | exception from `tick()` | `matcher_ok=False` |
| Actuator failure | `CoordinateError`, `ActionExecutionError`, anything matching `Actuator` | exception from `tick()` | `actuator_ok=False` |
| Invalid transition | `InvalidTransitionError`, `ValidationError`, `OrchestratorError` | exception from `tick()` | `orchestrator_ok=False` |
| ADB transport | `ADBError` | exception from `tick()` | `sensor_ok=False` AND `actuator_ok=False` |
| Telemetry failure (logger/metrics) | exception from inside the orchestrator's logger/metrics path | caught + WARN-logged inside the orchestrator (Phase 6) | not reflected in Watchdog health — the orchestrator absorbs it |
| Timeout (post-hoc) | tick exceeded its tier budget | `elapsed_ms > budget_ms` after `tick()` returns | `orchestrator_ok=False` |
| Unknown exception | any other `Exception` | exception from `tick()` | pessimistic `orchestrator_ok=False` |

### 2.2 Containment guarantees

Every supervised tick is wrapped in `try/except Exception`. The
watchdog **never re-raises** — it returns a `TickResult` (synthetic
on raise) and publishes a `RuntimeHealth` snapshot. Concretely:

- `orchestrator.tick()` may raise → caught → synthetic `IDLE →
  FAILED` `TickResult` returned + degraded health.
- `recovery.recover(...)` may raise → caught → `WARN` log + the
  immediate (pre-recovery) `RuntimeHealth` retained as
  `last_health`.
- `logger.log_*` or `metrics.observe_*` failures inside recovery
  → swallowed by recovery's own best-effort layer.

This is the Phase 7 "Contain inside Phase 7. Do NOT crash whole
runtime." promise.

### 2.3 Pre-emption model (honest stance)

Phase 7 **does not pre-empt** a running tick. The prompt forbids
threads / daemons / infinite loops, and CPython has no portable
way to interrupt blocking I/O from a non-main thread without
SIGALRM. We made a deliberate choice **not** to use SIGALRM in
v1.0:

- It is Unix-only and main-thread-only — fine for the framework
  but awkward for tests.
- A truly hung Python-level loop in `tick()` would not be caught
  even by SIGALRM (signal delivery requires the interpreter to
  reach a check-point).

Instead, Phase 7 enforces **post-hoc soft timeouts**:

1. `tick()` is called synchronously.
2. After it returns (or raises), `elapsed_ms` is compared against
   the tier-specific budget.
3. If exceeded, a `TimeoutFault` flag is set on the artifact +
   `last_health.last_error`.

A truly hung tick is bounded by the **lower-layer timeouts** the
existing framework already enforces:

- `ADB.shell` — `timeout=10.0 s` (`automation/adb.py`).
- `Sensor.capture` — `CAPTURE_TIMEOUT_S=30.0 s`
  (`automation/sensor.py`).
- `Actuator.tap/swipe/long_press` — `ACTION_TIMEOUT_S=10.0 s`
  (`automation/actuator.py`).

So the absolute hang ceiling per tick is bounded by these. Even
worst-case (capture-stalls-30 s + 2× validate cycles), the
framework cannot hang indefinitely.

---

## 3. Recovery behaviour

`RecoveryManager.recover(error, correlation_id=None) → RuntimeHealth`
is best-effort and **always returns a snapshot**; it never raises
to its caller. Two steps:

### 3.1 Step 1 — orchestrator back to IDLE

| Pre-recovery state | Action | Post-recovery state |
|---|---|---|
| `IDLE` | no-op | `IDLE` |
| `SEARCHING` | `_transition(FAILED, "recovery: force FAILED")` → `reset()` | `IDLE` |
| `ACTING` | same | `IDLE` |
| `VALIDATING` | same | `IDLE` |
| `FAILED` | `reset()` only | `IDLE` |

The `_transition` call uses the orchestrator's existing
centralized chokepoint (Phase 5 design). The allowed-transitions
table (`automation/state.py`) defines `SEARCHING → FAILED`,
`ACTING → FAILED`, and `VALIDATING → FAILED` as legal edges, so
this is not adding new orchestrator behaviour — only invoking
existing behaviour. The Phase 7 prompt's "do NOT redesign
orchestrator" is respected: the orchestrator's source remains
byte-identical to Phase 5/6.

### 3.2 Step 2 — ADB re-check

| `adb.get_state()` outcome | Action |
|---|---|
| returns `"device"` | sensor + actuator `_ok=True` |
| returns anything else | sensor + actuator `_ok=False`; `last_error` mentions the bad state |
| raises `ADBError` | sensor + actuator `_ok=False`; `last_error` mentions the ADB error |
| raises any other `Exception` | sensor + actuator `_ok=False`; pessimistic |

Matcher is CPU-bound and not affected by ADB; `matcher_ok` is
unchanged by Step 2.

### 3.3 Step 3 — best-effort log

If a `StructuredLogger` is supplied, `recover()` emits one record
to `errors.jsonl` carrying the correlation id, error type,
message, FSM state, reset note, ADB note, and the resulting
health dict. Failure of this log write is caught and swallowed —
telemetry faults during recovery cannot crash the framework.

### 3.4 What recovery does NOT do

Per the Phase 7 prompt's prohibitions:

- No `adb kill-server` / `start-server` (that is the v1.1+
  `RESET_HARD` state from SYSTEM-ROADMAP §11.1).
- No `am force-stop` against any app.
- No device reboot / USB reset.
- No root / `su` invocations.
- No retries beyond the single attempt the watchdog issues.

---

## 4. Timeout results (live)

Configured: `search_only` 200 ms (deliberately tight), `validated`
4000 ms, `validated_retry` 5000 ms (default). Real device, random-
noise template, Settings baseline (per Phase 6.5).

```
Scenario 2 — forced timeout (tight search_only budget = 200 ms)
  TickResult: TickResult(FAIL IDLE→FAILED tick=981.7 ms ...)
  Health:     RuntimeHealth(DEGRADED ... last_error=
              'TimeoutFault: tick 983.70 ms exceeded
               search_only budget 200 ms')
  Timeout flagged: True
```

The real tick took **983.70 ms** (one full SENSE capture + match
on Settings, SEARCH miss). With `search_only` budget tightened
to 200 ms, the post-hoc detection flagged a `TimeoutFault`. The
watchdog invoked recovery, which reset the orchestrator back to
IDLE (it was in FAILED from the natural SEARCH-miss tick).
**Detection works.**

With the production defaults (search_only 2500 ms / validated
4000 ms / validated_retry 5000 ms), Phase 5/6 live ticks never
breach any tier — the budgets sit comfortably above the
frozen-NFR p95s:

| Tier | Production budget | Phase 6 measured median | Headroom |
|---|---:|---:|---:|
| search_only | 2500 ms | 1066 ms | 1.4 s |
| validated | 4000 ms | 1917 ms | 2.1 s |
| validated_retry | 5000 ms | 2972 ms | 2.0 s |

### 4.1 Tier-derivation simplification

`TickResult` (Phase 5 schema) does not carry `retries_used`, so
the watchdog cannot strictly distinguish `validated` from
`validated_retry`. It infers a coarse tier from `action_latency_ms`:

- `action_latency_ms is None` → `search_only`.
- `action_latency_ms is set` → `validated_retry` (the more lenient
  budget, conservative choice).

The strict `validated` 4000 ms budget is **not separately enforced**
in v1.0 Phase 7. A future enhancement that surfaces `retries_used`
on `TickResult` would let the watchdog distinguish the tiers
strictly. Out of v1.0 scope (touching `TickResult` is a Phase-5
schema change).

---

## 5. Hardening overhead

The watchdog's per-tick wrapping cost (the cost the framework pays
in exchange for hardening) was measured over **500 supervised
calls** against a noop orchestrator. This isolates the watchdog
overhead from the underlying tick cost.

```
watchdog wrap cost over 500 calls (noop orchestrator):
  mean   = 0.018 ms
  median = 0.017 ms
  p95    = 0.025 ms
≈ 0.002% of a 1000 ms search_only tick
```

The Phase 7 NFR is `hardening overhead < 5% of tick`. Measured
overhead is **0.002%** on the operator's hardware — **2500×
under budget**. The instrumentation surface is essentially free.

For context vs Phase 6's logger+metrics overhead (1.788 ms median
per tick, ~0.2% of tick), the Phase 7 watchdog adds another ~0.02%.
Composed: Phase-6 + Phase-7 instrumentation is still ≤ 0.25% of
a search-only tick.

When the watchdog writes a debug artifact (`WATCHDOG_DEBUG=1`),
add ~0.5–1 ms per artifact (one `json.dumps` + atomic write).
Still under 0.1% of tick. The live validation runs with the debug
flag enabled to exercise the artifact path; the production
default is debug-off.

---

## 6. Test results

```
$ .venv/bin/pytest -q
================================ 517 passed in 2.42s ===========================
```

**517 / 517 tests pass** (Phase 6 baseline: 452; Phase 7 net +65).

### 6.1 Coverage

```
Name                           Stmts   Miss  Cover
--------------------------------------------------
automation/runtime_health.py      51      1    98%
automation/recovery.py            69      2    97%
automation/watchdog.py           140      6    96%
... (prior phases unchanged or improved)
--------------------------------------------------
TOTAL                           1807    110    94%
```

Phase 7 module coverage: **runtime_health 98%, recovery 97%,
watchdog 96%**. Package-wide coverage rose from 93% (Phase 6) to
**94%** with no regression in prior modules. All new modules well
above the ≥ 90% bar.

Uncovered Phase 7 lines are defensive: the catch-all
`OSError/ValueError` exception cleanup in `watchdog._write_artifact`
and `recovery`'s fall-through paths when both reset AND ADB error
simultaneously. Both are exercised in conceptually nearby tests
(`test_artifact_write_failure_does_not_break_run_tick`,
`test_recover_always_returns_runtime_health`) but the precise
lines escape via different code branches.

### 6.2 Test inventory (Phase 7 additions only)

| File | Tests | Key coverage |
|---|---:|---|
| `tests/test_runtime_health.py` | 18 | construction; healthy() factory + default `ts`; subsystem-`_ok` type rejection; `last_error` type rejection; naive-ts rejection; degraded ↔ unhealthy coupling (both directions); empty-string error rejection; frozen; hashable; debug-dict JSON safety; summary lists impacted subsystems |
| `tests/test_recovery.py` | 16 | reset from FAILED; force-FAILED from SEARCHING / ACTING / VALIDATING; IDLE no-op; reset failure marks orchestrator unhealthy; ADB error path; non-`device` state path; non-ADBError exception caught pessimistically; clean recovery still degraded (because original error preserved); logger emission + collision rules; logger failure swallowed; works without logger; always-returns-RuntimeHealth; original error preserved in `last_error`; tz-aware ts |
| `tests/test_watchdog.py` | 31 | default budgets match spec; custom budgets; missing-tier rejected; zero/non-int budget rejected; initial healthy; pass-through on success; healthy on success; no recovery on success; exception containment per subsystem (capture/actuator/matcher/transition/adb/unknown); post-hoc timeout detection (tight budget); no false-positive when within budget; tier inference (validated_retry vs search_only); recovery wiring (exception triggers, timeout triggers); recovery health published as last_health; recovery swallowed; one-attempt-only; no recovery without recovery=...; per-tick correlation id; artifact written/skipped/env-var/atomic/failure-survival |

### 6.3 Determinism

All Phase 7 tests pass under the strict `filterwarnings = ["error"]`
pytest setting. No test depends on real time (the few that use
`time.sleep` use small fixed durations like 200 ms — fast and
reliable), real ADB, real OpenCV, or real device.

---

## 7. NFR comparison

| NFR | Target | Measured | Status |
|---|---|---:|---|
| Hardening overhead per tick | < 5% of tick | 0.002% (median 0.017 ms vs 1000 ms tick) | ✅ |
| Watchdog catches exceptions without crashing | required | all 6 exception classes exercised by tests + live | ✅ |
| Watchdog enforces tier budgets | required | post-hoc detection verified live (Scenario 2) | ✅ (soft) |
| Recovery returns RuntimeHealth always | required | 4 contract tests including triple-failure path | ✅ |
| Recovery is one-shot per call | required | `test_run_tick_recovery_only_one_attempt_per_call` | ✅ |
| Recovery is observable via Phase 6 logger | required | `test_recover_emits_log_error_when_logger_supplied` | ✅ |
| `last_health` published after every `run_tick` | required | structural — `_last_health` field always set | ✅ |
| Artifact carries correlation id + tier + health + recovery flags | required | live artifact schema verified | ✅ |
| Phase 6 telemetry surfaces preserved | required | logger/metrics/correlation all reachable by recovery + orchestrator unchanged | ✅ |
| New module coverage | ≥ 90% | 96%–98% | ✅ |

---

## 8. Phase-8 readiness

| Phase-8 prerequisite | Status |
|---|---|
| Recovery cascade ground floor (Phase 7) | ✅ — `RecoveryManager.recover()` is the L1 layer per ADR-11 |
| External watchdog process (Phase 7 supervisor) | ❌ — Phase 7 ships an **in-process** `Watchdog` (per the prompt's prohibition on daemons). The **external** watchdog process specified in `PHASE-MASTER-PROMPTS.md` Phase 7 (`watchdog/watchdog.py`, systemd unit) is **NOT** delivered. See §10 below. |
| `RuntimeHealth` published surface for downstream consumers | ✅ |
| Telemetry surfaces still consumable | ✅ — Phase 6 untouched |
| Tier-aware budgets configurable | ✅ — `timeout_budgets_ms` constructor arg |
| Phase 7 tests provide fault-injection patterns for Phase 8 soak | ✅ — `_MockOrchestrator(raise_on_tick=...)`, `_MockADB(raise_adb_error=...)` |
| `phase55_consistency_patch.md` §2.12 (PHASE-MASTER-PROMPTS Phase 5 vs delivered scope) | OPEN — Phase 8 must reconcile (recovery cascade RESET_LITE / RESET_HARD / RECONNECTING is Phase 8 work). |

**Phase 8 can begin** for the elements within the in-process
hardening layer that Phase 7 delivered. The **external watchdog
process** and **systemd unit** require a separate, non-runtime
scope (see §10) that the next phase should address.

---

## 9. Files

```
automation/runtime_health.py          165 lines  (new)
automation/recovery.py                230 lines  (new)
automation/watchdog.py                415 lines  (new)
automation/errors.py                  +47 lines  (WatchdogError, TimeoutFault, RecoveryError)

scripts/phase7_live_validation.py     ~250 lines (new throwaway harness)

tests/test_runtime_health.py          ~180 lines (18 tests)
tests/test_recovery.py                ~265 lines (16 tests)
tests/test_watchdog.py                ~410 lines (31 tests)

bench/results/phase7_live_validation.json   (sidecar)
phase7-report.md                      (this file)
```

Phase 7 net additions: **11 modified/created files** (8 Python +
1 sidecar + 1 throwaway harness + this report).

---

## 10. Unresolved risks

### 10.1 In-process watchdog vs external watchdog process

The Phase 7 prompt prohibits "daemon" and "threads" and asked for
a single-call supervision class. We delivered exactly that:
`Watchdog.run_tick()` is invoked per tick by the caller. But the
original `PHASE-MASTER-PROMPTS.md` Phase 7 (and ADR-11) describes
an **external** watchdog process that reads a heartbeat file and
SIGTERMs / SIGKILLs the framework on staleness. That is **NOT**
shipped by v1.0 Phase 7.

**Implications:**
- A truly hung Python-level loop in `tick()` (i.e., a bug, not an
  expected fault) is not caught by either the in-process watchdog
  OR by an external watchdog (the latter doesn't exist).
- A segfault inside an OpenCV C extension would crash the whole
  framework with no restart.

**Path forward (v1.1 candidate):** add the external watchdog as a
small `watchdog/watchdog.py` script + a systemd `--user` unit
per ADR-11. ~50 LOC stdlib-only. Tracked in
`docs/phase55_consistency_patch.md` §2.12 (open item, deferred
to Phase 8 per the original PHASE-MASTER-PROMPTS Phase 5 scope
reconciliation).

### 10.2 Tier inference is coarse

The watchdog cannot distinguish `validated` from `validated_retry`
without `retries_used` on `TickResult`. It uses the
`validated_retry` (lenient) budget for any action-bearing tick.
This means a tick that legitimately exceeded the `validated`
budget but stayed under `validated_retry` would be considered
in-budget by the watchdog. The Phase 6 metrics still record the
precise tier (via the orchestrator's own metadata), so this is a
watchdog-policy gap, not an observability gap.

**Path forward:** surface `retries_used` on `TickResult`. A
small Phase 5 schema amendment.

### 10.3 No pre-emption

As discussed in §2.3: a truly hung Python-level path is not
interrupted by the watchdog. The lower-layer timeouts (ADB shell,
Sensor capture, Actuator) bound this in practice. A SIGALRM-based
preemption is a v1.1 candidate; the trade-off is signal-handling
fragility vs the rare case of a hard Python loop.

### 10.4 Recovery does not restart ADB

`adb kill-server` / `start-server` is the natural next step for
recovery when `adb get-state` returns errors. It's also dangerous
(brief connection lost; race with other ADB users on the host).
Per the Phase 7 prompt's prohibition on `RESET_HARD` semantics,
this is deferred. ADR-11's `RESET_HARD` state remains a Phase 8+
scope.

### 10.5 Watchdog does not auto-reset after FAILED-by-design ticks

A SEARCH-miss tick lands in `FAILED` via the FSM design. The
watchdog does **not** auto-reset the orchestrator — the caller is
responsible. This was a deliberate choice: SEARCH miss is normal
FSM behaviour, not a fault; auto-reset would conflate the two and
add policy the orchestrator was designed to avoid.

**Live consequence:** the Phase 7 demo harness explicitly calls
`orch.reset()` between scenarios. A future "run loop" caller
(Phase 8 soak harness) will need a similar `if state == FAILED:
orch.reset()` pattern, OR a thin wrapper class above the watchdog
that adds the policy.

### 10.6 Recovery's "best-effort" makes some faults silent

If `RecoveryManager.recover()` itself fails (e.g., the ADB
re-check raises), the framework records WARN and continues. The
exception is contained but not surfaced to the caller. The next
`run_tick()` will likely fail again the same way. This is the
correct behaviour for v1.0 (no crash loop), but operators may
want a circuit-breaker that escalates after N consecutive
recovery failures. A v1.1 candidate.

### 10.7 Live validation sample is small

Phase 7's live harness runs **3 scenarios** + an off-tick overhead
bench. Statistically thin compared to Phase 6's 10-tick run, but
adequate to demonstrate each fault category. Phase 8's soak test
provides the long-tail evidence.

---

## 11. Readiness verdict

**Phase 7: COMPLETE.**

- 517 / 517 tests pass; **94%** package coverage; Phase-7 modules
  at 96%–98% individually.
- Live device validation: three scenarios (normal supervised
  tick, forced timeout, forced recovery) all completed without
  crashing the framework. The watchdog caught faults, the
  recovery brought the orchestrator back to IDLE, and the
  artifact + log + metrics surfaces are unchanged.
- Hardening overhead **0.002% of tick** vs the 5% NFR — over
  three orders of magnitude under budget.
- All Phase 7 prompt prohibitions honoured: no daemon, no
  threads, no infinite loop, no orchestrator/telemetry redesign,
  no reboots, no app-kills, no soak automation, no Phase 8+
  functionality.
- Acknowledged limitations: in-process watchdog only (external
  watchdog process deferred to v1.1 / Phase 8); coarse tier
  inference (no `retries_used` on TickResult); no preemption
  (lower-layer timeouts bound hangs in practice).

The Phase 8 implementer should next read
`PHASE-MASTER-PROMPTS.md` Phase 7's *original* scope (recovery
cascade `RESET_LITE` / `RESET_HARD` / `RECONNECTING`; external
watchdog; heartbeat writer; soak test scaffold), this report's
§10 (unresolved risks), and `docs/phase55_consistency_patch.md`
§2.12 (still OPEN).
