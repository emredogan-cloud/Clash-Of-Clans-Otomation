# Phase 8A Report — External Watchdog (L2 observation) / Heartbeat / ADR-11 Reconciliation

> **Phase:** 8A — External L2 supervision (observation half)
> **Date:** 2026-05-21
> **Host:** Ubuntu 24.04.4 LTS, kernel 6.17, AMD Ryzen 5 5500
> **Device:** Xiaomi 22095RA98C (Redmi Note 11R), Android 13, USB 2.0 @ 480 Mbps
> **Companion documents:** [phase7-report.md](./phase7-report.md), [docs/phase55_consistency_patch.md](./docs/phase55_consistency_patch.md), [ADR.md ADR-11 / ADR-11a / ADR-12 / ADR-16](./ADR.md), [SYSTEM-ROADMAP.md §5.7 / §11](./SYSTEM-ROADMAP.md), [PHASE-MASTER-PROMPTS.md](./PHASE-MASTER-PROMPTS.md)

---

## 1. What was built

**A new top-level `watchdog/` package** delivering the
observation + escalation-policy half of ADR-11's L2 watchdog.
Two modules, both stdlib-only, both free of any `automation/`
runtime imports beyond the typed exception classes in
`automation.errors`.

| File | Purpose | LOC |
|---|---|---:|
| `watchdog/__init__.py` | Package init; documents the L2 contract. | 25 |
| `watchdog/heartbeat.py` | `HeartbeatWriter(path)` + `beat(correlation_id, runtime_health)`. Atomic JSON writes of the per-tick liveness beacon. Duck-types `runtime_health` (any object with `to_debug_dict()`) so the writer never imports `automation/runtime_health`. | 230 |
| `watchdog/watchdog.py` | `ExternalWatchdog(heartbeat_path, stale_after_s=15)`. `check() → WatchdogStatus`. Reads heartbeat, classifies freshness (HEALTHY / STALE / MISSING / INVALID), maps to recommendation. Immutable `WatchdogStatus` container. Optional `WATCHDOG_L2_DEBUG` artifact. | 380 |

**Extensions**:

| File | Change |
|---|---|
| `automation/errors.py` | Added `HeartbeatError`, `ExternalWatchdogError`. |
| `tests/test_heartbeat.py` | 19 tests — atomic write, schema, overwrite, validation, I/O-failure swallowing. |
| `tests/test_external_watchdog.py` | 40 tests — `WatchdogStatus` validation, construction validation, HEALTHY / STALE / MISSING / INVALID classification (with subtypes), clock-skew clamping, boundary conditions, escalation map completeness, artifact behavior, **module-does-not-import-orchestrator** structural test. |
| `scripts/phase8a_live_validation.py` | Throwaway live harness for the three reachable verdicts + overhead bench. |
| `bench/results/phase8a_live_validation.json` | Sidecar JSON. |
| `ADR.md` | Added ADR-11a (L1/L2 supervision split). ADR-11 status-noted with the delivery split. |
| `SYSTEM-ROADMAP.md` §5.7 / §5.7.1 | Reconciled L1 vs L2 delivery; deferred L2 *action* layer to Phase 8B explicitly. |
| `DESIGN-REVIEW.md` §10a | Six Phase 8A discoveries catalogued. v1.1 backlog rows #24 and #25 added. |
| `docs/phase55_consistency_patch.md` §2.12 | Marked PARTIALLY RESOLVED — Phase 7 closed L1; Phase 8A closes L2 observation; remaining gaps documented. |

**No changes to:** `automation/orchestrator.py`, `automation/watchdog.py`
(the Phase 7 L1 supervisor), `automation/sensor.py`,
`automation/matcher.py`, `automation/actuator.py`,
`automation/metrics.py`, `automation/logger.py`, or any other
runtime module beyond `errors.py`. The framework's runtime
behaviour is byte-identical to Phase 7.

Total Phase 8A net additions: **11 modified/created files**
(2 new package modules + 1 new package init + 2 new tests +
1 throwaway script + 1 sidecar + this report + 4 docs touched).

