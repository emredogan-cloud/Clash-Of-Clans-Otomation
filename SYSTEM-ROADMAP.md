# System Roadmap & Engineering Design Dossier

> **Document type:** Master engineering design dossier
> **System:** Android UI Automation Framework (Python + OpenCV + ADB)
> **Reference architecture:** SENSE → THINK → ACT
> **Status:** Design phase — implementation has not begun
> **Companion documents:** [ADR.md](./ADR.md), [ARCHITECTURE-DIAGRAMS.md](./ARCHITECTURE-DIAGRAMS.md), [PHASE-MASTER-PROMPTS.md](./PHASE-MASTER-PROMPTS.md), [DESIGN-REVIEW.md](./DESIGN-REVIEW.md)

---

## 0. Document purpose

This dossier is the canonical engineering specification for the framework. It is written for an implementation team that has not been part of the design conversation. It is intentionally opinionated where opinions have been considered and intentionally agnostic where they have not. Every load-bearing decision is captured in an ADR; every uncertain estimate is labeled as such.

Three categories of statement appear throughout:

- **Verified fact** — known to be true from documentation or first-principles reasoning that does not depend on measurements we have not yet taken. Example: `adb exec-out` does not create a temp file on the device.
- **Engineering assumption** — a claim we believe is likely true and are willing to design against, but which has not been measured in the target environment. Example: "USB 2.0 latency overhead per round trip is ~5–20 ms."
- **Uncertain estimate** — a number whose magnitude we can reason about but whose precise value will only be known after Phase 0 measurement. Example: "End-to-end tick latency is 200–500 ms on a mid-tier host."

Wherever a number appears, it is annotated with one of these labels.

---

## 1. Executive summary

The framework automates interaction with the Android UI from a Linux desktop, using:

- **SENSE** — ADB-mediated screenshot capture (`adb exec-out screencap`, raw framebuffer, ADR-01/02);
- **THINK** — OpenCV normalized template matching with masks, multi-scale fallback, and a resolution-independent reference frame (ADR-03/04/05);
- **ACT** — `adb shell input` for taps/swipes/keys, with bounded jitter for robustness (ADR-06/15).

The design optimizes for **stability, observability, and long-run operability**, not peak FPS. Target tick rates and latencies are *tier-split* by FSM path on the operator's hardware (USB 2.0 host, Xiaomi 22095RA98C / Android 13):

- **Search-only ticks** (no HIT, no action, no validate): ~0.5–1 Hz, ~1.0–1.5 s median tick latency.
- **Validated ticks** (HIT → action → 1 validate cycle): ~0.4–0.5 Hz, ~2.0–2.2 s median tick latency.
- **Validated + retry ticks** (HIT → action → 1 validate fails → retry validate): ~0.3 Hz, ~2.5–3.0 s median tick latency.

