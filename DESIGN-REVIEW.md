# Internal Design Review

> **Document type:** Honest internal critique of the design dossier
> **System:** Android UI Automation Framework (Python + OpenCV + ADB)
> **Companion documents:** [SYSTEM-ROADMAP.md](./SYSTEM-ROADMAP.md), [ADR.md](./ADR.md), [ARCHITECTURE-DIAGRAMS.md](./ARCHITECTURE-DIAGRAMS.md), [PHASE-MASTER-PROMPTS.md](./PHASE-MASTER-PROMPTS.md)
> **Author posture:** Adversarial; the goal is to expose, not defend.

The other documents in this dossier present the architecture in its best light. This document is the counterweight. It catalogs what the design does not do well, what it does not yet know, and what should be expected to fail or to need rework. Each entry is labeled with a position:

- **MITIGATE** — there is a plan in v1 to address it. The dossier already accounts for this and the work is scheduled.
- **ACCEPT** — the team has decided to live with this. The rationale is recorded; the cost is understood.
- **DEFER** — this is real and not addressed in v1. It lands in the v1.1 backlog.
- **INVESTIGATE** — the team does not yet know enough to take a position. Phase 0 measurement or Phase 8 observation is required.

If a future maintainer is wondering "why didn't they do X?", the answer is in here.

---

## 1. Architectural weaknesses

### 1.1 The framework is a single point of failure for everything below it (ACCEPT)

ADR-07 chose a single-process architecture. The blast radius of a crash is the entire automation. The watchdog (ADR-11) restarts on hard fault, but during the restart window (typically 5–30 s), the device is unmanaged. For the target use case — single-device, low-frequency automation on a single operator's host — this is acceptable. For any deployment that values *continuous* coverage, a different topology would be required, but designing for that would have inflated v1 by a large factor.

**Rationale to accept:** the cost of multi-process / multi-host topologies is real (IPC for frames, supervision hierarchy, distributed state) and not warranted by current requirements.

**What changes the calculus:** moving to multi-device, real-time-critical use, or "must be available 24/7 with < 5 s downtime" SLOs. Any of those reopens this choice.

### 1.2 Reference resolution is a single locked value (DEFER → v1.1)

ADR-04 locked the reference resolution at 1080×1920. This means:

- Operators with 1440×3200 phones run with a downsampled reference, losing some precision.
- Operators with extreme aspect ratios (foldables in unfolded mode, tablets) get letterboxed beyond what was anticipated.
- Templates captured at the reference resolution have one fixed level of detail; very fine UI details (1-pixel borders, small font weight differences) may not survive resample.

A future version should support an *operator-configurable* reference resolution chosen at calibration time per device profile, with templates re-keyed automatically. This is not a small change — every template's coordinates and dimensions are reference-dependent — so it is deferred.

### 1.3 Templates as the unit of recognition limit what the system can recognize (ACCEPT)

ADR-03 chose template matching. This excludes:

- UIs that are procedurally generated and never identical twice (e.g. dynamic art, particle effects).
- UIs that scale or rotate freely.
- UIs that are text-heavy and where the text itself is the discriminator.

For these classes, the framework either needs a different recognition strategy (OCR, feature matching, neural detection) or cannot reliably automate them. The `Matcher` interface is designed to accommodate alternative implementations, but actually building them is out of scope.

### 1.4 No first-class "screen" concept above templates (DEFER → v1.1)

A "screen" — the collection of templates that together identify the device's current logical screen — is implicit in the FSM. Script authors must encode "this screen is recognized by X AND Y AND NOT Z" through the FSM transition logic. This is workable but not ergonomic; a `Screen` abstraction would clean it up substantially. Deferred because v1 needs a working FSM, not a perfect one.

### 1.5 No first-class "wait for animation to settle" primitive (DEFER → v1.1)

The "wait until N consecutive frames match" pattern is common. It is currently encoded ad-hoc in scripts. A built-in `StablePredicate` that subsumes this would simplify scripts and make the intent more readable.

### 1.6 No semantic versioning of the template manifest (DEFER → v1.1)

If templates change incompatibly (e.g. a new mandatory field is added), an older framework version will load them and behave incorrectly. A `manifest_version` field at the top of the manifest, checked by the loader, would prevent this. Small change but real; deferred only because the manifest format is still in flux through Phase 3.

### 1.7 Single-device assumption (ACCEPT)

Stated in §2.2. Adding multi-device is a re-architecture, not a refactor. The orchestrator owns one FSM; multi-device needs one FSM per device with shared resources (template manifest, observability sinks). This is fine; many operators run a single device per host. Multi-device is a v2 conversation.

---

## 2. Unresolved technical risks

### 2.1 Phase 0 measurements have not been taken (~~INVESTIGATE~~ RESOLVED 2026-05-20)