---

## 2. L1 vs L2 model

| Layer | Process | Responsibility | Status (2026-05-21) |
|---|---|---|---|
| **L1 supervision** | inside framework | wrap each tick, catch exceptions, post-hoc timeout flag, build `RuntimeHealth` | ✅ Phase 7 |
| **L1 recovery** | inside framework | force orchestrator → IDLE, re-check ADB, one-shot best-effort | ✅ Phase 7 |
| **L2 observation** | *outside* framework | read heartbeat, classify freshness, emit `WatchdogStatus` + recommendation (data only) | ✅ **Phase 8A — this PR** |
| **L2 action** | *outside* framework | translate L2 recommendation into action (SIGTERM/SIGKILL, restart, restart-rate ceiling) | ❌ Phase 8B |

The boundary between L2 observation and L2 action is the
**recommendation string**. Phase 8A's `WatchdogStatus.recommendation`
is one of three wire-stable tokens (`none` / `RESET_LITE` /
`RESET_HARD`). Phase 8B will consume those tokens; the v1.0 Phase
8A consumer is the operator (or the live-validation harness).

Why split this way:

- The Phase 8A prompt explicitly prohibited daemons, threads,
  signal handlers, kill semantics, reboot, and any `systemd`
  dependency. Implementing the action layer would require at
  least one of those.
- Splitting observation from action lets operators wire the L2
  into any supervision substrate — `systemd --user`, container
  restart policy, a runit / s6 service, a manual shell loop —
  without re-implementing the observer.
- The L2 observer needs to keep running when the framework has
  crashed. That's the whole point of ADR-11's "external"
  designation. Putting it in the framework process (Phase 7) is
  conceptually impossible.

---

## 3. Heartbeat schema

The framework writes one heartbeat record per tick. The contract
is documented in `watchdog/heartbeat.py:HEARTBEAT_SCHEMA_VERSION = 1`.

```json
{
  "schema_version": 1,
  "ts": "2026-05-21T10:11:46.590471+00:00",
  "correlation_id": "tick_20260521T101146_590471",
  "degraded": false,
  "health": {
    "sensor_ok": true,
    "matcher_ok": true,
    "actuator_ok": true,
    "orchestrator_ok": true,
    "last_error": null,
    "degraded": false,
    "ts": "2026-05-21T10:11:46.590471+00:00"
  },
  "pid": 810303
}
```

Field semantics:

- **`schema_version`** (int) — bumped on any breaking change. The
  L2 watchdog refuses to interpret unknown versions (returns
  `INVALID + RESET_HARD`).
- **`ts`** (ISO 8601 UTC, tz-aware) — instant the heartbeat was
  written. Used by the L2 watchdog to compute age.
- **`correlation_id`** (str) — the supervised tick's correlation id,
  matching the format from `automation/correlation.py`. Cross-
  referenceable with `var/logs/ticks.jsonl` and the orchestrator's
  artifact directories.
- **`degraded`** (bool) — convenience flag, mirrors
  `health.degraded`. Lets a future L2 action layer branch on
  "framework reports it is degraded" without parsing the nested
  health object.
- **`health`** (object) — full `RuntimeHealth.to_debug_dict()`
  (subsystem flags + last_error + degraded + ts).
- **`pid`** (int) — the framework process id. Reserved for the
  Phase 8B action layer to know which pid to SIGTERM.

Atomic semantics: every `beat()` writes to a tmpfile in the same
directory and then `os.replace()`s over the destination. On POSIX,
this is the canonical atomic rename — concurrent readers see
either the old complete file or the new complete file. No partial
content.

Best-effort I/O: a routine ENOSPC / EACCES / EBUSY during the
write is logged at WARN and swallowed. The framework cannot
crash on a missed heartbeat (the L2 watchdog interprets a
missing-or-stale heartbeat as `RESET_LITE`, exactly the right
behaviour).

---

## 4. Escalation policy

The classification → recommendation map. Single source of truth
in `watchdog/watchdog.py`. Wire-stable for v1.0.

