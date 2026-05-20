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

### 2.1 Phase 0 measurements have not been taken (INVESTIGATE)

The latency budget (§3.1 of SYSTEM-ROADMAP) consists of **engineering estimates**, not measurements. Phase 0 will validate them. Until that report lands, the NFRs are aspirational. The strongest possible statement we can make about v1 performance today is: *it should work, given the structural costs of the chosen pipeline*. We cannot promise the numbers in §3.1 without measuring on the operator's hardware.

### 2.2 USB power management is operator-dependent (INVESTIGATE)

Some Linux distributions, especially on laptops, aggressively autosuspend USB devices to save power. ADB will reconnect on resume, but the latency to detect "device is back" can be tens of seconds. The framework's RECONNECTING path handles this, but the operator's experience under aggressive autosuspend has not been measured. Phase 0 includes a check; if it fails, the operator must disable autosuspend on the device's USB port, which adds an operator setup step.

### 2.3 Raw screencap header format on uncommon devices (INVESTIGATE)

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

| # | Item | Source | Estimated effort |
|---|------|--------|------------------|
| 1 | Manifest versioning (§1.6) | self | S |
| 2 | Disk-space circuit breaker for logs (in addition to artifacts) | adversarial review §13.5 of SYSTEM-ROADMAP | S |
| 3 | Log throttling for flapping events (§13.2) | adversarial review | S |
| 4 | `automation tools template-new` interactive CLI (§3.1) | self | M |
| 5 | "Screen" abstraction above templates (§1.4) | self | M |
| 6 | `StablePredicate` for "wait for animation to settle" (§1.5) | self | S |
| 7 | Live screen-capture explorer for debugging (§3.2) | self | M |
| 8 | Concurrent-adb-user detection (§2.6) | self | S |
| 9 | Mask authoring helper (frame-diff-based animated-pixel detector) (§3.1) | self | M |
| 10 | OEM ROM coverage in replay corpus (§5.4) | self | S–M |
| 11 | Optional HTTP endpoint for metrics (§4.6) | self | S |
| 12 | OCR add-on as a focused tool, not a primary matcher (§5.3 of SYSTEM-ROADMAP) | self | M |
| 13 | Multi-resolution reference support (§1.2) | self | L |
| 14 | Multi-device per host (§1.7) | self | XL |
| 15 | A small TUI / web operator console (§13.4 of SYSTEM-ROADMAP) | self | M |

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

## 9. Closing position

The architecture, as documented, is a defensible v1 design. Its weaknesses are known, its assumptions are labeled, and its deferral list is real and acknowledged. The team should expect:

- Real measurements in Phase 0 to revise at least one ADR. (Possible: a new screencap format on the operator's device; or USB 2.0 latency higher than estimated.) That is fine — the dossier is meant to evolve.
- At least one new failure mode in Phase 8 that the dossier did not anticipate. That is normal and the v1.1 backlog absorbs it.
- The template asset to be the single biggest sustained source of maintenance effort across the framework's lifetime. Plan for it.
- The watchdog and recovery cascade to be the most operationally valuable pieces of the system, even though they are small in code. Treat them with care.

If a reviewer reads only one part of this dossier, this document is a fine choice — it tells them where to push hardest.

---

## End of design review

Updates to this document should be additive. Items are not deleted when they are addressed; they are crossed out with a reference to the PR / phase where they were resolved. The historical record of "what we thought might be wrong" is as valuable as the eventual fix.