> **Update 2026-05-20:** Phase 0 measurements complete. See
> [phase-0-report.md](./phase-0-report.md). Several latency NFRs were
> revised in [docs/frozen_nfrs_v1.md](./docs/frozen_nfrs_v1.md); see
> also [ADR-01a](./ADR.md#adr-01a--screenshot-pipeline-phase-0-reality-content-dependent-ordering-usb-link-speed-prerequisite).
> The new Phase-0 discoveries are catalogued in §9 below.

The latency budget (§3.1 of SYSTEM-ROADMAP) consists of **engineering estimates**, not measurements. Phase 0 will validate them. Until that report lands, the NFRs are aspirational. The strongest possible statement we can make about v1 performance today is: *it should work, given the structural costs of the chosen pipeline*. We cannot promise the numbers in §3.1 without measuring on the operator's hardware.

### 2.2 USB power management is operator-dependent (~~INVESTIGATE~~ RESOLVED for this host 2026-05-20)

> **Update 2026-05-20:** Phase 0's 5-minute idle test confirms
> `adb devices` continues to report the device with no autosuspend
> remediation needed on Ubuntu 24.04 / kernel 6.17. `power/control=on`
> is the kernel default for this device class on this host. The
> general concern remains valid for *laptops* with more aggressive
> power management policies — those operators may still need to
> disable autosuspend, but the operator's current host does not.

Some Linux distributions, especially on laptops, aggressively autosuspend USB devices to save power. ADB will reconnect on resume, but the latency to detect "device is back" can be tens of seconds. The framework's RECONNECTING path handles this, but the operator's experience under aggressive autosuspend has not been measured. Phase 0 includes a check; if it fails, the operator must disable autosuspend on the device's USB port, which adds an operator setup step.

### 2.3 Raw screencap header format on uncommon devices (~~INVESTIGATE~~ PARTIALLY RESOLVED 2026-05-20)

> **Update 2026-05-20:** Phase 0 verifies the documented 16-byte
> Android 9+ layout (width, height, format=1 / RGBA_8888, colorspace)
> on the Xiaomi 22095RA98C (Android 13). Buffer round-trips to
> `16 + W * H * 4` exactly. See [phase-0-report.md §6](./phase-0-report.md).
> The wider concern for *other* OEMs remains; that part is still
> INVESTIGATE for future operators on different hardware.

The documented header is `uint32 width, uint32 height, uint32 format[, uint32 colorSpace]`. We trust this on Pixel, Samsung, Xiaomi, and OnePlus devices, where it has been observed in the wild. We have not verified it on every OEM. A device that ships with a non-standard header will fall back to PNG mode (slower) but should not fail outright. The Phase 0 report must include a documented header layout for the operator's specific device.

### 2.4 OpenCV ABI / wheel drift across Python minor versions (ACCEPT)

OpenCV wheels are tied to Python ABI. A forced Python 3.11 → 3.12 upgrade has, in the past, broken `opencv-python-headless`'s installation transiently on some platforms. The lockfile (ADR-16) defends against this, but a Python upgrade is an event that must be planned (re-bench, re-test). Documented in OPERATIONS.md.

### 2.5 ADB version drift (ACCEPT)

`adb devices -l` output format has changed between platform-tools versions. The parser is defensive (line-by-line, looks for the token `device` in the second column), but adversarial output could fool it. We have not enumerated every output variant from every adb version. Phase 1 tests with fixtures from at least three platform-tools versions; this is sufficient for confidence, not for certainty.

### 2.6 Concurrent adb clients on the same host (DEFER → v1.1)

If another tool on the host runs `adb` against the same device (Android Studio, a separate script, an IDE plugin), it will share the adb server. Our framework can detect "the device is in an unexpected state" via VALIDATING, but cannot diagnose "another tool is interfering." The DESIGN-REVIEW position is to detect-and-refuse: refuse to run if `adb` reports the device in any state other than `device` after our health check. This is a minor v1.1 enhancement.

### 2.7 The watchdog has no watchdog (ACCEPT)

ADR-11 acknowledges this. The watchdog is small enough to audit, and systemd `Restart=on-failure` on the watchdog unit catches *crashes*; what it doesn't catch is *hangs* (the watchdog itself hung in a deadlock). The watchdog's design forbids any blocking operation longer than a few seconds, so this is mitigated by construction, but a `kill -SIGSTOP` on the watchdog would silently disable supervision. Acceptable for a single-host setup; revisit if there is ever a remote attacker model.

---

## 3. Anticipated technical debt

### 3.1 Template authoring is manual and labor-intensive (DEFER → v1.1)

Today: capture a screenshot, crop it, save as PNG, write a TOML alongside, possibly draw a mask in an image editor. A single new template can take 5–15 minutes. The system *works* but the friction is real, and authors will be tempted to skip the mask or skip a ROI just to ship.

A v1.1 should include `automation tools template-new <id>` that takes the current frame, opens an interactive crop tool, prompts for fields, and produces the metadata file. The mask remains manual unless we add a "find animated pixels by differencing N captures" utility (also a v1.1 candidate).

### 3.2 No "screen capture explorer" tooling (DEFER → v1.1)

When templates start to fail, the operator's first need is to see *what the framework sees right now* and pull *what the framework is looking for* up next to it. The replay CLI from Phase 6 helps after the fact; a live explorer (saving every Nth frame to a watched directory while a debug session runs) would help during.

### 3.3 The script DSL is implicit (DEFER → v1.1)

Phase 5 introduces `Script` as a configuration object. Realistic operator scripts will be sequences of conditional logic ("if this template, tap there, else swipe up"). The v1 design accommodates this as TOML-declared rules consumed by the FSM, but the format is not yet ergonomic. Expect early operators to express dissatisfaction with the script format; a v1.1 should iterate based on real script examples from Phase 8.

### 3.4 No central observability (ACCEPT in v1; DEFER for fleets)

v1 emits to local files. Single operator, single device, one host: fine. The moment there is a fleet, this becomes painful. Adding a Prometheus push-gateway or an OTLP exporter is a small change *if* the operator's environment supports it. Out of scope for v1.

### 3.5 Asyncio + thread pool with no explicit task supervision (ACCEPT)

asyncio in Python 3.11 has `TaskGroup` for structured concurrency. The orchestrator should use it; in places where Phase 5 introduces concurrent tasks (e.g. a watcher task alongside the main loop), `TaskGroup` is preferable to bare `create_task`. This is a code-quality concern, not a correctness concern, and it is captured as a Phase 5 implementation note.

### 3.6 The thread pool size is configured globally (ACCEPT)

A single bounded thread pool serves all ADB subprocess calls. If a future requirement introduces a second high-throughput ADB user (e.g. a sidecar that pushes logs to the device), they will share the same pool and may starve each other. v1 has one consumer; this is theoretical. Captured for awareness.

---

## 4. Open questions

These are decisions the dossier deliberately did not make because the cost of being wrong was less than the cost of waiting for information.

### 4.1 Default jitter envelope values

ADR-15 commits to *the existence* of bounded jitter envelopes. The numeric defaults (50–150 ms pre-delay, 0.005 normalized dispersion, etc.) are placeholders. Phase 0 should propose initial values; Phase 8 will refine them in light of actual operator scripts.

### 4.2 Which observation strategy for "the screen hasn't changed in N frames"

Options: pixel-difference threshold, structural-similarity (SSIM), hash comparison (perceptual hash). Each has tradeoffs. v1 defers the choice; the first script that needs it will force the decision. The `StablePredicate` work-item in §1.5 will resolve this.

### 4.3 How to handle modal popups that appear randomly

The framework's recovery cascade handles known modals (RESET_LITE can dismiss a configurable safe-area tap). Unknown modals (a Play Store update prompt, a system permissions dialog) cannot be dismissed by a template the framework doesn't have. v1's stance: the operator must capture these as they appear and add them to a "dismiss list" template set; the FSM handles dismiss-list matches with priority. This works but burdens the operator. A v1.1 could add anomaly detection: "the current frame doesn't match any expected screen and contains text" → escalate without auto-dismissing.

### 4.4 The exact semantics of "destructive action"

§9 R-07 names destructive actions as a risk (false positive → bad outcome). The dossier proposes "destructive action class requires double-confirmation template," but does not specify which actions are destructive. This must be decided per-operator script; the framework provides the *mechanism* (an action class flag + a required validation template).

### 4.5 Whether to support Wayland on the host

The framework is headless; this should not matter. But the operator's interaction with the framework (running CLI commands, viewing logs) does happen on a desktop session. If the operator uses GNOME on Wayland, certain debugging tools (screen recording, taking host screenshots of the artifact directory) behave differently than on X11. v1 makes no Wayland-specific accommodations. Captured for awareness.

### 4.6 Whether to expose the metrics file over HTTP

A small HTTP server exposing `var/metrics/metrics.prom` would let Prometheus scrape it. The implementation is 30 LOC. The reason to *not* do this in v1 is to avoid opening any network surface, even local. Acceptable to defer; the file is readable directly.

---

## 5. Validation gaps

Categories of behavior the test plan does not yet adequately cover.

### 5.1 Long-tail device profiles

Replay traces are captured on a small number of devices. The framework will be deployed on *one* operator's device (we know which one), so the validation gap is closed by Phase 8 *for that device*. For any future operator with a different device, the validation is essentially restarting at Phase 0.

### 5.2 Theme variants

Tests cover default theme only. Dark mode, accessibility large-text, high-contrast modes — each is a separate validation surface. v1 supports them via per-theme templates, but the test corpus does not exercise them.

### 5.3 Battery / charging state

The framework keeps the device's screen on via `svc power stayon usb`. We have not validated behavior under low-battery conditions, where the OS may push aggressive power-management policies that override our setting. Phase 8 should observe this.

### 5.4 Real OEM ROM variance

Stock Android (Pixel) is the implicit test target. OEM ROMs (One UI, MIUI, Realme UI, ColorOS) ship variations in:

- USB-debug authorization dialog behavior.
- `screencap` binary outputs.
- Animation curves on system UI.

The replay corpus should include at least one OEM ROM beyond stock. This is captured as a Phase 3 task improvement; partially mitigated by the framework's defensive parsing, but not eliminated.

### 5.5 Soak-test workload representativeness

Phase 7's 24-hour soak runs a synthetic script. Phase 8's 7-day soak runs the operator's real script. There is no overlap *between* "synthetic, controlled fault injection" and "real, no fault injection." A real operator's script under fault injection should be added to the v1.1 backlog as a more confidence-inspiring soak.

### 5.6 Concurrency under heavy CV load

Tests assume per-template match cost stays within budget. We have not modeled what happens if a single tick's active template set is unusually large (say 20 templates on a complex screen). Will GIL release timing in OpenCV remain favorable, or will the event loop stutter? Phase 0 should bench a worst-case template-set size as part of its match benchmarks.

### 5.7 Negative-test coverage for malformed configs

Phase 1's config tests cover malformed TOML (parse error) and missing required keys. They do *not* cover:

- valid TOML with semantically invalid values (e.g. `hard_threshold = -1`).
- configuration "drift" — a config that was valid in v1.0 but is no longer valid after Phase 7's stricter validator was added.

The validator added in Phase 7 (§7.7 of the Phase 7 prompt) closes the first. The second is a migration concern, deferred.

---

## 6. Recommended further investigations

For the team executing the phases, before locking in significant work:

1. **(Phase 0)** Bench the operator's specific hardware end-to-end. If latency comes in 2× worse than the engineering estimates, the NFRs need revision *before* implementing against them.
2. **(Phase 0)** Capture raw screencap header bytes on the operator's device, hex-dump them, verify against the documented layout. If different, write a new ADR.
3. **(Phase 0)** Time `cv2.matchTemplate` on a representative full-resolution frame across the seed template corpus. Decide multi-scale defaults from data.
4. **(Phase 3)** Take 3–5 captures of the *same* UI screen seconds apart. Diff them. Identify which pixels move (animation), which don't (stable). This informs mask authoring.
5. **(Phase 5)** Prototype the FSM with a *throwaway* script before committing to the transition table format. The cost of changing the format later (rewriting every script) is high.
6. **(Phase 7)** Bench logging overhead. If structured JSON logging at default verbosity costs > 1% of tick time, drop log level or downsample. Do not let observability become the bottleneck.
7. **(Phase 8)** During the first day of soak, sit with the framework. Don't just inspect logs after the fact. The first day is when surprising behaviors surface most quickly.

---

## 7. Future improvements (v1.1 backlog)

Ordered by approximate priority, top first. None are committed; this is a starting list for v1.1 planning.

Tags:
- **v1.0** — should land in v1.0 (Phase 1–8), not v1.1
- **v1.1** — landed in v1.1 (post-v1.0)
- **future** — beyond v1.1; deferred indefinitely

| # | Tag | Item | Source | Estimated effort |
|---|---|------|--------|------------------|
| 1 | v1.1 | Periodic runtime USB link-speed re-check (the bootstrap-time check lands in v1.0; this covers mid-run link degradation) | Phase 0.5 §9.1 | S |
| 2 | v1.1 | `sensor.mode = "auto"` enabled by default + A/B sampler hysteresis tuning (Phase 0.5 ships behind a feature flag) | Phase 0.5 §9.2 / ADR-01a | M |
| 3 | v1.1 | Manifest versioning (§1.6) | self | S |
| 4 | v1.1 | Disk-space circuit breaker for logs (in addition to artifacts) | adversarial review §13.5 of SYSTEM-ROADMAP | S |
| 5 | v1.1 | minicap escalation path documented + tested for high-tick-rate operators | Phase 0.5 §9.4 | M |
| 6 | v1.1 | Log throttling for flapping events (§13.2) | adversarial review | S |
| 7 | v1.1 | `automation tools template-new` interactive CLI (§3.1) | self | M |
| 8 | v1.1 | "Screen" abstraction above templates (§1.4) | self | M |
| 9 | v1.1 | `StablePredicate` for "wait for animation to settle" (§1.5) | self | S |
| 10 | v1.1 | Device-side `screencap` composition cost measurement (`adb shell time screencap`) | Phase 0.5 §9.5 | S |
| 11 | v1.1 | Live screen-capture explorer for debugging (§3.2) | self | M |
| 12 | v1.1 | Concurrent-adb-user detection (§2.6) | self | S |
| 13 | v1.1 | Mask authoring helper (frame-diff-based animated-pixel detector) (§3.1) | self | M |
| 14 | v1.1 | OEM ROM coverage in replay corpus (§5.4) | self | S–M |
| 15 | v1.1 | Optional HTTP endpoint for metrics (§4.6) | self | S |
| 16 | v1.1 | OCR add-on as a focused tool, not a primary matcher (§5.3 of SYSTEM-ROADMAP) | self | M |
| 17 | future | Multi-resolution reference support (§1.2) | self | L |
| 18 | future | Multi-device per host (§1.7) | self | XL |
| 19 | future | A small TUI / web operator console (§13.4 of SYSTEM-ROADMAP) | self | M |
| 20 | v1.1 | Cheaper validation strategy — deferred validation (post-action observation tick is the validation evidence). Largest expected speed win on this hardware. | Phase-5 discovery §10.1 | M |
| 21 | v1.1 | Adaptive pre-validation delay per action class (reduce retry rate on slow animations) | Phase-5 discovery §10.3 | S |
| 22 | v1.1 | In-frame diff validation (cheap SSIM/pixel-diff fallback before full re-match) | Phase-5 discovery §10.1 | M |
| 23 | v1.1 | `Action.requires_validation: bool` annotation for action classes whose effect is structurally unobservable (key/text) | Phase-5 discovery §10.1 | S |
| 24 | v1.1 | Auto-wire `HeartbeatWriter.beat()` into the Phase 7 `Watchdog.run_tick()` so the L1 supervisor produces a beat for every supervised tick without operator wiring | Phase-8A discovery §10a.2 | S |
| 25 | v1.1 | Ship the L2 action layer (Phase 8B) — systemd `--user` unit + restart-rate ceiling + `var/run/watchdog-restarts.log` | Phase-8A discovery §10a.1, ADR-11a | M |

---

## 8. Things we explicitly chose not to do, and why

For the record, so they are not re-debated later without new information:

- **Use a state-machine library.** ADR-08. The transition table is small enough to hand-roll.
- **Use minicap / scrcpy as the primary screenshot pipeline.** ADR-01. Operational complexity is not justified at our tick rate.
- **Use ORB / SIFT as the primary matcher.** ADR-03. Wrong tool for the UI matching surface; reserved as a fallback.
- **Install an on-device companion app.** Out of scope §2.2. Doubles the trust and deployment surface for marginal benefit.
- **Use a deep-learning detector.** ADR-03. Adds GPU dependency, training data lifecycle, opaque failure modes; not justified for v1.
- **Use multi-process architecture.** ADR-07. Frame serialization cost > parallelism gain at our throughput.
- **Use YAML or JSON for configuration.** ADR-13. TOML is the right balance.
- **Implement detection-evasion countermeasures.** §10 of SYSTEM-ROADMAP. Out of scope by design; the operator owns ToS risk.
- **Support Windows / macOS.** §2.1. Increases test surface ~3×; Linux is the operator's environment.
- **Add a GUI.** §2.2. Headless is faster to ship; a TUI/web console is a v1.1 candidate.

---

## 9. Phase 0 discoveries (added 2026-05-20)

This section is **additive** — added during Phase 0.5 Spec Lock to
catalogue concerns surfaced by Phase 0 measurements that the
pre-Phase-0 dossier did not anticipate. Each entry is positioned (per
the document's convention) MITIGATE / ACCEPT / DEFER / INVESTIGATE.

### 9.1 USB topology risk — silent 12 Mbps hub failure (MITIGATE)

**Discovery (VF):** Phase 0 first observed the operator's device
negotiated at 12 Mbps (USB 1.1 Full Speed) because the cable was
plugged through a keyboard's built-in USB hub. After re-plugging
directly into a USB 2.0 high-speed port, the device renegotiated at
480 Mbps. The 12 Mbps state would have multiplied screencap latency
by ~40× silently — no error, just a 40× slower framework.

**Position — MITIGATE.** Phase 1 `bootstrap.sh` SHALL read
`/sys/bus/usb/devices/<path>/speed` for the connected device and
refuse to start (or warn loudly) if the link is below 480 Mbps. See
[ADR-01a §Decision (5)](./ADR.md#adr-01a--screenshot-pipeline-phase-0-reality-content-dependent-ordering-usb-link-speed-prerequisite),
[SYSTEM-ROADMAP §5.1.7](./SYSTEM-ROADMAP.md#517-usb-link-speed-validation-phase-05-addition),
and `PHASE-MASTER-PROMPTS.md` Phase 1 (updated).

**Residual concern (v1.1):** the link speed could drop *after* a
successful bootstrap if the operator unplugs and replugs into a
slower port mid-run. Bootstrap-time check does not catch this. A
periodic runtime re-check is captured as v1.1 backlog row #1.

### 9.2 Entropy-dependent screencap ordering (ACCEPT)

**Discovery (VF):** PNG-vs-raw ordering reverses with screen content
on this hardware. On low-entropy screens (homescreen-like, ~500 KB
PNG payload) PNG is ~450 ms faster than raw. On high-entropy screens
(~1.4 MB PNG payload) raw is ~360 ms faster than PNG. The fastest
mode depends on the target app's hot screens.

**Position — ACCEPT.** ADR-01a documents this and provides a
configuration knob (`sensor.mode`). Default `"raw"` because raw is
content-deterministic; operators on low-entropy UIs are documented to
override.

**Residual concern (v1.1):** the `auto` mode that A/B-samples and
switches dynamically is small code (~50 LOC) but has not been
soak-tested. ADR-01a ships it behind a feature flag in v1.0; v1.1
should enable by default once Phase 8 confirms behavior.

### 9.3 Mandatory ROI discipline for hot-path templates (MITIGATE)

**Discovery (VF):** full-frame BGR matching takes 137.9 ms median per
template on this hardware. At 8 templates per tick the framework
spends 1.1 s in matching alone, exceeding the MATCHING state's 500 ms
timeout. Full-frame grayscale (33.6 ms) and ROI variants (2.2–7.0 ms)
are within budget.

**Position — MITIGATE.** Phase 3's manifest loader will WARN on
hot-path templates that omit a ROI hint. ADR-03 received a Phase-0.5
clarification note (preserved as a status block; ADR text unchanged).
Operators may still author full-frame BGR templates but must opt in
with an explicit metadata flag (`full_frame: true`) and accept the
per-state timeout implications.

**Residual concern:** none in v1.0. Phase 3's manifest validation
closes this.

### 9.4 Revised throughput expectations (ACCEPT)

**Discovery (VF):** sustained tick rate of 2–5 Hz is unachievable on
this hardware with `adb exec-out screencap`. Realistic v1.0 tick rate
is 0.5–1 Hz.

**Position — ACCEPT.** NFRs frozen at the new numbers; see
[docs/frozen_nfrs_v1.md §1](./docs/frozen_nfrs_v1.md). Operators
requiring higher tick rates have two paths:
1. Switch to minicap (deferred per ADR-01; Phase 8 may revisit).
2. Use a USB 3.x-capable device on a USB 3.x host (operator-side,
   no framework change required).

**Residual concern (v1.1):** if operators consistently want higher
tick rates, prioritize minicap as a Phase 8 follow-up. Backlog row #5.

### 9.5 Device-side `screencap` composition cost (INVESTIGATE)

**Discovery (UE):** raw screencap on the operator's device takes
~947 ms median while the USB transport floor for 10.4 MB at 260 Mbps
is ~324 ms. The implied ~620 ms is device-side `screencap`
composition cost (the device's `screencap` binary rendering and
serializing the framebuffer on each invocation). Not directly
measured.

**Position — INVESTIGATE.** A targeted experiment (`adb shell time
screencap > /sdcard/x.raw`) would pin down the device-side number.
This does not change v1.0 architecture but informs the realistic
upper bound on what any pipeline change could buy. Backlog row #10.

### 9.6 Phase 0 confirmed no remediation needed (ACCEPT)

For the operator's specific host + device pair, Phase 0 confirmed:

- USB autosuspend does not engage with kernel defaults (§2.2).
- Raw screencap header layout matches the documented format (§2.3).
- ADB subprocess latency is at the *better* end of the 30–80 ms
  engineering range (28 ms median; §2.2 of ADR.md).

These are documented as **resolved** rather than residual concerns.

---

## 10. Phase 5 discoveries (added 2026-05-21)

This section is additive — added during Phase 5.5 Reality Sync to
catalogue concerns surfaced by Phase 5's `Orchestrator` measurements
that the pre-Phase-5 dossier did not anticipate. Same positioning
convention as §9 (MITIGATE / ACCEPT / DEFER / INVESTIGATE), with
v1.0 / v1.1 / future tags.

### 10.1 Validation dominates tick cost (ACCEPT for v1.0; DEFER cheaper-validation to v1.1)

**Discovery (VF):** the Phase-5 `Orchestrator.tick()` reaches
`VALIDATING` on every successful `ACTING` step. The `VALIDATING`
state is, by design, a full `Sensor.capture()` + `Matcher.match()`
cycle — identical in cost to the `SEARCHING` state. On the
operator's hardware that is ~990 ms per cycle (940 ms capture +
50 ms match). With the single validation retry, a validated tick
costs *up to 3 captures* (~3.0 s wall-clock).

The frozen `tick_latency_median ≤ 1500 ms` NFR predated this
design and was framed against `tick = SENSE + THINK + ACT`. It is
violated on every validated tick.

**Position — ACCEPT for v1.0.** The Phase-5 design is correct
given the single-template constraint and the single-tick scope of
Phase 5. NFRs were tier-split in Phase 5.5 (`docs/frozen_nfrs_v1.md`
§1.1 amended) to reflect measurement honestly:

- search-only tick: ≤ 1500 ms median (unchanged).
- validated tick, no retry: ≤ 2200 ms median (new tier).
- validated tick + retry: ≤ 3000 ms median, ≤ 3300 ms p95 (new tier).

See [ADR-08a](./ADR.md#adr-08a--validation-cost-consequence-of-the-fsm-design-phase-55).

**Residual concern (v1.1):** three candidate cheaper-validation
strategies are documented but not implemented:

1. *In-frame diff* — a cheap pixel/SSIM diff on the ROI around the
   matched template detects "did the screen change at all" without
   the full match. Cuts the match cost from a cycle but not the
   capture cost.
2. *Deferred validation* — the next observation tick's natural
   capture *is* the validation evidence. Moves validation off the
   action-bearing tick's critical path. Requires multi-tick state
   tracking in the FSM.
3. *No-validation action classes* — an `Action.requires_validation:
   bool` annotation. Actions whose effect is structurally
   unobservable (e.g. future `key`/`text` global hotkeys) skip the
   cycle entirely.

The biggest single win is (2). Tracked as v1.1 backlog (row #20 in §7).

### 10.2 Single-retry budget proved its worth live (ACCEPT)

**Discovery (VF):** during Phase 5's Demo 3 (engineered happy path)
the **first** validation cycle caught a *mid-animation transition
frame* — the recents-to-app launch animation was still playing,
the recents template was partially visible, the match scored
above threshold. The **retry** validation cycle (one sleep-and-
recapture later) caught the settled frame; the template was
cleanly absent and the tick was correctly marked as success.

Without the retry, the orchestrator would have declared a
validation failure on a tick that actually succeeded. The
`VALIDATION_RETRY_BUDGET = 1` constant earned its place.

**Position — ACCEPT.** The single retry is the right number for
v1.0: zero retries would have produced a false negative on Demo 3;
unlimited retries would mask actions that legitimately did not
take effect. One retry is the smallest budget that handles the
transition-frame edge case without hiding genuine failures.

**Residual concern:** none in v1.0. Phase 7 soak should verify
that the retry rate stays low (a high retry rate would suggest
the animation/timing assumption is wrong for some scripts).

### 10.3 Transition-frame reality (INVESTIGATE → Phase 7)

**Discovery (UE):** Phase 5's Demo 3 showed that the *first*
validation capture can land mid-animation. The animation duration
is device- and app-dependent; on this device the recents-to-app
animation appears to settle within ~1 capture cycle, i.e. ~940 ms.
We do not have a measurement of the animation distribution.

**Position — INVESTIGATE in Phase 7 soak.** Real scripts will
encounter animations of varying durations. The Phase 7 soak
should instrument the *retry rate* as a function of action class
and target template; if retries cluster on specific scripts the
operator can either widen pre-validation delay or split the action
into sub-actions.

**Residual concern (v1.1):** the framework currently has no
adaptive pre-validation delay. A simple "wait N ms before
validating" policy per action class would reduce the retry rate
at the cost of tick latency. ADR-15 envelopes contemplate
pre/post-delay but Phase 5 does not consume them. Tracked v1.1
(row #21 in §7).

### 10.4 Validation economics — capture cost dwarfs everything else (ACCEPT)

**Discovery (VF):** decomposing a validated tick on this hardware:

```
search capture   ~940 ms  (46% of tick)
search match     ~50 ms   (2%)
action (tap)     ~60 ms   (3%)
validate capture ~940 ms  (46%)
validate match   ~50 ms   (2%)
                 ─────────
                 ~2040 ms (100% — happy path)
```

The capture cost dominates so completely that the only meaningful
optimisation lever is *fewer captures per tick*. Per-template
match cost (the natural place to look for cycle savings) is
already near its floor.

**Position — ACCEPT.** The implication is that v1.1's
cheaper-validation strategies (§10.1) are the *only* path to
faster validated ticks on this hardware. Optimising the matcher
(adding mask support, multi-scale, etc.) would not move the tick
latency needle materially.

**Residual concern:** none for v1.0. Operators wanting faster
ticks should follow the path documented in
`docs/frozen_nfrs_v1.md` §1.3: minicap (deferred per ADR-01),
scrcpy frame intercept (same), or USB 3.x hardware.

### 10.5 Phase 5 narrowed scope vs original prompt (ACCEPT)

**Discovery (factual):** the original `PHASE-MASTER-PROMPTS.md`
Phase 5 specified a 13-state FSM (SYSTEM-ROADMAP §11 in full),
plus `Script`, `Screen`, recovery cascade, CLI extension, Mermaid
exporter, and heartbeat writer. The actual Phase 5 delivered the
inner-slice 5-state FSM (`automation/state.py`) with no
recovery cascade, no heartbeat, no Mermaid exporter, no CLI, no
Script abstraction. Documented in `phase5-report.md` §2.4.

**Position — ACCEPT.** The narrower scope was the operator's
deliberate choice; it produced a smaller, fully tested,
production-quality orchestrator core (333/333 tests, 94%
coverage on `orchestrator.py`). The unimplemented surface —
recovery cascade, heartbeat, Script, Mermaid exporter, CLI —
lands in **Phase 7** (Hardening) per the existing
PHASE-MASTER-PROMPTS structure. The Phase-5 prompt itself is
not amended in Phase 5.5; the reconciliation happens when
Phase 7 lands.

**Residual concern:** none. The Phase 5.5 consistency audit
(`docs/phase55_consistency_patch.md` §2.12) records this as an
OPEN tracked-for-Phase-7 item.

---

## 10a. Phase 8A discoveries (added 2026-05-21)

This section is additive — catalogues observations from the
Phase 8A L2 watchdog implementation. Positioning convention same
as §9 / §10 (MITIGATE / ACCEPT / DEFER / INVESTIGATE).

### 10a.1 L2 observation is sufficient on its own for the operator's primary need (ACCEPT)

**Discovery (factual):** the operator's primary missing-watchdog
concern is "I'd like to know when the framework has hung."
Phase 8A's `ExternalWatchdog.check()` answers that question
without any side-effects: poll once per N seconds from a shell
loop / cron / systemd timer, get a `WatchdogStatus`, decide what
to do with it. The "decide what to do" half is small (≤ 20 LOC
of operator script) but framework-independent.

**Position — ACCEPT for v1.0.** Splitting observation from
action allows operators to wire the L2 into different
supervision substrates without re-implementing the observer.
Phase 8B will add a thin "act on the recommendation" piece —
small, substrate-specific, and not part of the framework's
core API.

### 10a.2 Heartbeat is *not* auto-wired into the Phase 7 Watchdog (DEFER → Phase 8B)

**Discovery (factual):** Phase 8A ships `HeartbeatWriter` as a
standalone utility; the Phase 7 `Watchdog.run_tick()` does not
call `heartbeat.beat(...)` automatically. The Phase 8A prompt
prohibits modifications to Phase 7's `automation/watchdog.py`,
so the wiring is left to a future caller — either a Phase 8B
"run loop" that owns the iteration cadence, or an operator
script that wraps `run_tick + beat` together.

**Position — DEFER to Phase 8B.** The wiring is small (one
`hb.beat(correlation_id, wd.last_health)` line after each
`run_tick()`); it does not warrant breaking the Phase 8A
prompt's process-boundary discipline. Captured as v1.1 backlog
row #24.

### 10a.3 L2 recommendation strings are wire-stable (ACCEPT)

**Discovery (factual):** Phase 8A's recommendations are exactly
three strings: `"none"`, `"RESET_LITE"`, `"RESET_HARD"`. These
match the recovery cascade tokens in SYSTEM-ROADMAP §11.1 (which
were specified by ADR-08 / ADR-11) so a future Phase 8B caller
can route them into the right recovery handler without
translation.

**Position — ACCEPT.** Wire-stable strings are easier to parse
in shell scripts than enums, and the small set avoids the
"recommendation: 0x4" debugging trap.

### 10a.4 No L2 ⇄ L1 leakage (ACCEPT)

**Discovery (factual):** `watchdog/watchdog.py` imports nothing
from `automation/orchestrator`, `automation/sensor`,
`automation/matcher`, `automation/actuator`, the L1
`automation/watchdog`, or `automation/runtime_health`. A
dedicated unit test (`test_module_does_not_import_orchestrator`)
fails the build if any such import sneaks in. The process
boundary is structurally enforced.

**Position — ACCEPT.** The L2 watchdog could be split out into
its own distribution / repository without code changes; only the
`automation.errors` import would need replacing.

### 10a.5 Heartbeat schema is versioned (MITIGATE)

**Discovery (factual):** the heartbeat carries
`schema_version: 1`. The L2 watchdog refuses to interpret any
schema_version other than 1 (returns `INVALID + RESET_HARD`).
This makes future schema changes safe: an old L2 watchdog
observing a new framework will recommend HARD-restart (the
operator notices and upgrades) rather than silently
mis-classify.

**Position — MITIGATE.** v1.0 ships with schema_version 1.
Any future change needs the schema-version bump + matching
L2 watchdog version. Tracked in this report's §10a.

### 10a.6 L2 status enum is closed (ACCEPT)

**Discovery (factual):** the four statuses (HEALTHY / STALE /
MISSING / INVALID) are validated by `WatchdogStatus.__post_init__`.
New statuses cannot be silently introduced.

**Position — ACCEPT.** Closed enum is the right v1.0 choice;
the discovery is that no Phase 8B caller will need additional
verdicts to act on the existing three recommendations.

---

## 11. Closing position

The architecture, as documented, is a defensible v1 design. Its weaknesses are known, its assumptions are labeled, and its deferral list is real and acknowledged. The team should expect:

- Real measurements in Phase 0 to revise at least one ADR. (Possible: a new screencap format on the operator's device; or USB 2.0 latency higher than estimated.) That is fine — the dossier is meant to evolve.
- At least one new failure mode in Phase 8 that the dossier did not anticipate. That is normal and the v1.1 backlog absorbs it.
- The template asset to be the single biggest sustained source of maintenance effort across the framework's lifetime. Plan for it.
- The watchdog and recovery cascade to be the most operationally valuable pieces of the system, even though they are small in code. Treat them with care.

If a reviewer reads only one part of this dossier, this document is a fine choice — it tells them where to push hardest.

---

## End of design review

Updates to this document should be additive. Items are not deleted when they are addressed; they are crossed out with a reference to the PR / phase where they were resolved. The historical record of "what we thought might be wrong" is as valuable as the eventual fix.