| Status   | Trigger                                                | Recommendation |
|----------|--------------------------------------------------------|----------------|
| HEALTHY  | heartbeat present + parses + valid schema + age ≤ T    | `none`         |
| STALE    | heartbeat present + parses + valid schema + age > T    | `RESET_LITE`   |
| MISSING  | heartbeat file does not exist                          | `RESET_LITE`   |
| INVALID  | heartbeat exists but malformed JSON / bad schema / unreadable | `RESET_HARD`   |

Rationale:

- `MISSING` → `RESET_LITE`: the framework may not have started
  yet, or never wrote a beat. A light recovery (re-launch the
  framework process) is the appropriate first step.
- `STALE` → `RESET_LITE`: the framework was alive recently but
  has stopped beating. A light recovery is still the right call.
- `INVALID` → `RESET_HARD`: the framework wrote a malformed
  beat — that indicates either (a) a schema mismatch between L2
  watchdog version and framework version, or (b) a write that
  was interrupted in a non-atomic way (which the writer's
  implementation rules out, but a corrupted FS could produce).
  Both cases warrant the heavier handler.

The recommendation is **data only** in Phase 8A. The Phase 8B
action layer will consume it.

### 4.1 What Phase 8A explicitly does NOT do

Per the Phase 8A prompt's prohibitions:

- No `os.kill`, no `subprocess.run("kill", ...)`, no signal
  delivery.