These numbers were originally projected as 2–5 Hz / 200–500 ms but were revised down by Phase 0 measurements, then tier-split by Phase 5 measurements; see [docs/frozen_nfrs_v1.md](./docs/frozen_nfrs_v1.md) for the v1.0 frozen NFRs (amended 2026-05-21), [ADR-01a](./ADR.md#adr-01a--screenshot-pipeline-phase-0-reality-content-dependent-ordering-usb-link-speed-prerequisite) for the screenshot-pipeline reality check, and [ADR-08a](./ADR.md#adr-08a--validation-cost-consequence-of-the-fsm-design-phase-55) for the validation-cost consequence of the FSM design. The framework is a single Python process supervised by an external watchdog, with a formal state machine as the orchestrator and a recorded-trace replay harness as the primary integration-test surface.

The system is designed as a **generic UI automation framework**, with games and game-like interfaces as a reference use case. Use against any specific application is subject to that application's terms of service, which the framework cannot itself enforce and does not attempt to circumvent. Risk relating to ToS, account standing, and detection is discussed neutrally in §10 and is the operator's responsibility.

---

## 2. System context and scope

### 2.1 In scope (v1)

- Single Android device per host, connected over USB.
- Linux host (Debian/Ubuntu/Fedora family); no Windows/macOS support promised.
- Single-process Python framework, Python 3.11+.
- Image-based UI matching (templates) as the primary recognition strategy.
- ADB as the only device-side surface (no on-device companion app).
- Headless operation (no GUI for the framework itself).
- Local observability — logs, metrics file, on-disk artifacts.

### 2.2 Out of scope (v1)

- Multi-device parallelism (deferred; design preserves a single-tenant assumption).
- Network-attached observability (Prometheus scrape is *possible* via file but not part of the deployment manifest).
- Wireless ADB / ADB over network (deferred — adds reliability and security considerations).
- On-device companion app (Accessibility Service, instrumentation app).
- Reinforcement-learning or generative-model-driven decision making.
- Mobile-OS targets other than Android.
- A graphical operator console.

### 2.3 Glossary

| Term | Meaning |
|------|---------|
| **Tick** | One SENSE → THINK → ACT iteration. |
| **Template** | A reference image (with optional mask, metadata, thresholds) used by THINK to recognize a UI element. |
| **Reference resolution** | The single virtual resolution (1080×1920 portrait) into which all captures are remapped before THINK. |
| **Normalized coordinates** | A `(x, y)` pair where both components are in `[0, 1]`, independent of device pixels. |
| **Soft threshold / hard threshold** | Two-level confidence gating. A match above hard is decisive; above soft is acted on but logged as suspect. |
| **Trace** | A serialized recording of frames + actions used by the replay harness. |
| **Heartbeat** | A file the framework rewrites every N seconds; the watchdog uses it as a liveness signal. |

---

## 3. Non-functional requirements

Pre-Phase-0 targets were **engineering estimates**. After Phase 0
(see [phase-0-report.md](./phase-0-report.md)), several were revised;
the frozen v1.0 numbers live in [docs/frozen_nfrs_v1.md](./docs/frozen_nfrs_v1.md)
and that document is authoritative where this section differs. Tables
below preserve both the OLD (pre-Phase-0) and the NEW (v1.0 frozen)
columns so the historical reasoning is auditable.

### 3.1 Performance

> **Phase-5.5 amendment (2026-05-21):** the single-tier
> `tick_latency` row in the v1.0 frozen column was tier-split into
> three. The Phase-0 frozen value (≤ 1500 ms median / ≤ 2000 ms
> p95) was framed against an implicit *tick = SENSE + THINK + ACT*
> model. The Phase 5 orchestrator's tick also includes a validation
> cycle (`VALIDATING` state: full recapture + rematch). The
> validated tier is below; the full tier-split table is in
> [`docs/frozen_nfrs_v1.md` §1.1](./docs/frozen_nfrs_v1.md#11-frozen-targets).
> See [ADR-08a](./ADR.md#adr-08a--validation-cost-consequence-of-the-fsm-design-phase-55)
> for the rationale, and [phase5-report.md §4](./phase5-report.md)
> for the measurements.

| NFR | OLD (pre-Phase-0) | Phase-0 frozen (2026-05-20) | Phase-5.5 frozen (2026-05-21) | Evaluation |
|-----|-------------------|-----------------------------|-------------------------------|-----------|
| Tick latency, search-only (median) | ≤ 500 ms (single tier) | ≤ 1500 ms (single tier) | **≤ 1500 ms** (search-only) | Phase 5 Demo 1 measured 1211 ms ✓ |
| Tick latency, search-only (p95) | ≤ 900 ms (single tier) | ≤ 2000 ms (single tier) | **≤ 1800 ms** (search-only) | Phase 7 soak |
| Tick latency, validated, no retry (median) | (not split) | (not split) | **≤ 2200 ms** | arithmetic; Phase 5 Demo 3 used retry, see Phase 7 soak |
| Tick latency, validated + retry (median) | (not split) | (not split) | **≤ 3000 ms** | Phase 5 Demo 2 measured 2956 ms, Demo 3 measured 2584 ms ✓ |
| Tick latency, validated + retry (p95) | (not split) | (not split) | **≤ 3300 ms** | Phase 7 soak |
| Screenshot capture (median) | ≤ 250 ms | **≤ 1000 ms (raw)** / ≤ 1500 ms across modes | unchanged | Phase 0 ✓ |
| Per-template match cost (median) | ≤ 25 ms (full screen) | **tier-split**: ≤ 5 ms (ROI gray), ≤ 10 ms (ROI BGR), ≤ 50 ms (full-frame gray); full-frame BGR opt-in only | unchanged | Phase 0 ✓ + Phase 3 ✓ |
| Sustained tick rate, search-only (default) | 2–5 Hz | 0.5–1 Hz (single tier) | **0.5–1 Hz** (search-only) | Phase 0 floor; Phase 7 soak |
| Sustained tick rate, validated (default) | (not split) | (not split) | **0.3–0.5 Hz** | composition of validated-tick latency tier |
| Concurrent template matches per tick | ≤ 8 default, 20 cap | ≤ 8 default (ROI-required), 20 cap (opt-in) | unchanged | configuration |
| **USB link speed at bootstrap (new)** | — | **≥ 480 Mbps** | unchanged | Phase 1 bootstrap ✓ |

> **Why the regression vs OLD**: USB transport floor + device-side
> `screencap` composition cost dominate. See
> [phase-0-report.md §3](./phase-0-report.md) and
> [ADR-01a](./ADR.md#adr-01a--screenshot-pipeline-phase-0-reality-content-dependent-ordering-usb-link-speed-prerequisite).
>
> **Why the tier-split vs Phase-0 frozen**: the Phase-0 frozen
> tick-latency NFR was framed for "tick = SENSE + THINK + ACT".
> Phase 5 implemented the actual `Orchestrator.tick()`, which adds
> a validation cycle (`VALIDATING` state, full recapture + rematch
> per ADR-08 + ADR-08a). Each validation cycle adds ~990 ms on
> this hardware; the retry adds another ~990 ms. The tier-split
> reflects measurement, not an NFR loosening.

### 3.2 Resource usage

| NFR | Target | Notes |
|-----|--------|-------|
| RAM (steady state) | ≤ 300 MB | Engineering assumption. Frames are short-lived; manifest in memory. Native raw frame is 10.4 MB on the operator's 1080×2408 device; resampled to reference (1080×1920) is 6.2 MB. |
| RAM (artifact spike) | ≤ 600 MB | When writing debug artifacts. |
| CPU (single core, steady state) | ≤ 30% (with ROI discipline); ≤ 60% if full-frame templates are present | Mid-tier host. ROI discipline mandatory per ADR-03 Phase-0.5 clarification; see [docs/frozen_nfrs_v1.md §2](./docs/frozen_nfrs_v1.md). |
| Disk write (logs + metrics) | ≤ 50 MB / day | Logs at default verbosity. |
| Disk write (artifacts under load) | ≤ 500 MB / day, rotation-capped | Hard cap by rotator. |

### 3.3 Reliability

| NFR | Target | Notes |
|-----|--------|-------|
| Mean time between unrecovered faults | ≥ 24 h | Uncertain estimate. Validate Phase 8. |
| Mean time to recovery (soft fault) | ≤ 10 s | RESET_LITE budget. |
| Mean time to recovery (hard fault) | ≤ 60 s | RESET_HARD budget incl. ADB restart. |
| Watchdog restart bound | ≤ 5 restarts / hour | Above this, halt + notify. |
| Heartbeat staleness threshold | 30 s | Configurable. |

### 3.4 Maintainability

| NFR | Target | Notes |
|-----|--------|-------|
| New template authored to integrated | ≤ 15 min | Asset workflow + reload via restart. |
| New state added to FSM | ≤ 1 file change | Hand-rolled FSM (ADR-08). |
| Adding a new action class | ≤ 1 file change | ACT engine interface. |
| Replacing the screenshot backend | ≤ 1 module change | Behind `Sensor` interface. |
| Replacing the matcher implementation | ≤ 1 module change | Behind `Matcher` interface. |

### 3.5 Observability

| NFR | Target | Notes |
|-----|--------|-------|
| Every state transition | logged | structured JSON |
| Every match | logged (debug level) | with score + duration |
| Every failed match below soft threshold | artifact written | rotation-capped |
| Every action | logged | with normalized + denormalized coords |
| Heartbeat | written ≤ every 5 s | unconditional |
| Metrics file | rewritten ≤ every 10 s | Prometheus text |

### 3.6 Portability

| NFR | Target | Notes |
|-----|--------|-------|
| Linux distributions supported | Debian 12+, Ubuntu 22.04+, Fedora 39+ | Phase 1 verifies. |
| Python versions | 3.11, 3.12 | 3.13 once libraries catch up. |
| ADB minimum version | platform-tools 34.0+ | older versions not supported. |
| Android versions targeted | 10–14 (API 29–34) | older versions best-effort. |

### 3.7 Extensibility

The framework exposes these interfaces as stable extension points:

- `Sensor` (frame capture)
- `Matcher` (image recognition)
- `Actuator` (input delivery)
- `RecoveryHandler` (state-machine recovery action)
- `Observer` (a sink for events: logs, metrics, custom)

Adding a new implementation behind any of these interfaces is expected to be a single-file change.

---

## 4. Reference architecture

The framework is the SENSE → THINK → ACT canonical loop, plus four cross-cutting subsystems: orchestration, observability, recovery, and asset management. See [ARCHITECTURE-DIAGRAMS.md §1](./ARCHITECTURE-DIAGRAMS.md#1-component-diagram--subsystem-boundaries) for the structural view.

The loop, briefly:

1. The orchestrator (a finite-state machine, ADR-08) decides what *kind* of work to do this tick — observe, act, validate, recover.
2. SENSE captures a frame from the device via ADB (ADR-01/02).
3. THINK runs the active set of templates against the frame (ADR-03), producing match results.
4. The orchestrator updates state based on results.
5. ACT issues an ADB input if the new state demands one (ADR-06).
6. Observability events are emitted throughout.
7. The heartbeat is touched at the end of the tick (ADR-11).

A tick is the unit of work. Multiple ticks may run per second in `OBSERVING`/`MATCHING`; ticks may be widely spaced in `WAITING` (e.g. waiting for a 30-second cooldown).

---

## 5. Subsystem deep-dives

### 5.1 Device communication (ADB)

#### 5.1.1 Mechanism

ADB exposes a client–server architecture: the `adb` binary on the host is both client and (transparently) launches a server daemon on first use. The server multiplexes connections from clients to devices over USB (or TCP). For our purposes:

- We always talk to the *adb client*, never USB directly.
- We use only stable, documented ADB commands: `devices`, `shell`, `exec-out`, `pull`, `push`, `forward`, `kill-server`, `start-server`, `wait-for-device`.
- The framework runs `adb` as a subprocess; output is captured on `stdout`, status on exit code, errors on `stderr`.

#### 5.1.2 Engineering implications

- ADB subprocess spawn cost is **~30–80 ms across hardware**
  (engineering range). The operator's host measured **28 ms median**
  (`adb shell echo hi`, 200 iter, USB 480 Mbps; VF). Operators on
  slower hosts will sit higher in the range. See
  [phase-0-report.md §5](./phase-0-report.md).
- The adb server is a *separate* OS process and a long-lived daemon. It is not part of our framework but its lifecycle affects us — restart of the host or `adb kill-server` will require the framework to detect and recover.
- USB 2.0 high-speed delivers **~260 Mbps practical** on the operator's
  host (10 MB blob via `adb pull`; VF). On a 1080×2408 device the raw
  framebuffer is **10.4 MB**; at 260 Mbps that's a **~324 ms USB
  transport floor**. Add ~600 ms of device-side `screencap`
  composition cost (inferred from `exec-out screencap` median minus
  the transport floor) and the raw screencap mode lands at ~947 ms
  median (VF). USB 3.0 would bring the transport floor below 100 ms
  but the operator's device does not expose a USB 3.x port. **Phase 0
  determined this USB tier and the resulting bench numbers; see
  [ADR-01a](./ADR.md#adr-01a--screenshot-pipeline-phase-0-reality-content-dependent-ordering-usb-link-speed-prerequisite).**

#### 5.1.3 Limitations

- ADB authorization is per-host-keypair. A reformat of the Linux host invalidates the device's stored authorization; the user must re-confirm on the device.
- USB-debugging stays on through reboots, but USB-debug *authorization* can revoke if the operator clears it.
- Over USB, the device must be unlocked at the moment of first authorization. Subsequent sessions do not require unlock for ADB itself, but the screen-state requirement of automated tasks usually does.

#### 5.1.4 Failure modes

| Failure | Symptom | Mitigation |
|---------|---------|-----------|
| Device unauthorized | `adb devices` shows `unauthorized` | Surface clear error; framework refuses to start. |
| USB cable failure | Sporadic ADB command failures | ADB error counter → RESET_HARD. |
| adb server crash | All ADB commands fail | Detected at command level; RESET_HARD bounces server. |
| Device sleep | Capture returns black frame | Maintain `screen on` (see §5.1.5). |
| Phone reboot | Device disappears from `adb devices` | `wait-for-device` loop in CONNECTING. |
| Host suspend | adb server may need restart | Watchdog restarts the framework after host wake; CONNECTING handles. |

#### 5.1.5 Device-sleep prevention

The operating system aggressively sleeps the screen and CPU. The framework must keep the screen state active for SENSE to capture meaningful frames. Options:

- `adb shell svc power stayon usb` — keeps screen on while plugged in. *Recommended.*
- `adb shell input keyevent KEYCODE_WAKEUP` — periodic poke; lower power but jittery.
- Persistent foreground app — out of scope (requires on-device app).

We choose `svc power stayon usb` as the baseline because it is the simplest and most reliable; it requires no on-device code and reverts to default on disconnect.

#### 5.1.6 ADB recovery

`RESET_HARD` (see §11 state machine) executes:

1. `adb kill-server`
2. `adb start-server`
3. `adb wait-for-device` (bounded timeout, 30 s)
4. Re-fingerprint device (resolution, orientation)
5. Reissue `svc power stayon usb`

This sequence is sufficient for the common failure modes we expect.

#### 5.1.7 USB link-speed validation (Phase 0.5 addition)

A USB hub between the host and the device can silently negotiate the
link down to USB 1.1 full-speed (12 Mbps), reducing effective
screencap throughput by ~40×. Phase 0 observed this on the operator's
hardware before the cable was replugged into a USB 2.0 high-speed
port. Without an explicit check, an operator could waste hours
diagnosing what looks like a framework regression.

Phase 1's `bootstrap.sh` SHALL therefore:

1. After `adb devices` confirms a connected device, resolve the
   device's USB sysfs path by walking `/sys/bus/usb/devices/*` and
   matching the `serial` attribute.
2. Read `/sys/bus/usb/devices/<path>/speed`.
3. Accept `480` (USB 2.0 HS) or any of `5000` / `10000` / `20000` (USB
   3.x SuperSpeed). Log at INFO and proceed.
4. On `12` (USB 1.1 FS) or `1.5` (low-speed): WARN with remediation
   ("device is plugged through a full-speed hub; replug directly into
   a USB 2.0 high-speed port") and exit non-zero.
5. On sysfs path not resolvable: WARN ("cannot verify USB link speed")
   and proceed.

The link-speed NFR is frozen at ≥ 480 Mbps. See
[docs/frozen_nfrs_v1.md §5](./docs/frozen_nfrs_v1.md) and
[ADR-01a §Decision (5)](./ADR.md#adr-01a--screenshot-pipeline-phase-0-reality-content-dependent-ordering-usb-link-speed-prerequisite).

### 5.2 Screenshot pipeline (SENSE)

Detailed in [ADR-01](./ADR.md#adr-01--screenshot-pipeline-adb-exec-out-screencap-raw-as-primary), [ADR-01a](./ADR.md#adr-01a--screenshot-pipeline-phase-0-reality-content-dependent-ordering-usb-link-speed-prerequisite), and the SENSE pipeline diagram in [§7 of ARCHITECTURE-DIAGRAMS.md](./ARCHITECTURE-DIAGRAMS.md#7-subsystem-internal--sense-pipeline-detail). The key points:

- **Primary mode:** `adb exec-out screencap` raw framebuffer. Parse header → read pixels → RGBA→BGR. Default. Latency is content-deterministic (~947 ms median on the operator's hardware; VF).
- **Configurable modes:** `sensor.mode = "raw" | "png" | "pull" | "auto"` (ADR-01a §Decision). PNG is recommended for low-entropy target UIs.
- **Fallback policy:** if the raw header parser fails (unknown format), the framework switches to PNG for the rest of the session and emits a metric.
- **Format detection** runs on the first capture per session; the chosen mode is sticky for the session unless `auto` switches based on A/B sampling.
- **Resampling** to reference resolution (ADR-04) happens inside SENSE so downstream sees a uniform frame size.

**Phase 0 benchmark outcomes** (see [phase-0-report.md §3](./phase-0-report.md)):

- Raw vs PNG median latency over 200 captures: PNG-vs-raw ordering **reverses with screen content**. Raw is content-deterministic at ~947 ms median; PNG ranges from 578 ms (low-entropy) to 1311 ms (high-entropy). See ADR-01a.
- USB 2.0 vs USB 3.0 transport floor: USB 2.0 measured at ~260 Mbps practical; USB 3.0 not benchmarked because the operator's device does not expose a USB 3.x port (documented limitation).
- RGBA→BGR conversion: not directly microbenched in Phase 0 (the bench uses indexing via `cv2.cvtColor(RGBA → BGR)`). Phase 2 will compare `cv2.cvtColor` against `frame[:, :, [2, 1, 0]]` indexing.
- `cv2.resize` interpolation: not directly microbenched; Phase 2 selects `INTER_AREA` (downsample) or `INTER_LINEAR` (upsample) per ADR-04.

These results informed the v1.0 NFR freeze; see [docs/frozen_nfrs_v1.md](./docs/frozen_nfrs_v1.md).

#### 5.2.1 Maintenance burden

- **Low.** The pipeline depends on `adb` and `cv2` only. The raw-header parser is a small, isolated module covered by fixtures.

#### 5.2.2 Failure modes

| Failure | Detection | Mitigation |
|---------|-----------|-----------|
| Capture timeout | Subprocess exceeds 2 s | Increment capture-error counter |
| Empty frame | bytes_read = 0 | Counter |
| Header parse failure | format byte unknown | Auto-fallback to PNG mode |
| Wrong dimensions | (W, H) ≠ fingerprint | Re-fingerprint (re-enter CALIBRATING) |
| Corruption | partial bytes | Counter; discard frame |

### 5.3 Vision engine (THINK)

Detailed in [ADR-03/04/05](./ADR.md#adr-03--primary-cv-strategy-normalized-template-matching-with-masks) and the THINK pipeline diagram in [§8 of ARCHITECTURE-DIAGRAMS.md](./ARCHITECTURE-DIAGRAMS.md#8-subsystem-internal--think-pipeline-detail).

#### 5.3.1 Core abstraction

```
Matcher.match(frame: Frame, templates: List[Template]) -> List[MatchResult]
```

`Template` carries its own ID, image, mask, ROI hint, color mode, thresholds, and capture metadata. `MatchResult` is `(template_id, status ∈ {HIT, SOFT, MISS}, score, location, duration_ms)`.

The orchestrator passes *only the active subset* of templates per tick — templates the state machine deems relevant in the current state. This is the most important performance lever in THINK: matching 4 templates per tick is 5× faster than matching 20.

#### 5.3.2 Preprocessing

- Frame is converted to grayscale once per tick and cached. Templates declaring grayscale mode use the cached gray frame.
- ROIs are cropped on demand and cached for the tick.
- No edge-detection preprocessing in v1. Edge-based matching is in scope only as a future Matcher implementation if template matching proves insufficient for a specific UI class.

#### 5.3.3 Match algorithm

- `cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED, mask=mask)` is the workhorse.
- Result is a correlation map; we take the maximum location and its score.
- Score is in `[-1, 1]`; we use `[0, 1]` as the effective range (negative scores are MISS).
- Per-template `hard` and `soft` thresholds gate the result.

#### 5.3.4 Multi-scale fallback

If single-scale matching MISSes for a template flagged `multi_scale: true`, retry at three scales (0.9x, 1.0x, 1.1x of the template) and take the best. Costs ~3× per template; reserved for templates known to be scale-sensitive.

#### 5.3.5 Animation tolerance

Templates over animated regions (pulsing buttons, glow effects) are masked. The mask covers the animated pixels; only the stable region contributes to the score. This is more robust than threshold inflation, which would dilute discrimination across the entire template.

#### 5.3.6 Limitations

- Template matching is **not** scale-invariant beyond the multi-scale fallback's narrow range. UIs that scale arbitrarily (zoom gestures, dynamic layouts) require a different strategy.
- Template matching is **not** rotation-invariant. Rotated UI elements need feature matching.
- Template matching is **brittle** under aggressive theme changes (dark/light mode, accessibility themes). Per-theme templates are required, or the operator must lock the theme.
- Template matching produces *one* peak per template per call. For "find all instances of X on screen" you must iteratively suppress detected peaks and rematch (non-maximum suppression). This is in scope as a utility, not the default.

#### 5.3.7 Maintenance burden

- **Medium.** Templates rot as the target app updates. Soft-match telemetry (ADR-12) is the early-warning system; a sustained spike of soft matches is a template-update signal.

### 5.4 Action engine (ACT)

Detailed in [ADR-06](./ADR.md#adr-06--input-injection-adb-shell-input-with-optional-minitouch-escalation) and [ADR-15](./ADR.md#adr-15--anti-fragility-bounded-behavioral-jitter-opt-in-per-action-class), and the ACT pipeline diagram in [§9 of ARCHITECTURE-DIAGRAMS.md](./ARCHITECTURE-DIAGRAMS.md#9-subsystem-internal--act-pipeline-detail).

#### 5.4.1 Action classes

| Class | ADB form | Latency envelope (engineering est.) |
|-------|----------|------------------------------------|
| `tap` | `input tap X Y` | 80–250 ms |
| `swipe` | `input swipe X1 Y1 X2 Y2 dur_ms` | 80–500 ms inc. swipe duration |
| `long_press` | `input touchscreen swipe X Y X Y dur_ms` | dur + 80 ms |
| `key` | `input keyevent KEYCODE` | 80–200 ms |
| `text` | `input text "string"` | variable |

#### 5.4.2 Jitter envelope (ADR-15)

Each action class declares:
- `pre_delay_ms` range (default 50–150 ms)
- `coord_dispersion_norm` σ (default 0.005 of screen — ~5 px on 1080)
- `swipe_duration_ms` range (default depends on class)
- `post_delay_ms` range (default 100–300 ms)

These can be tightened per call when precision matters or widened for "broad area" interactions.

#### 5.4.3 Action validation

Every action that is expected to change the visible state has an associated *expected-state template*. The orchestrator transitions to `VALIDATING` after `ACTING` and confirms within 2 s that the expected template matches. If not, the action is considered failed and the recovery cascade begins.

#### 5.4.4 Stuck-state recovery

If validation fails three times in a row for the same action, the state machine escalates to `RESET_LITE`: a back-button press or a tap at a "safe area" (a region known to dismiss most modals). If that does not return the device to a known state, escalate to `RESET_HARD`.

#### 5.4.5 Failure modes

| Failure | Detection | Mitigation |
|---------|-----------|-----------|
| Tap landed outside hit target | validation MISSes | retry with reduced jitter |
| App not responsive | validation timeout | RESET_LITE |
| Modal blocking expected UI | unexpected template HITs | dismiss-modal handler |
| ADB input subprocess slow | latency > 1 s | log + counter |

### 5.5 State machine (orchestrator)

Detailed in [ADR-08](./ADR.md#adr-08--state-machine-hand-rolled-finite-state-engine-no-framework) and the state diagram in [§4 of ARCHITECTURE-DIAGRAMS.md](./ARCHITECTURE-DIAGRAMS.md#4-formal-state-diagram--orchestrator-state-machine). Formal specification in §11 of this document.

The state machine is the *one* place where domain logic lives. Everything else is a mechanism. This separation is deliberate:

- SENSE knows how to capture a frame; it does not know which frames matter.
- THINK knows how to match a template; it does not know which templates to match.
- ACT knows how to issue an input; it does not know which input to issue.
- The state machine decides all of the above.

A typical interaction script is *not* a sequence of Python calls; it is a set of state declarations consumed by the state machine.

> **Phase-5.5 note (2026-05-21):** the `VALIDATING` state is, by
> design, a full recapture + rematch cycle (no cheaper validation
> is available in v1.0). This roughly doubles per-tick capture
> cost vs a search-only tick, and with the single validation
> retry can triple it. See [ADR-08a](./ADR.md#adr-08a--validation-cost-consequence-of-the-fsm-design-phase-55)
> for the architectural rationale and the candidate cheaper-
> validation strategies (deferred to v1.1+); see
> [`docs/frozen_nfrs_v1.md` §1.1](./docs/frozen_nfrs_v1.md#11-frozen-targets)
> for the tier-split tick-latency NFR that this implies.

### 5.6 Observability

Detailed in [ADR-12](./ADR.md#adr-12--observability-structured-json-logs--metrics-file--artifact-store).

#### 5.6.1 Logs

- Format: one JSON object per line (JSONL).
- Fields (mandatory): `ts` (ISO 8601, UTC), `level`, `event`, `tick_id`, `state`, `correlation_id`. Event-specific fields beyond that.
- Levels: TRACE, DEBUG, INFO, WARN, ERROR. Default INFO.
- Rotation: 50 MB per file, 7 days retained.

#### 5.6.2 Metrics

- File: `var/metrics/metrics.prom`, Prometheus text exposition format.
- Updated every 10 s (configurable).
- Key metrics:
  - `tick_total{state}` — counter
  - `tick_duration_seconds_bucket{state,...}` — histogram (Phase 6 produces the bucket layout)
  - `screen_capture_duration_seconds_bucket{mode}` — histogram
  - `template_match_score{template_id}` — gauge (last score)
  - `template_match_duration_seconds_bucket{template_id}` — histogram
  - `template_match_total{template_id,outcome}` — counter
  - `action_total{action_class,outcome}` — counter
  - `action_duration_seconds_bucket{action_class}` — histogram
  - `adb_error_total{kind}` — counter
  - `recovery_total{kind}` — counter
  - `watchdog_restart_total` — counter (exposed by watchdog separately)
  - `heartbeat_last_age_seconds` — gauge

#### 5.6.3 Artifacts

- Location: `var/artifacts/`.
- Triggers: matches below soft threshold (configurable), validation failures, RESET_LITE/HARD entries, FAULTED.
- Contents per event: original frame as PNG, side-by-side debug image (frame with ROI overlays and match score annotations), template that was being sought.
- Rotation: max 500 events retained, 500 MB cap, whichever comes first.

#### 5.6.4 Replay debugging

A `replay <tick_id>` CLI mode reconstructs a tick from logs + artifacts and re-runs THINK against the saved frame. This is the primary debugging surface; it does not require the device.

### 5.7 Recovery & watchdog

Detailed in [ADR-11](./ADR.md#adr-11--recovery--watchdog-external-process-not-in-process-supervisor) and [ADR-11a](./ADR.md#adr-11a--l1l2-supervision-split-phase-8a); recovery flow in [§5 of ARCHITECTURE-DIAGRAMS.md](./ARCHITECTURE-DIAGRAMS.md#5-recovery-flow--what-happens-when-something-goes-wrong).

Two layers, delivered across two phases:

1. **L1 — In-process supervision + recovery** (Phase 7, shipped). The `Watchdog` class in `automation/watchdog.py` wraps each orchestrator tick: catches exceptions, post-hoc-flags timeouts, composes a `RuntimeHealth` snapshot. The `RecoveryManager` in `automation/recovery.py` performs one-shot best-effort recovery (force orchestrator FSM back to IDLE via the `_transition` chokepoint; re-check ADB device state). The full `RESET_LITE` / `RESET_HARD` / `RECONNECTING` recovery cascade described in §11.1 is a Phase 8B candidate; v1.0 Phase 7 implements only the L1 essentials.
2. **L2 — External watchdog**, delivered in two parts:
   - **L2 observation** (Phase 8A, shipped). The `ExternalWatchdog` class in `watchdog/watchdog.py` runs outside the framework process. It reads `var/watchdog/heartbeat.json` written by `watchdog/heartbeat.py:HeartbeatWriter`, classifies freshness (HEALTHY / STALE / MISSING / INVALID), and returns a `WatchdogStatus` carrying an escalation *recommendation* (`none` / `RESET_LITE` / `RESET_HARD`). Stdlib-only; no imports from `automation/*`. The recommendation is **data only** — Phase 8A does not signal, kill, or restart anything.
   - **L2 action** (Phase 8B, deferred). A small (~50 LOC) Python script (or systemd unit) that consumes the L2 recommendation and translates it into the action: `SIGTERM → wait → SIGKILL → restart`; restart-rate ceiling (halt + notify above 5 restarts/hour). Implementation is the operator-facing supervision substrate (systemd `--user`, supervisord, container restart policy — substrate-independent because the L2 observer only emits data).

#### 5.7.1 Watchdog as systemd user unit (Phase 8B)

The framework will be delivered as two units (Phase 8B work):

- `automation.service` — the framework.
- `automation-watchdog.service` — invokes `ExternalWatchdog.check()` on a schedule (timer or short poll loop) and acts on the recommendation.

`automation.service` does *not* declare `Restart=on-failure` because that would race the watchdog. The watchdog owns restart.

### 5.8 Configuration & asset management

Configuration — detailed in [ADR-13](./ADR.md#adr-13--configuration-layered-toml--environment-overrides-no-runtime-mutation). Asset management — detailed in [ADR-10](./ADR.md#adr-10--asset--template-library-content-addressed-on-disk-store-with-manifest).

Configuration layers (lowest to highest precedence):

1. Built-in defaults (in code, immutable).
2. Repo-shipped `config/runtime.toml`.
3. User-local `~/.config/automation/runtime.toml`.
4. Environment variable overrides for declared keys (e.g. `AUTOMATION_LOG_LEVEL`).

The resolved configuration is serialized at startup to `var/run/effective-config.toml` for postmortem reference.

Assets live in `assets/templates/`. Each template is `{name}.png` accompanied by `{name}.toml`:

```toml
id = "play_button"
captured_on = "2026-05-13"
captured_device = "Pixel 7 (Pixel)"
captured_resolution = [1080, 2400]
match_strategy = "grayscale"  # one of: bgr, grayscale, channel-{0,1,2}
roi_norm = [0.35, 0.78, 0.65, 0.92]  # optional, [x0, y0, x1, y1] normalized
mask = "play_button.mask.png"  # optional
multi_scale = false
hard_threshold = 0.92
soft_threshold = 0.86
```

The manifest loader validates every metadata file against its PNG (dimensions, channel count) at startup.

---

## 6. Setup & installation

### 6.1 Phone ↔ Linux connection

#### 6.1.1 On the Android device

1. Enable **Developer options**: Settings → About phone → tap **Build number** 7 times.
2. Enable **USB debugging**: Settings → System → Developer options → **USB debugging** → On.
3. Plug the device into the Linux host using a **data-capable** USB cable. (Many "charging" cables omit data lines; the device will not appear in `adb devices`.)
4. On first connection, the device prompts to authorize the host's RSA key. Tap **Allow** and check *Always allow from this computer*.

#### 6.1.2 On the Linux host

```
sudo apt install android-tools-adb    # Debian/Ubuntu
sudo dnf install android-tools         # Fedora
```

Or download `platform-tools` from `developer.android.com` and put `adb` on `PATH`. Minimum version: **platform-tools 34.0** (ADR-16).

#### 6.1.3 Udev rules

Without udev rules, ADB requires root because the USB device node defaults to root-owned. The fix is one file:

```
# /etc/udev/rules.d/51-android.rules
SUBSYSTEM=="usb", ATTR{idVendor}=="<vendor-hex>", MODE="0666", GROUP="plugdev"
```

The vendor hex varies by manufacturer (`18d1` Google, `04e8` Samsung, etc.). A catch-all approach is to install the community-maintained `android-udev` package where available. The user is added to `plugdev` and reauthenticates.

#### 6.1.4 Verification

```
adb devices
```

Expected output:

```
List of devices attached
XXXXXXXX        device
```

If the device shows `unauthorized`, the authorization dialog has not been accepted. If it shows `no permissions`, udev rules are missing.

#### 6.1.5 Failure scenarios and remedies

| Symptom | Likely cause | Remedy |
|---------|-------------|--------|
| `adb` not found | platform-tools not installed | install platform-tools |
| no devices listed | cable, USB-debug off, or daemon not running | check cable, settings, `adb start-server` |
| device shows `unauthorized` | authorization not granted | reconnect, accept dialog |
| device shows `no permissions` | udev rule missing | install rule, re-plug |
| sporadic disconnects | low-quality cable, USB power management | replace cable, disable USB autosuspend |

### 6.2 Linux desktop dependencies

| Dependency | Reason | Minimum |
|------------|--------|---------|
| `adb` (android-tools / platform-tools) | device communication | 34.0 |
| Python | runtime | 3.11 |
| `python3-venv` (or equivalent) | environment isolation | matches Python |
| `libstdc++6` | OpenCV wheel dep | distro default |
| `libgl1` *(if needed by wheel)* | OpenCV headless still imports a small bit of GL on some distros | distro default |
| `systemd --user` | watchdog + framework supervision | distro default |

The bootstrap script (§6.4) verifies each of these at first run and fails loudly if a dep is missing.

### 6.3 Python environment

- Python 3.11+ (ADR-16).
- Project virtualenv at `./.venv/`.
- `uv` is the recommended dependency manager (`pip-tools` + `pip` is acceptable). Lockfile is `uv.lock` (or `requirements.txt` from `pip-tools`).
- Dependency set (initial, illustrative — full pin set in Phase 1):
  - `opencv-python-headless` (CV)
  - `numpy` (CV substrate)
  - `tomli` / stdlib `tomllib` (config)
  - `prometheus_client` (optional, for metrics format helpers)
  - `pytest` + `pytest-asyncio` (test)
- **No** dependency on PyTorch, TensorFlow, Pillow (OpenCV covers it), `requests` (no network in v1).

### 6.4 Bootstrap and reproducibility

The bootstrap script (`scripts/bootstrap.sh`) is idempotent and:

1. Verifies Python version.
2. Verifies `adb` version.
3. Creates `./.venv/` if absent.
4. Installs locked dependencies into the venv.
5. Creates `var/`, `var/log/`, `var/metrics/`, `var/artifacts/`, `var/run/` directories.
6. Verifies template manifest by loading it (refuses to proceed on validation failure).
7. Optionally installs systemd user units if `--install-service` is passed.
8. Optionally runs `adb devices` and reports.

Bootstrap completion is a precondition for running the framework. The framework refuses to start if `.venv/` is missing or the manifest fails to load.

---

## 7. Estimated costs

### 7.1 Hardware

The framework runs on commodity Linux desktop hardware. No GPU is required. A USB cable and an Android phone are the only device-side requirements.

- **Host CPU:** any x86_64 ≥ 4 cores from the last 10 years is sufficient. Engineering assumption: 30% of one core at steady state.
- **Host RAM:** 4 GB free is comfortable; the framework's working set is ≤ 300 MB (§3.2).
- **Host disk:** 1 GB for the framework + venv + assets; another 1 GB for logs/metrics/artifacts (rotation-capped).
- **Phone:** any Android 10–14 device. Battery wear is a real factor over months of always-plugged operation (lithium-ion cells degrade at high state-of-charge; many phones now self-limit to 80% when plugged-in mode is detected, mitigating this).
- **Cable:** data-capable, ideally USB 3.0 if the phone supports it (halves USB-transport latency for screencap).

### 7.2 Power

A mid-tier Linux desktop running the framework draws on the order of 30–80 W (engineering assumption). At 24/7 it consumes ~260–700 kWh/year. The framework itself accounts for a small fraction of this; the host's idle draw dominates. The phone draws ~3–8 W on its own and the framework adds little to that.

### 7.3 Maintenance burden

Honest estimate of recurring effort once the framework is in stable operation:

- **Template upkeep:** ~1 hour per month, scaling with the rate of UI changes in the target app. Soft-match telemetry tells you when to spend it.
- **Dependency updates:** ~30 minutes per quarter (lockfile refresh + test pass).
- **Operational incident response:** unknowable in advance; depends on the operator's tolerance for downtime. With the watchdog in place, the framework recovers from most known faults without intervention.

### 7.4 Operational cost

Near zero in cash terms — no managed services, no cloud, no licensed software. The cost is the operator's time and the host's electricity. If observability is ever scraped to a remote Prometheus, that adds the cost of running Prometheus, which is also nearly free at the scale of a single source.

---

## 8. Expected accuracy

These are **engineering estimates**. Phase 0 produces measured numbers against the operator's specific device and content.

| Scenario | Detection rate (engineering est.) | Notes |
|----------|----------------------------------|-------|
| Static, well-isolated UI element, reference resolution | 99–99.9% | template + ROI + grayscale |
| Static, full-frame search, no ROI | 98–99.5% | full-frame is slower, slightly less robust |
| Animated element (pulse/glow), masked correctly | 95–99% | mask quality determines the gap |
| Resolution mismatch within remap tolerance | 95–99% | resampling artifacts reduce precision |
| Lighting/theme variant (dark vs. light mode) | 50–95% | requires per-theme templates |
| Rotated UI | <50% with template matching | feature matching required |
| OCR-able text (using `Tesseract` add-on) | 90–98% per region | font, antialiasing, region size |
| Tap landed on intended target | 99–99.9% | jitter envelope is well within hit-target margins |

**False positives** (matching the wrong thing) — the hard threshold is set so that false-positive rate is below 1% in the configurations above. Mistuned thresholds are the most common cause of high false-positive rates; the soft/hard split makes mistuning observable.

**False negatives** (missing a real element) — dominate failure modes. Most are caught by validation + retry. Sustained false negatives are a template-rot signal.

The numbers above are **not guarantees**. They are calibration targets for Phase 0 measurement. The framework will not claim a detection rate it has not measured against the operator's device and content.

---

## 9. Risk register

Risks are scored on **likelihood** (L: Low, M: Medium, H: High) and **severity** (S: Low, M: Medium, H: High). Combined into a coarse priority (P1 highest).

| ID | Risk | L | S | P | Mitigation |
|----|------|---|---|---|-----------|
| R-01 | Target app UI updates break templates en masse | H | H | P1 | Soft-match telemetry; template regeneration workflow; loose decoupling of state machine from templates. |
| R-02 | ADB version skew between host and device | M | M | P2 | Bootstrap verifies version; documented minimum. |
| R-03 | USB cable degradation causes silent capture corruption | M | H | P1 | Capture-error telemetry; explicit "bad-cable" diagnostic. |
| R-04 | Host suspends, framework reawakens into stale state | M | M | P2 | RECONNECTING on first ADB error post-wake; watchdog catches the rest. |
| R-05 | OpenCV API drift between minor versions | L | M | P3 | Lockfile + Phase 6 regression suite. |
| R-06 | Template threshold drift due to subtle rendering changes | M | M | P2 | Soft-match telemetry; periodic threshold recalibration. |
| R-07 | False positive triggers a destructive action | L | H | P1 | Hard-threshold gating; explicit "destructive action" class requires double-confirmation template. |
| R-08 | Long-run memory leak | L | H | P1 | Memory metric + soft-restart policy; integration test exercises 24 h soak. |
| R-09 | Account ban or ToS violation by operating against game/app | M-H | H | P1 | Out-of-scope of framework guarantees; operator's responsibility; framework does not attempt evasion. |
| R-10 | Watchdog flapping masks an underlying issue | M | M | P2 | Restart-rate ceiling; ceiling-breach notification. |
| R-11 | Heartbeat written on hung loop (false liveness) | L | H | P1 | Heartbeat write is at end-of-tick only; orchestrator deadlock prevents end-of-tick. |
| R-12 | Asset manifest corruption | L | M | P3 | Validation at startup; checksums in manifest. |
| R-13 | Operator misconfigures threshold leading to false-positive flood | M | M | P2 | Boot-time threshold sanity check; warn if hard ≤ soft. |
| R-14 | Phone battery degradation from always-plugged | M | M | P3 | Modern phones self-limit charge; document the consideration. |
| R-15 | Power loss / kernel panic mid-action | L | M | P3 | State machine is in-process only; on restart, framework re-enters BOOTSTRAP. |
| R-16 | Adversarial input (e.g. malicious USB device pretending to be the phone) | L | H | P2 | RSA authorization gates ADB; host-side mitigations are out of scope. |
| R-17 | Concurrent automation tool also driving the phone | L | M | P3 | Detect by observing unexpected state in VALIDATING; refuse to run. |
| R-18 | Long-run log/metric/artifact disk exhaustion | M | H | P1 | Rotation policies enforced by the writers themselves; df-based circuit breaker. |
| R-19 | Time zone / DST handling in logs | L | L | P3 | All timestamps in UTC; convert at display. |
| R-20 | Python version drift in distro upgrade breaks venv | M | M | P2 | venv contains the interpreter pin; bootstrap re-validates. |

---

## 10. Detection and account-risk — neutral technical discussion

This section is **descriptive**, not prescriptive. It is included because operators must understand the system they are deploying; nothing here constitutes a recommendation to bypass any vendor's terms of service. The framework does not implement detection-evasion countermeasures.

### 10.1 Signals that distinguish automated interaction from human use

At the *behavioral* layer, the following are commonly cited:

- **Inter-action timing distribution** — humans exhibit a long-tailed, log-normal distribution of inter-event delays. A flat-uniform distribution stands out.
- **Coordinate distribution** — humans tap with finger-sized dispersion (5–20 px around an intended center). Pixel-precise repeated taps are anomalous.
- **Session length** — multi-hour uninterrupted sessions with no idle gaps are atypical of human play patterns.
- **Action-rate consistency** — humans speed up and slow down; bots tend to run at a steady rate.
- **Cross-session continuity** — patterns repeating exactly across sessions are a signal.

At the *signal* layer, ADB-mediated input is *not* distinguishable from finger input at the kernel event level (both are `EV_ABS` events through the touch input driver). The discriminators above operate above the kernel layer.

### 10.2 Engineering implications

- **ADR-15 (bounded jitter)** is *not* primarily a detection-evasion measure; it is a robustness measure (avoiding beat-locking with on-device animation). Its incidental effect on the behavioral signals above is real but not the design intent.
- Long-run automation will, by construction, exhibit *some* of the signatures above. The framework cannot fully reproduce human behavioral variance because it does not have access to the higher-order context (mood, attention, fatigue) that drives that variance.

### 10.3 Operator's responsibility

Use of automation against a third-party app or service is governed by that party's terms of service. Many vendors' terms explicitly prohibit automation. Consequences of detection include account suspension or termination. The framework provides no guarantees, no detection-evasion, and the operator is responsible for understanding the risk surface of their specific use case.

### 10.4 What we explicitly do not do

- No on-device modification of the input subsystem.
- No injection of fabricated sensor data (accelerometer, etc.) to simulate device motion.
- No process or memory inspection of the target app.
- No bypass of integrity attestations (Play Integrity, etc.).

These are out of scope by design and by intent.

---

## 11. Formal state machine specification

### 11.1 State table

| State | Type | Description | Entry conditions | Exit conditions | Timeout | On timeout | Retry policy |
|-------|------|-------------|------------------|-----------------|---------|-----------|---------------|
| BOOTSTRAP | initial | Load config, validate env, load manifest | process start | config OK ∧ env OK ∧ manifest OK → CONNECTING; else → FAULTED | 10 s | → FAULTED | none |
| CONNECTING | transitive | Establish ADB device | from BOOTSTRAP, RECONNECTING | `adb devices` shows `device` for fingerprinted serial → CALIBRATING | 30 s | → RECONNECTING (budget) / FAULTED | 3 attempts |
| CALIBRATING | transitive | Fingerprint device, compute remap, verify pipeline | from CONNECTING | first capture parses and resamples successfully → READY | 15 s | → FAULTED | 1 attempt |
| READY | stable | Warm idle, awaiting `run()` | from CALIBRATING | `run()` invoked → OBSERVING | ∞ | — | — |
| OBSERVING | active | Acquire a frame for the current tick | from READY, WAITING, VALIDATING | frame captured → MATCHING | 2 s | → RECONNECTING (counter) | 3 fails → escalate |
| MATCHING | active | Run THINK on the frame | from OBSERVING | match results computed → ACTING / WAITING / RECOVERING | 500 ms | → RECOVERING | n/a (CPU-bound) |
| ACTING | active | Send one ADB input | from MATCHING | input subprocess exits → VALIDATING | 2 s | → RECOVERING (counter) | 3 fails → escalate |
| VALIDATING | active | Confirm action took effect | from ACTING | expected-state template HITs → OBSERVING; else → RECOVERING | 2 s ¹ | → RECOVERING (counter) | 3 fails → escalate |
| WAITING | passive | Scheduled delay before next observation | from MATCHING (when state requires waiting) | delay elapsed → OBSERVING | per spec | → OBSERVING | none |
| RECOVERING | meta | Decide next recovery step | from MATCHING, ACTING, VALIDATING, OBSERVING | recovery policy resolves → RESET_LITE / RESET_HARD / FAULTED | 5 s | → FAULTED | none |
| RESET_LITE | active | Soft recovery: back-button, dismiss-modal | from RECOVERING | back-to-known-state → OBSERVING; failure → RESET_HARD | 5 s | → RESET_HARD | 2 attempts |
| RESET_HARD | active | Hard recovery: ADB restart + reconnect | from RECOVERING, RESET_LITE | ADB back online → CONNECTING | 30 s | → FAULTED | 1 attempt |
| RECONNECTING | active | ADB server bounce, re-handshake | from CONNECTING, OBSERVING | adb server responsive → CONNECTING | 30 s | → FAULTED | 2 attempts |
| FAULTED | terminal | Unrecoverable; process exits | from anywhere | process exits | — | — | watchdog restart |

> ¹ **VALIDATING timeout — Phase-5.5 note (2026-05-21):** the 2 s
> timeout is **per state entry** but the state may run two capture
> + match cycles in sequence (initial validate + one retry).
> Phase-5 live measurements show ~990 ms per cycle on this
> hardware, so the worst case is ~1.98 s — *tight* inside 2 s.
> Phase 6, when it adds explicit timeout enforcement, should raise
> the `VALIDATING` timeout to **3000 ms** (one full retry cycle
> plus a small head-room margin) OR re-scope the timeout as
> "per-cycle, not per-state". Tracked in
> [`docs/phase55_consistency_patch.md` §2.9](./docs/phase55_consistency_patch.md).

### 11.2 Transition table (selected high-value paths)

| From | Event | To | Side effect |
|------|-------|-----|-------------|
| BOOTSTRAP | config validated | CONNECTING | load manifest into memory |
| CONNECTING | `adb devices` shows device | CALIBRATING | issue `svc power stayon usb` |
| CALIBRATING | calibration OK | READY | emit `device_ready` event |
| READY | `run()` | OBSERVING | start tick counter |
| OBSERVING | frame captured | MATCHING | record capture latency |
| MATCHING | HIT on action-bearing template | ACTING | record match latency |
| MATCHING | HIT on waiting-required template | WAITING | schedule wakeup |
| MATCHING | no HIT | RECOVERING | increment miss counter |
| ACTING | input issued | VALIDATING | record action latency |
| VALIDATING | post-action template HIT | OBSERVING | clear retry counters for this action |
| VALIDATING | no HIT, retries remain | ACTING | increment validation-fail counter |
| VALIDATING | no HIT, retries exhausted | RECOVERING | record validation failure event |
| RECOVERING | first attempt | RESET_LITE | preserve fault category |
| RESET_LITE | succeeded | OBSERVING | reset miss counters |
| RESET_LITE | failed | RESET_HARD | log escalation |
| RESET_HARD | succeeded | CONNECTING | full re-handshake |
| RESET_HARD | failed | FAULTED | exit code = recovery_exhausted |
| any | unexpected exception | FAULTED | log + artifact |

### 11.3 Dead-state detection

A "dead state" is a state with no outgoing transitions that fire within its timeout. The state machine treats any state remaining beyond `timeout` as dead and either escalates per the timeout policy above or transitions to FAULTED. There is no state from which forward progress is impossible; the FSM is provably acyclic from the recovery cascade's perspective (RECOVERING → RESET_LITE → RESET_HARD → CONNECTING → ... → FAULTED is bounded).

### 11.4 Fail-safe exits

`FAULTED` is the universal fail-safe. Any unrecoverable condition routes here. On entry:

1. Final artifact snapshot is written (frame + state dump + last 50 log entries).
2. Heartbeat is *not* updated.
3. Process exits with a structured exit code.

The watchdog then restarts from `BOOTSTRAP`.

### 11.5 Recovery paths

See [§5 of ARCHITECTURE-DIAGRAMS.md](./ARCHITECTURE-DIAGRAMS.md#5-recovery-flow--what-happens-when-something-goes-wrong) for the recovery cascade diagram.

---

## 12. Roadmap

Phases are ordered by dependency. A later phase may begin only when the prior phase's exit criteria are met. Each phase produces a verifiable artifact set; nothing implicit.

The roadmap is **implementation-realistic**. It does not promise weeks-level certainty for any phase; difficulty estimates are coarse (S/M/L/XL).

### Phase 0 — Research & feasibility

| Field | Content |
|-------|---------|
| Goal | Validate the central engineering assumptions with measurements against the operator's actual hardware. Lock in the ADR set or revise. |
| Technical tasks | Bench screenshot pipelines (raw vs PNG) on the target device over USB 2.0 / 3.0; measure ADB subprocess overhead; profile `cv2.matchTemplate` on representative templates at 1080×1920; confirm raw screencap header layout on the device; measure USB autosuspend behavior. |
| Deliverables | `phase-0-report.md` containing: tabulated latencies, host CPU under load, decision to accept or revise ADR-01/02; a small Python script harness used to take the measurements (kept in `bench/`). |
| Success criteria | All ADR assumptions either confirmed or explicitly revised; latency NFRs verified or revised. |
| Risks | Operator hardware differs significantly from assumptions; USB cable in use is sub-spec; device's screencap header differs from documented layout. |
| Difficulty | S |
| Dependencies | A real Android device, a Linux host, ADB installed. |
| Engineering notes | The bench harness in this phase is intentionally throwaway; do not over-engineer it. The output is the report, not the harness. |
| Entry criteria | A device is available; ADB is functional (`adb devices` shows the device as `device`). |
| Exit criteria | Report committed; ADRs reviewed and either accepted as-is or amended. |
| Validation artifacts | `phase-0-report.md`, `bench/*` scripts, raw measurement CSVs. |
| Rollback considerations | n/a (research only). |

### Phase 1 — Environment & ADB foundation

| Field | Content |
|-------|---------|
| Goal | A reproducible Python environment, an ADB abstraction layer, and a working device-ready check. No CV, no FSM, no action engine yet. |
| Technical tasks | Build venv bootstrap script; pick `uv` vs `pip-tools` and produce lockfile; write `ADBClient` wrapper around subprocess invocations (`devices`, `shell`, `exec-out`, `wait-for-device`, `kill-server`, `start-server`); implement `DeviceFingerprint` (resolution, orientation, sdk version); implement device-sleep prevention (`svc power stayon usb`); write systemd user unit files (framework + watchdog stub); write `scripts/bootstrap.sh`. |
| Deliverables | `automation/adb.py`, `scripts/bootstrap.sh`, `systemd/automation.service`, `systemd/automation-watchdog.service`, `requirements.lock` (or equivalent), `tests/test_adb.py`. |
| Success criteria | Fresh checkout + `bootstrap.sh` + `python -m automation.cli probe` correctly reports the connected device. ADB commands recoverable from `kill-server`. Tests for ADB wrapper pass against a real device. |
| Risks | Bootstrap fragility across distros; ADB version skew on operator's host. |
| Difficulty | M |
| Dependencies | Phase 0 exit. |
| Engineering notes | The ADB wrapper is the most-touched module in the codebase; make its API surface small. Every ADB call goes through it. |
| Entry criteria | Phase 0 ADRs locked. |
| Exit criteria | All success criteria met. |
| Validation artifacts | `tests/test_adb.py` test report; manual `bootstrap.sh` run log on at least two distros. |
| Rollback considerations | The systemd units must be uninstallable (`bootstrap.sh --uninstall-service`). |

### Phase 2 — Screenshot pipeline

| Field | Content |
|-------|---------|
| Goal | A `Sensor` interface delivering `Frame` objects at the reference resolution, with primary and fallback modes. |
| Technical tasks | Implement raw screencap header parser; implement RGBA→BGR conversion; implement resampler (ADR-04); implement Sensor with primary/fallback selection; implement device fingerprint → remap computation; write fixtures for header-parse tests (capture sample bytes from at least two device profiles). |
| Deliverables | `automation/sense/sensor.py`, `automation/sense/raw_parser.py`, `automation/sense/remap.py`, `automation/sense/frame.py`, fixtures in `tests/fixtures/`. |
| Success criteria | 200 consecutive captures succeed; median latency within NFR; PNG fallback exercised via fault injection; remap is correct for at least 1080×1920 and 1080×2400. |
| Risks | Header-format variance on uncommon devices. |
| Difficulty | M |
| Dependencies | Phase 1. |
| Engineering notes | The raw parser is small but load-bearing; cover it with as many real-device-byte fixtures as possible. |
| Entry criteria | Phase 1 exit. |
| Exit criteria | Soak test: 1 hour of continuous 2 Hz capture with zero unrecovered failures. |
| Validation artifacts | Microbench results, soak test logs, fixture set. |
| Rollback considerations | Sensor interface should accept a `MockSensor` so the rest of the stack can be developed against recorded frames before the live pipeline is stable. |

### Phase 3 — Vision engine

| Field | Content |
|-------|---------|
| Goal | A `Matcher` that evaluates a set of templates against a `Frame` and produces calibrated `MatchResult`s. |
| Technical tasks | Implement Template / TemplateManifest with TOML metadata; implement matcher with `cv2.matchTemplate` + masks + ROI + per-template channel/grayscale; implement multi-scale fallback; implement preprocessing cache (gray frame, ROI crops); implement soft/hard threshold gating; build the replay harness scaffold (ADR-14); seed a small representative template corpus. |
| Deliverables | `automation/think/matcher.py`, `automation/think/template.py`, `automation/think/manifest.py`, `automation/think/preprocess.py`, `assets/templates/`, `tests/replay/`, `tests/test_matcher.py`. |
| Success criteria | Matcher correctly classifies HIT/SOFT/MISS on the seed corpus; per-template latency NFRs met; multi-scale fallback recovers MISSes on synthetically scaled inputs; replay harness can ingest a saved frame and re-run THINK. |
| Risks | Template corpus too small to be representative; threshold mistuning. |
| Difficulty | L |
| Dependencies | Phase 2. |
| Engineering notes | Resist the urge to make the matcher "smart" — keep it mechanical. Smarts live in the state machine. |
| Entry criteria | Phase 2 exit. |
| Exit criteria | Replay harness exercises ≥ 50 frames across ≥ 5 templates, with 100% expected classifications. |
| Validation artifacts | Replay harness output, microbench results. |
| Rollback considerations | The matcher must be swappable behind the `Matcher` interface so an experimental implementation can be A/B-tested. |

### Phase 4 — Action engine

| Field | Content |
|-------|---------|
| Goal | An `Actuator` that converts normalized action requests into ADB inputs, with jitter and validation hooks. |
| Technical tasks | Implement action classes (`tap`, `swipe`, `long_press`, `key`, `text`); implement jitter envelope sampling (ADR-15); implement coordinate denormalization with inverse remap; implement post-action waits; expose interface for future minitouch backend; write tests with a mock ADB client; live test on real device. |
| Deliverables | `automation/act/actuator.py`, `automation/act/jitter.py`, `automation/act/classes.py`, `tests/test_actuator.py`. |
| Success criteria | All action classes successfully drive a known UI; jitter sampling distribution matches spec; coordinate denormalization round-trips with negligible error. |
| Risks | `input` latency higher than estimated on the operator's device. |
| Difficulty | M |
| Dependencies | Phase 2 (for denormalization). |
| Engineering notes | The Actuator is the place where a misconfiguration causes real-world consequences (e.g. tapping the wrong place). The validation hook in Phase 5 closes that loop. |
| Entry criteria | Phase 2 exit. (Phase 3 not strictly required to start, but advisable.) |
| Exit criteria | 200 actions issued against a real device with 100% successful issuance and post-action validation handled by Phase 5 once it lands. |
| Validation artifacts | Action log, jitter distribution histogram. |
| Rollback considerations | Actuator behind interface; swap to mock for testing. |

### Phase 5 — State machine

| Field | Content |
|-------|---------|
| Goal | A hand-rolled FSM (ADR-08) realizing §11 of this dossier, integrated with SENSE / THINK / ACT, capable of executing simple end-to-end interactions. |
| Technical tasks | Define `State` enum and transition table; implement engine with entry/exit hooks, timeouts, retry counters; implement recovery states (RESET_LITE, RESET_HARD); integrate with Sensor, Matcher, Actuator; add per-state timeout enforcement; wire heartbeat write to tick end; Mermaid exporter for the transition table. |
| Deliverables | `automation/orchestrator/fsm.py`, `automation/orchestrator/recovery.py`, `automation/orchestrator/heartbeat.py`, integration tests. |
| Success criteria | An end-to-end "tap-and-validate" interaction runs cleanly on a real device; FAULTED is reachable from every fault path; the FSM diagram exported from code matches the diagram in ARCHITECTURE-DIAGRAMS.md. |
| Risks | Subtle race conditions between state transitions and asyncio scheduling. |
| Difficulty | L |
| Dependencies | Phases 2, 3, 4. |
| Engineering notes | Write the transition table first as a Python literal; the engine is a small interpreter over it. Avoid "smart" callbacks. |
| Entry criteria | Phase 4 exit. |
| Exit criteria | A 30-minute soak with a simple two-state script (observe → tap → observe → ...) runs cleanly. |
| Validation artifacts | Soak test log, exported Mermaid matches the diagram. |
| Rollback considerations | FSM is in one file; reverting is straightforward. |

### Phase 6 — Observability

| Field | Content |
|-------|---------|
| Goal | Production-grade observability: structured logs, metrics file, artifact store, replay debugging CLI. |
| Technical tasks | Implement structured JSON logger with correlation IDs; implement metrics writer (Prometheus text); implement artifact writer with rotation; implement `replay` CLI subcommand; instrument every subsystem with the events listed in §5.6.2; add df-based circuit breaker that disables artifact writes when disk is low. |
| Deliverables | `automation/observability/log.py`, `automation/observability/metrics.py`, `automation/observability/artifacts.py`, `automation/cli/replay.py`, instrumentation throughout the codebase. |
| Success criteria | A 1-hour soak produces correctly-shaped logs, metrics, and (under induced fault injection) artifacts; replay CLI reproduces a tick from a saved frame. |
| Risks | Logging overhead under high tick rates. |
| Difficulty | M |
| Dependencies | Phase 5. |
| Engineering notes | Observability is not the place to be clever; structured + consistent beats elegant. |
| Entry criteria | Phase 5 exit. |
| Exit criteria | Soak test produces ≤ 50 MB/day logs at default verbosity; rotation enforced. |
| Validation artifacts | Soak log artifacts, sample metrics output. |
| Rollback considerations | All observability is additive; can be disabled per subsystem via config. |

### Phase 7 — Hardening

| Field | Content |
|-------|---------|
| Goal | Long-run reliability: external watchdog, fault injection coverage, soak tests, configuration sanity checks. |
| Technical tasks | Implement watchdog process (ADR-11 L2); ship systemd units; fault-injection harness (kill adb server mid-tick, unplug-replug USB, force capture corruption); restart-rate ceiling; configuration validator (refuses hard ≤ soft, etc.); document operational runbook in `OPERATIONS.md`. |
| Deliverables | `watchdog/watchdog.py`, `tests/fault_injection/`, `OPERATIONS.md`, hardened systemd units. |
| Success criteria | A 24-hour soak with a script that includes induced faults completes; watchdog restart count under threshold; no unrecovered crash. |
| Risks | Watchdog itself has bugs (small but real); fault injection harness misses real-world failure modes. |
| Difficulty | L |
| Dependencies | Phase 6. |
| Engineering notes | The watchdog should be auditable in a single sitting — keep it minimal. |
| Entry criteria | Phase 6 exit. |
| Exit criteria | 24-hour soak passes. |
| Validation artifacts | Soak report, fault-injection coverage matrix. |
| Rollback considerations | Watchdog optional via config; framework runs without it (with reduced reliability). |

### Phase 8 — Deployment & long-run stability

| Field | Content |
|-------|---------|
| Goal | Operationalize the framework on a real operator host. Validate long-run behavior. |
| Technical tasks | Install systemd user units on the target host; complete `OPERATIONS.md` (start/stop, log inspection, template update workflow, incident response); 7-day soak; collect operational metrics; produce a small "first-failure analysis" report identifying anything observed during the soak that should feed back into a v1.1 backlog. |
| Deliverables | Installed framework, `OPERATIONS.md` validated by the operator, soak report. |
| Success criteria | 7-day soak meets reliability NFRs; operator can perform documented operations without engineer intervention. |
| Risks | Real-world conditions surface previously-unconsidered failure modes; operator workflows differ from assumptions. |
| Difficulty | M |
| Dependencies | Phase 7. |
| Engineering notes | This phase is *operational*, not implementation. Resist the temptation to keep coding once deployment begins; capture issues into the v1.1 backlog instead. |
| Entry criteria | Phase 7 exit + operator host availability. |
| Exit criteria | 7-day soak passes; operator signoff. |
| Validation artifacts | Soak report, signed-off OPERATIONS.md, v1.1 backlog. |
| Rollback considerations | `bootstrap.sh --uninstall-service` removes systemd units cleanly. |

---

## 13. Adversarial architecture review

This section attempts to break the design. Each finding is a *real* concern; the goal is honesty, not exhaustiveness. Findings are addressed in [DESIGN-REVIEW.md](./DESIGN-REVIEW.md) with a position (mitigate / accept / defer).

### 13.1 Brittle assumptions

- **USB-only assumption.** Reflexively excludes wireless ADB. A future requirement to support over-WiFi ADB invalidates parts of §5.1 and the latency budget.
- **Single-device assumption.** The orchestrator implicitly assumes one device. Multi-device is not a refactor; it is a re-architecture (per-device FSM instance, shared template store, contention on the adb client serial port).
- **Reference-resolution = 1080×1920.** Choosing 1080×1920 as the *canonical* reference baked in a portrait, 16:9 worldview. Modern phones are 19.5:9 or 20:9. Letterboxing helps but does not eliminate the issue.
- **`adb` client behavior is consistent across versions.** Platform-tools occasionally changes output format (`adb devices -l` for instance). The wrapper must be parsed defensively.
- **Templates are stable enough to be a sustainable asset.** True for *some* apps; not true for live-service games that re-skin frequently.

### 13.2 Scaling bottlenecks

- **Template-count scaling.** `n` templates × per-template match cost × tick rate is the dominant CPU cost. At ~20 templates per tick at 5 Hz, we are at the edge of the budget. The fix is more aggressive ROI use, but ROIs require human curation.
- **Frame size scaling.** A 2K-screen capture is ~3× the bytes of 1080. USB transport time scales accordingly. Going to 1440 baseline triples capture latency.
- **Log volume under noisy conditions.** A flapping match could produce thousands of soft-match log lines per minute. Log throttling is not in the v1 spec; it should be.

### 13.3 Single points of failure

- **adb server.** A single daemon between us and the device. Recoverable, but every recovery is a multi-second pause.
- **USB cable.** Mechanical failure mode (intermittent contact) is the single most common operational failure in this class of system. Outside framework's control.
- **Watchdog itself.** If the watchdog dies, no one restarts the framework. The watchdog has no watchdog — by design (no infinite regress), but the operator must monitor it. Mitigation candidate: systemd's own `Restart=always` on the watchdog unit (acceptable because the watchdog has no race against itself).
- **Disk fills.** Both logs and artifacts are local-file-only. A pathological log day fills the disk and brings down the host. Rotation policies must be enforced *defensively*.

### 13.4 Operational weaknesses

- **No central observability.** Per the scope of v1, observability is local. Diagnosing issues requires SSH'ing to the host and reading files. Acceptable for a single-host setup, painful at any scale.
- **Template authoring is manual.** A new template requires a captured screenshot, a TOML metadata file, optional mask creation. No tooling assists; this is real friction.
- **Theme drift detection.** A target app silently switches to a new theme variant (dark-mode-by-time-of-day, accessibility settings). The framework will degrade gracefully (soft matches → recovery) but the *cause* is not surfaced clearly; the operator sees "lots of soft matches" without knowing why.

### 13.5 Hidden maintenance burden

- **Templates per resolution-bucket.** The remap (ADR-04) reduces but does not eliminate per-resolution sensitivity for templates with very fine detail (1-px font weight differences, etc.). In practice this means a small number of templates need per-resolution variants, and the manifest schema must accommodate that — which it does not currently.
- **Phase 0 measurements assume the operator's hardware doesn't change.** A laptop replacement re-opens Phase 0. Mitigation: the bench harness from Phase 0 stays in the repo for re-running.
- **Python ecosystem rot.** OpenCV's wheel ABI has broken between Python minor versions before. The lockfile mitigates but does not eliminate; a forced Python upgrade re-opens the dep set.

### 13.6 Fragility risks

- **Subprocess overhead floor.** Every ADB call pays ~30–80 ms of subprocess spawn. Batching could amortize this (`adb shell` with multiple commands in one invocation) but introduces parsing complexity. Not addressed in v1; flagged as a future optimization.
- **Asyncio + blocking I/O.** The thread pool offloads blocking I/O, but a misbehaving CV call that *doesn't* release the GIL would freeze the event loop. The lint rule and runtime guard in ADR-07 mitigate, but the failure mode exists.
- **State machine misconfiguration.** The transition table is data; nothing prevents an operator from authoring an FSM with unreachable states. Mitigation: a startup-time graph validator (currently in Phase 7 backlog).

### 13.7 Architectural blind spots

- **No first-class "wait for animation to settle" primitive.** A common pattern is "wait until the screen stops changing, then act." The framework currently encodes this as "loop in WAITING + observe with the same template and require N consecutive matches." It works, but the lack of a dedicated primitive means every script author reinvents the pattern.
- **No "expected screen" abstraction higher than templates.** A "screen" (a collection of templates that together identify a state) is implicit in the FSM's transition logic, not first-class. A future refactor should introduce a `Screen` concept.
- **No semantic versioning of the template manifest.** If templates change incompatibly, no version field stops an older framework from loading them and behaving badly. Mitigation: add a `manifest_version` to the manifest's top-level metadata.

### 13.8 Summary

Most findings above are either *mitigated within v1* (with explicit references to ADRs or phases) or *deferred with a clear rationale*. A small number — the disk-fill circuit breaker, the manifest version field, the log throttling — should be addressed in v1.1 and are noted in [DESIGN-REVIEW.md](./DESIGN-REVIEW.md).

The architecture as drawn is honest about its limits. It is not the highest-performance design possible. It is the *highest-reliability* design we can build with the technology stack chosen, at the budget of operator effort we expect.

---

## End of dossier

Companion documents:
- [ADR.md](./ADR.md) — full decision records for every load-bearing choice.
- [ARCHITECTURE-DIAGRAMS.md](./ARCHITECTURE-DIAGRAMS.md) — canonical visual reference.
- [PHASE-MASTER-PROMPTS.md](./PHASE-MASTER-PROMPTS.md) — exhaustive per-phase implementation prompts.
- [DESIGN-REVIEW.md](./DESIGN-REVIEW.md) — honest weakness assessment and v1.1 backlog.