- No `subprocess.run("systemctl", ...)`.
- No `adb kill-server` / `start-server`.
- No reboot, no `am force-stop`.
- No infinite loop, no daemon, no thread, no signal handler.
- No write to `var/run/watchdog-restarts.log` (that's
  Phase 8B's restart-rate-ceiling state).

The L2 observer is a **pure function**: given a heartbeat file at
a path, it returns a `WatchdogStatus`. Side effects are limited
to the optional `var/artifacts/external_watchdog/.../metadata.json`
debug write, which is itself best-effort.

---

## 5. L2 overhead

Per-check cost measured over 500 invocations of
`ExternalWatchdog.check()` against a valid heartbeat (warmup
discarded). Source: `bench/results/phase8a_live_validation.json`.

| Metric | Value |
|---|---:|
| Mean | 0.036 ms |
| Median | **0.030 ms** |
| p95 | 0.051 ms |

**NFR: < 1 ms per check. Measured median: 0.030 ms.**
**Headroom: 33× under budget.** With debug artifact writes
enabled, expect ~0.5 ms extra per check (one `json.dumps` +
atomic write); still well under the budget.

The check is essentially:
1. One `Path.is_file()` syscall (≈ 10 µs).
2. One `Path.read_text()` (≈ 10 µs for the small heartbeat).
3. One `json.loads()` (~5 µs on the ~600-byte payload).
4. One `datetime.fromisoformat()` + arithmetic (≈ 1 µs).
5. WatchdogStatus construction + validation (≈ 5 µs).

For a Phase 8B action layer polling at e.g. 1 Hz, the L2
watchdog's CPU footprint is ~0.03 ms / 1000 ms = **0.003% of one
core**. The L2 supervisor is effectively free.

---

## 6. Test results

```
$ .venv/bin/pytest -q
================================ 576 passed in 2.51s ===========================
```

**576 / 576 tests pass.** Phase 7 baseline: 517; Phase 8A net +59.

### 6.1 Coverage

```
Name                           Stmts   Miss  Cover
--------------------------------------------------
watchdog/__init__.py               1      0   100%
watchdog/heartbeat.py             65      6    91%
watchdog/watchdog.py             144      8    94%
... (prior phases unchanged)
--------------------------------------------------
TOTAL                           2019    124    94%
```

**Phase 8A module coverage: heartbeat 91%, watchdog 94%.**
Both above the ≥ 90% bar. Package-wide coverage holds at **94%**.

Uncovered Phase 8A lines are defensive:

- `heartbeat.py` — `_atomic_write_text` exception cleanup,
  reachable but hard to trigger without monkeypatching `os` itself.
- `watchdog.py` — the WatchdogStatus summary "age=—" branch when
  age_s is None (exercised in `test_watchdog_status_age_may_be_none`
  via construction but not via the summary path); the artifact
  exception-cleanup path.

### 6.2 Test inventory (Phase 8A additions only)

| File | Tests | Key coverage |
|---|---:|---|
| `tests/test_heartbeat.py` | 19 | construction rejects non-`Path`; schema constants; required fields written; degraded flag mirrored; default ts is recent; parent dir auto-created; overwrite; atomic — no `.tmp` leak; 50 sequential writes; reject empty/non-string correlation_id; reject health without to_debug_dict; propagate to_debug_dict failure; reject naive ts; reject non-datetime ts; reject non-JSON-encodable health; I/O failure swallowed; `last_written_ts` None before first beat / tz-aware after |
| `tests/test_external_watchdog.py` | 40 | `WatchdogStatus` validation (status / recommendation / age_s / ts coupling, frozen, debug-dict shape, summary); `ExternalWatchdog` construction (non-Path, zero / negative / non-number / bool threshold rejected); default threshold matches constant; HEALTHY (fresh; at zero age; clock-skew clamp); STALE (above threshold; boundary at threshold = healthy strict >); MISSING (no file; missing parent dir); INVALID (malformed JSON / array root / missing field / wrong schema version / non-int schema version / non-string ts / unparseable ts / naive ts / unreadable file); escalation-map completeness; artifact written/skipped/env-var/atomic/failure-survival; `parse_note` recorded for INVALID; **structural test that the L2 module does not import any forbidden `automation/*` module** |

### 6.3 Determinism

All 59 new tests pass under `filterwarnings = ["error"]`. Three
back-to-back full runs produce byte-identical output. No tests
depend on real ADB / real OpenCV / real device. The few that
sleep do so with deterministic durations (`time.sleep(2.0)`
in Scenario 2 is the only real-clock dependency, and only in
the live-validation script — the unit tests use injected `ts`
parameters).

---

## 7. ADR-11 reconciliation

### 7.1 What was reconciled

| Doc | Edit |
|---|---|
| `ADR.md` ADR-11 | Status note added — L1 shipped in Phase 7; L2 observation shipped in Phase 8A; L2 action deferred to Phase 8B. ADR-11 text unchanged. |
| `ADR.md` ADR-11a | New addendum documenting the L1/L2 delivery split, escalation policy, and the four delivery units (L1 supervision, L1 recovery, L2 observation, L2 action). |
| `SYSTEM-ROADMAP.md` §5.7 | Rewrote the "Two layers" subsection to match the Phase 7 / Phase 8A / Phase 8B delivery split. |
| `SYSTEM-ROADMAP.md` §5.7.1 | Re-labelled as "Phase 8B" work; preserves the systemd-unit topology as the substrate the future Phase 8B action layer targets. |
| `DESIGN-REVIEW.md` §10a | Six Phase 8A discoveries: L2 observation sufficient; heartbeat not auto-wired (DEFER → Phase 8B); recommendation strings wire-stable; no L2→L1 leakage; heartbeat schema versioned; status enum closed. v1.1 backlog rows #24 and #25 added. |
| `docs/phase55_consistency_patch.md` §2.12 | Marked PARTIALLY RESOLVED. L1 recovery (Phase 7) + L2 observation (Phase 8A) closed. L2 action + Script + Mermaid exporter + CLI still OPEN. |

### 7.2 Preserved history

Per the dossier convention (ADR-01a / ADR-08a precedent), no
existing ADR text was destructively edited. ADR-11 still reads as
the original two-layer decision; ADR-11a is an additive amendment.
SYSTEM-ROADMAP §5.7 was rewritten to reflect delivered reality
but the original ADR-11 deferred design is still accessible at
that ADR.

### 7.3 What still needs to ship (Phase 8B candidates)

| Item | ADR / spec | Phase 8A status |
|---|---|---|
| systemd `automation.service` unit | ADR-11 / SYSTEM-ROADMAP §5.7.1 | not yet shipped |
| systemd `automation-watchdog.service` unit (or equivalent supervisor) | ADR-11 / SYSTEM-ROADMAP §5.7.1 | not yet shipped |
| `SIGTERM → wait → SIGKILL → restart` action consuming the L2 recommendation | ADR-11 | not yet shipped |
| Restart-rate ceiling + `var/run/watchdog-restarts.log` | ADR-11 / DESIGN-REVIEW §10a.1 | not yet shipped |
| Auto-wire `HeartbeatWriter.beat()` into the Phase 7 `Watchdog.run_tick()` | DESIGN-REVIEW §10a.2, backlog #24 | not yet shipped |
| FSM recovery cascade (RESET_LITE / RESET_HARD / RECONNECTING states) | SYSTEM-ROADMAP §11.1, ADR-08 | not yet shipped |

Phase 8B is the natural home for all of the above; Phase 8A
deliberately stayed within the *observation* boundary.

---

## 8. Phase-8B readiness

| Phase-8B prerequisite | Status |
|---|---|
| L1 supervision + recovery available for the action layer to consult | ✅ Phase 7 |
| L2 observation API with wire-stable recommendations | ✅ Phase 8A — `WatchdogStatus.recommendation ∈ {"none", "RESET_LITE", "RESET_HARD"}` |
| Heartbeat writer exists and is testable | ✅ Phase 8A — `HeartbeatWriter` |
| Heartbeat schema versioned | ✅ Phase 8A — `schema_version: 1` |
| Escalation policy is data-only (no side effects) so Phase 8B can choose its substrate freely | ✅ Phase 8A |
| Process boundary structurally enforced | ✅ Phase 8A — `test_module_does_not_import_orchestrator` |
| Documentation of L1 / L2 delivery split | ✅ Phase 8A — ADR-11a |
| Phase 7 `Watchdog.run_tick()` integration of `HeartbeatWriter.beat()` | ❌ DEFERRED — Phase 8B (backlog row #24) |
| systemd unit file scaffolding | ❌ DEFERRED — Phase 8B (backlog row #25) |
| Restart-rate ceiling + halt-and-notify | ❌ DEFERRED — Phase 8B |
| Phase 5 narrow-scope items (Script, Screen, Mermaid exporter, CLI) | ❌ still OPEN per `docs/phase55_consistency_patch.md` §2.12 |

**Phase 8B may begin.** The L2 *observation* API is wire-stable;
the action layer is free to consume `WatchdogStatus` without
worrying about future schema churn.

---

## 9. Files

```
watchdog/__init__.py                       25 lines  (new)
watchdog/heartbeat.py                     230 lines  (new)
watchdog/watchdog.py                      380 lines  (new)
automation/errors.py                      +28 lines  (HeartbeatError, ExternalWatchdogError)

scripts/phase8a_live_validation.py        ~210 lines (new throwaway harness)

tests/test_heartbeat.py                   ~240 lines (19 tests)
tests/test_external_watchdog.py           ~420 lines (40 tests)

ADR.md                                    +110 lines (ADR-11a + ADR-11 status note)
SYSTEM-ROADMAP.md                          ~22 lines rewritten (§5.7 / §5.7.1)
DESIGN-REVIEW.md                          +110 lines (§10a + backlog rows #24, #25)
docs/phase55_consistency_patch.md          ~30 lines updated (§2.12 partial resolution)

bench/results/phase8a_live_validation.json  (sidecar)
phase8a-report.md                         (this file)
```

---

## 10. Unresolved risks

### 10.1 No restart action — observation only

Phase 8A is the *observation* half. A STALE or INVALID
recommendation is data; nothing in v1.0 acts on it. An operator
running the L2 watchdog today must:
- Read the recommendation manually (or via a tiny shell loop).
- Take action (e.g., `systemctl --user restart automation`).

This is exactly the Phase 8B gap. Until Phase 8B, the framework's
hard-fault recovery story is: "L1 keeps it running through soft
faults; L2 tells you when L1 isn't enough; YOU restart it."

### 10.2 Heartbeat isn't auto-written by the Phase 7 Watchdog

`HeartbeatWriter` is a standalone utility. Phase 7's
`automation/watchdog.py` does not call `hb.beat(...)`. The
Phase 8A live validation calls it manually after `wd_l1.run_tick()`.
Wiring this into Phase 7 is one line; Phase 8A did not modify
`automation/watchdog.py` per the prompt's prohibitions. v1.1
backlog row #24.

### 10.3 No pid liveness check

The heartbeat carries `pid`, but the L2 watchdog does not verify
that the pid is still alive (`os.kill(pid, 0)`). A hung but
still-alive framework with a stale heartbeat is correctly
classified as STALE; a crashed framework whose pid has been
reused by another process would also be STALE (correct verdict
by accident — STALE → RESET_LITE → restart resolves both).
A future enhancement could distinguish "pid alive but hung" from
"pid dead" and recommend differently. v1.1 candidate.

### 10.4 Heartbeat path is conventional

`var/watchdog/heartbeat.json` is the default. Tests inject
arbitrary paths. A future Phase 8B systemd unit will need to
agree with the framework on the path. Config-driven path
selection (per ADR-13's TOML layer) is the v1.1 candidate.

### 10.5 Schema-version mismatch policy is "INVALID + RESET_HARD"

If a future framework writes `schema_version: 2` but the
operator hasn't upgraded the L2 watchdog, the watchdog returns
INVALID → RESET_HARD. The Phase 8B action layer would then
restart the framework (which would write schema_version 2 again,
which would be INVALID again …). This is a stable crash-loop
that the restart-rate ceiling (also Phase 8B) catches; for v1.0
Phase 8A, document the failure mode clearly and rely on operator
upgrade discipline.

### 10.6 No L2-side observability of itself

The L2 watchdog can be debugged via WATCHDOG_L2_DEBUG=1 (writes
metadata.json per check), but does not emit its own metrics or
JSONL logs the way the framework runtime does. For Phase 8A this
is fine — the L2 watchdog is so small (~400 LOC) that printf
debugging via the stdlib `logging` module suffices. Phase 8B may
add a small `var/run/watchdog-l2.jsonl` log if needed.

### 10.7 Live validation does not exhaustively cover MISSING

The Scenario 1 → 2 → 3 chain demonstrates HEALTHY → STALE →
INVALID. The MISSING verdict was exercised in the unit tests but
not in the live demo. The behavior is identical (read fails →
classify MISSING → recommend RESET_LITE) and structurally
deterministic; no live re-verification needed.

---

## 11. Readiness verdict

**Phase 8A: COMPLETE.**

- 576 / 576 tests pass; **94%** package coverage; Phase 8A
  modules at 91%–94% individually (both above the ≥ 90% bar).
- Live device validation: three reachable verdicts (HEALTHY,
  STALE, INVALID) all demonstrated against a real heartbeat
  produced by a Phase 7 supervised tick on the connected
  device. The fourth verdict (MISSING) is exercised by unit
  tests.
- L2 overhead: **median 0.030 ms per check** vs the < 1 ms NFR.
  Headroom 33×. The L2 supervisor is effectively free.
- All Phase 8A prompt prohibitions honoured: no daemon, no
  thread, no signal handler, no `kill -9`, no reboot, no
  systemd dependency, no runtime / telemetry redesign, no
  Phase 8B leakage. The runtime behaviour is byte-identical to
  Phase 7.
- ADR-11 reconciled via the additive ADR-11a; SYSTEM-ROADMAP
  §5.7 rewritten to reflect delivered reality; DESIGN-REVIEW
  Phase-8A discoveries catalogued; consistency-patch open item
  partially resolved.
- Acknowledged limitations: observation-only (no automatic
  restart); heartbeat not auto-wired into the Phase 7 Watchdog;
  no pid liveness check; schema-mismatch produces a stable
  crash-loop that Phase 8B's restart-rate ceiling will catch.

The Phase 8B implementer should next read this report's §7.3,
§10, and ADR-11a's "L2 action" row in the delivery table.
The wire-stable recommendation strings + heartbeat schema +
artifact format are the integration surface to consume.
