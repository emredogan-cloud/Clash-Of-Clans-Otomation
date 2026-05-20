# Architecture Decision Records (ADR)

> **Document type:** Architecture Decision Records
> **System:** Android UI Automation Framework (Python + OpenCV + ADB)
> **Reference architecture:** SENSE → THINK → ACT
> **Status of dossier:** Design phase — no implementation has begun
> **Audience:** Engineers tasked with implementation, future maintainers, design reviewers

This document captures the major architectural decisions for the framework. Each ADR follows the format:

> Context → Decision → Alternatives → Consequences → Risks → Rejection rationale

ADRs are immutable after acceptance. If a decision is reversed, write a new ADR that supersedes the old one rather than editing it in place. The list is intentionally narrow: only decisions whose reversal would force a non-trivial code rewrite are included.

---

## Index

| ID | Title | Status | Supersedes |
|----|-------|--------|------------|
| ADR-01 | Screenshot pipeline — `adb exec-out screencap` (raw, pixel-format-aware) as primary | Accepted | — |
| ADR-02 | Screenshot encoding — uncompressed raw framebuffer over PNG | Accepted | — |
| ADR-03 | Primary computer-vision strategy — normalized template matching with masks | Accepted | — |
| ADR-04 | Resolution independence — fixed reference resolution + affine remap | Accepted | — |
| ADR-05 | Color representation — BGR retained, grayscale optional per template | Accepted | — |
| ADR-06 | Input injection — `adb shell input` with optional minitouch escalation | Accepted | — |
| ADR-07 | Process topology — single-process, asyncio + thread pool for blocking I/O | Accepted | — |
| ADR-08 | State machine — hand-rolled finite-state engine, no framework | Accepted | — |
| ADR-09 | Coordinate handling — normalized [0,1] internal, integer device pixels at edge | Accepted | — |
| ADR-10 | Asset / template library — content-addressed on-disk store with manifest | Accepted | — |
| ADR-11 | Recovery & watchdog — external watchdog process, not in-process supervisor | Accepted | — |
| ADR-12 | Observability — structured JSON logs + Prometheus-compatible metrics file + artifact store | Accepted | — |
| ADR-13 | Configuration — layered TOML + environment overrides, no runtime mutation | Accepted | — |
| ADR-14 | Testing — recorded-trace replay harness as the primary integration test surface | Accepted | — |
| ADR-15 | Anti-fragility — bounded behavioral jitter, opt-in per action class | Accepted | — |
| ADR-16 | Python version & dependency strategy — CPython 3.11+, pinned lockfile, no system Python | Accepted | — |

---

## ADR-01 — Screenshot pipeline: `adb exec-out screencap` (raw) as primary

### Context

The SENSE layer needs to capture the device screen on every automation tick. The pipeline dominates end-to-end latency: in steady-state operation the framework spends more wall-clock time waiting on a frame than on any other stage. Throughput, jitter, and reliability of this pipeline set a ceiling on the rest of the system. Four options are commonly available on stock Android over ADB:

1. `adb shell screencap /sdcard/x.png` followed by `adb pull /sdcard/x.png`
2. `adb exec-out screencap -p` streaming a PNG over stdout
3. `adb exec-out screencap` streaming a raw uncompressed framebuffer over stdout
4. A native helper binary (minicap / scrcpy frame interception)

A fifth, direct `/dev/graphics/fb0` reads, is unavailable on modern Android due to SELinux policy on user builds.

### Decision

Adopt **`adb exec-out screencap`** in **raw, pixel-format-aware mode** as the primary screenshot pipeline, with `screencap -p` (PNG over exec-out) as a portable fallback selected by configuration.

### Alternatives considered

| Option | Median latency (engineering estimate, USB 2.0) | CPU on host | Reliability | Complexity |
|--------|------------------------------------------------|-------------|-------------|------------|
| `screencap` + `pull` | 500–1500 ms | low | high | low |
| `exec-out screencap -p` (PNG) | 150–400 ms | medium (PNG decode) | high | low |
| `exec-out screencap` (raw) | 80–250 ms | low | medium (format parsing) | medium |
| minicap stream | 30–80 ms | low | medium (binary lifecycle) | high |
| scrcpy frame intercept | 30–80 ms | medium (H.264 decode) | medium | high |

Latency figures above are **engineering estimates** based on published community benchmarks and the structural cost of each step (USB transfer, PNG encode/decode, disk I/O). They are not measured numbers; Phase 0 must verify them on the target device class before lock-in.

### Consequences

- The framework parses the raw screencap binary header (`width:uint32, height:uint32, pixel_format:uint32`, optionally `colorSpace:uint32` on Android 9+) on every frame. This header format is stable but undocumented; the parser is treated as a load-bearing component and covered by dedicated unit tests against captured fixtures from representative devices.
- The host CPU avoids PNG decompression on the hot path. PNG decode of a 1080×1920 frame is on the order of 20–60 ms of CPU and dominates the THINK budget on slower hosts.
- On older Android versions (pre-9), the framebuffer pixel format must be queried and the parser must support both 3-byte-header and 4-byte-header variants. This is one of the few places the code carries an Android-version branch.

### Risks

- **Format drift:** Future Android versions may extend the header or change the default pixel format. Mitigation: the parser fails closed (refuses to decode unknown formats) and the system automatically falls back to `screencap -p` PNG mode, surfacing the event as a metric and a warning log.
- **Per-OEM variation:** Some OEM ROMs ship customized `screencap` binaries. Mitigation: device fingerprinting on first connection produces a small calibration screenshot that the parser must round-trip successfully before the framework declares the device "ready."
- **USB jitter:** USB transfer latency is bursty under contention. Mitigation: the pipeline measures per-frame USB transfer time and exposes it as a metric so degradation is observable.

### Rejected alternatives — why

- **`screencap` + `pull`**: 5–15× slower median, generates write amplification on `/sdcard`, leaves orphan files on crash, and adds a TOCTOU window between write and pull. The only reason to keep it would be portability to ancient `adb` clients we do not target.
- **minicap / scrcpy**: faster, but introduce a third-party binary lifecycle (push, chmod, port-forward, restart on disconnect), bind us to upstream maintenance, and historically have versioning fragility against new Android releases. They remain a future option for high-FPS use cases (>5 FPS sustained), behind an explicit feature flag. They are not appropriate as a baseline because they trade reliability and operational simplicity for latency we do not yet need.

---

## ADR-02 — Screenshot encoding: raw framebuffer over PNG

### Context

Closely related to ADR-01 but worth recording independently because the encoding choice is what most consumers see (the byte layout of a captured frame), whereas the pipeline choice is about the transport.

### Decision

Internal representation of a captured frame is a NumPy `ndarray` of `uint8` with shape `(H, W, 3)` in BGR order, materialized directly from the raw screencap framebuffer with a single in-process conversion step (RGBA → BGR, dropping alpha).

### Alternatives considered

| Encoding | Decode cost / 1080p frame | Memory | Notes |
|----------|---------------------------|--------|-------|
| PNG | 20–60 ms CPU | bounded | universal, slow |
| Raw RGBA → BGR | 1–4 ms CPU | 8 MB / frame | fast, format-sensitive |
| JPEG (via minicap) | 5–15 ms CPU | bounded | lossy, blocks pixel-perfect matching |
| H.264 (via scrcpy) | 8–25 ms CPU | bounded | lossy, decoder dep |

### Consequences

- Memory bandwidth per frame is ~8 MB (1080×1920×4). At 2 FPS this is 16 MB/s sustained — negligible.
- Lossless representation is required because some template matches use thresholds at the 0.92–0.98 normalized correlation level; lossy JPEG/H.264 can shave a few hundredths off correlation and silently flip a marginal match to non-match.

### Risks

- Memory leaks through retained `ndarray` views are a classic NumPy footgun. Mitigation: all frames are owned by a `Frame` object whose lifetime is tied to a single tick; views are forbidden across ticks.

### Rejected alternatives — why

- **JPEG / H.264 lossy**: not safe for pixel-precise UI matching against high-confidence thresholds.
- **PNG retained as primary**: forces CPU decode on every frame, breaks the latency budget on lower-end hosts.

---

## ADR-03 — Primary CV strategy: normalized template matching with masks

### Context

The THINK layer must answer two classes of question on every tick: *"Is element X present?"* and *"Where is element X?"* Both must be cheap (≤ 50 ms per template, ideally ≤ 15 ms), robust to mild rendering variation (anti-aliasing differences, minor animation phase), and produce a calibrated confidence score so downstream code can reason about uncertainty.

### Decision

Use **`cv2.matchTemplate` with `TM_CCOEFF_NORMED`** and **per-template binary masks** as the primary matching primitive. Wrap it in a higher-level abstraction that handles ROI restriction, multi-scale fallback, and confidence calibration. Feature-based matching (ORB) is retained as an opt-in secondary strategy for templates that cannot be made to work with normalized correlation.

### Alternatives considered

| Technique | Strength | Weakness | Wall-clock (1080p, full frame) |
|-----------|----------|----------|--------------------------------|
| `TM_CCOEFF_NORMED` | fast, calibrated [-1, 1] | not scale/rotation invariant | 5–25 ms |
| `TM_SQDIFF_NORMED` | fast | inverted polarity, less intuitive | 5–25 ms |
| ORB + BFMatcher / RANSAC | scale + rotation invariant | brittle on solid UI, slower | 20–120 ms |
| SIFT | very robust | overkill for UI, slowest | 50–300 ms |
| Deep-learning detector (e.g. YOLO) | most robust | requires labeled data + GPU; opaque failure modes | 30–80 ms with GPU |
| OCR (e.g. Tesseract) | reads text directly | slow, font-sensitive | 100–500 ms / region |

### Consequences

- The library of templates becomes the single most important asset. Templates are versioned, content-addressed (ADR-10), and accompanied by a binary mask whenever they contain transparent or animated regions.
- The matching abstraction caches grayscale conversions and ROI crops to avoid redundant work when multiple templates target overlapping regions.
- Animated elements (pulsing buttons, glow effects) are handled by *masking out* the animated pixels in the template and matching only the stable region, not by inflating the threshold or sampling multiple frames.

### Risks

- **Template drift**: A game or app update redraws an icon. The match silently degrades. Mitigation: every match below a `soft` threshold but above a `hard` threshold emits an observability event; a sustained spike of soft matches is a signal that templates need regeneration.
- **Resolution sensitivity**: `matchTemplate` is not scale-invariant. Mitigation: ADR-04 (resolution independence via affine remap) eliminates the common case; multi-scale fallback covers the long tail.
- **Lighting / theme variants**: Dark mode, accessibility themes, gamma differences across panels can shift correlation scores. Mitigation: per-template thresholds with per-device calibration profiles, plus the option to match on grayscale when color is incidental.

### Rejected alternatives — why

- **ORB as primary**: structurally too slow for full-frame search at our target tick rate, and its tolerance to rotation is wasted on UI elements that never rotate. Retained as a *fallback* for difficult templates.
- **Deep-learning detector as primary**: imposes an order-of-magnitude increase in operational complexity (model lifecycle, training data, GPU dependency), and removes the human-auditable, source-controllable nature of templates. The framework is designed so a future detector can be slotted in as another implementation of the `Matcher` interface, but it is not the baseline.
- **OCR as primary**: necessary for some text-driven flows (reading dynamic counters, timers) and will be included as a *focused* tool with explicit invocation, not as the general matching primitive.

---

## ADR-04 — Resolution independence: fixed reference resolution + affine remap

### Context

Real devices ship a long tail of resolutions and aspect ratios (1080×1920, 1080×2400, 1440×3200, 720×1600, foldables with mid-flow aspect changes, etc.). A naive template captured at 1080×1920 will not match at 1440×3200. The framework must work across at least the dominant resolution buckets without maintaining N copies of every template.

### Decision

Define a single **reference resolution** (1080×1920 portrait baseline). Every captured frame is **resampled (or letterboxed) into the reference frame** before THINK runs. Every ACT coordinate produced by THINK is **inverse-mapped back to device pixels** before being sent to ADB. The remap is a 2D affine transform (scale + optional letterbox offset) computed once per device session.

### Alternatives considered

1. **One template per resolution bucket** — explosive maintenance, brittle on novel devices.
2. **Multi-scale template matching only** — works but costs 3–5× CPU per match, and still does not handle aspect ratio differences.
3. **Per-device homography from anchor calibration** — most general, but introduces a calibration step before any automation runs.

### Consequences

- All templates are captured and stored at the reference resolution.
- The remap step costs ~3–8 ms per frame for bilinear resampling at 1080×1920. This is part of the per-tick budget.
- Letterboxing is preferred over distortion when aspect ratios differ, because UI elements are typically anchored to one edge (top status bar, bottom nav) and stretching distorts hit targets.

### Risks

- **Anchor mismatch on non-portrait UIs** (landscape games, tablets in portrait-only apps) — must be detected up front via orientation query (`dumpsys input`) and refused or remapped explicitly. Mitigation: refuse to run on unsupported orientations and surface a clear error.
- **Subpixel drift** — sub-pixel coordinates produced by resampling are rounded when sent to `input tap`. Mitigation: tap dispersion is bounded (ADR-15) and well within finger-touch tolerances.

### Rejected alternatives — why

- **One template per resolution**: linear template-asset growth in number of supported devices, which becomes unmanageable.
- **Multi-scale only**: doesn't address aspect ratio, and bakes a CPU cost into every match rather than once per session.

---

## ADR-05 — Color representation: BGR retained, grayscale optional per template

### Context

`cv2.matchTemplate` accepts both single-channel and 3-channel inputs. Grayscale matching is ~3× faster but loses information when the template's distinguishing feature is color (a green "play" vs. red "stop" of identical shape).

### Decision

Keep frames in BGR by default. Each template metadata record declares whether it matches in **BGR**, **grayscale**, or **a specific channel** (e.g. red channel only). The matcher honors that declaration.

### Consequences

- Templates that can match in grayscale (high-contrast icons, text) opt in and benefit from ~3× speed.
- Color-dependent templates remain unambiguous and the matching cost is acknowledged at template-creation time, not buried.

### Rejected alternatives — why

- **Global grayscale**: silently breaks color-discriminated templates.
- **Global BGR**: leaves easy speed-ups on the table for the majority of templates.

---

## ADR-06 — Input injection: `adb shell input` with optional minitouch escalation

### Context

The ACT layer must perform taps, swipes, long-presses, and (rarely) multi-touch. The two practical options on stock devices are `adb shell input` (high-level, slow per call) and `minitouch` (low-level, streaming, fast). Both are dual-use; both produce real touch events the system cannot distinguish from a human absent timing analysis.

### Decision

Use `adb shell input tap|swipe|keyevent` as the primary action engine. Expose a `LowLatencyInputAdapter` interface so that minitouch can be plugged in for use cases that require sub-100 ms input latency or multi-touch gestures.

### Alternatives considered

| Method | Latency per action | Multi-touch | Setup complexity |
|--------|--------------------|-------------|------------------|
| `input tap` | 80–250 ms | no | none |
| `sendevent` | 30–80 ms | yes (manually framed) | high (per-device event codes) |
| minitouch | 10–40 ms | yes | medium (push binary + socket) |
| scrcpy control channel | 20–60 ms | yes | medium |

### Consequences

- Default builds work with zero device-side setup beyond enabling USB debugging.
- The action engine internally measures actuation latency (time from request to `input` exit) and exposes it as a metric. If sustained latency exceeds budget, operators can opt in to minitouch without changing call sites — the `ActionEngine` interface is stable across both backends.
- `sendevent` is **not** exposed as a backend in v1 because the per-device event-code maintenance burden is unacceptable.

### Risks

- `input tap` has a known floor on actuation latency of roughly 80 ms even on fast devices, due to the JVM bootstrap of the `input` command. This is the single largest controllable contributor to per-action latency. Mitigation: only matters if the use case is latency-critical, in which case minitouch is the documented escape hatch.

### Rejected alternatives — why

- **`sendevent` primary**: per-device event-code fragility makes it a maintenance trap.
- **Custom Accessibility Service**: requires installing a signed app on the device, which moves the framework from "host-side only" to "host + on-device" and dramatically widens the deployment and trust surface.

---

## ADR-07 — Process topology: single process, asyncio + thread pool

### Context

The framework has three concurrent concerns: I/O against ADB (blocking), CV work (CPU-bound, releases the GIL inside OpenCV), and orchestration (state machine, timers, logging). It must run on a single-host Linux desktop.

### Decision

**Single process**, **asyncio event loop** as the orchestration backbone, and a **bounded thread pool** for blocking subprocess I/O against `adb`. CV work runs inline on the event-loop thread because OpenCV releases the GIL during heavy operations, so it does not block other asyncio tasks long enough to matter at our tick rate.

### Alternatives considered

1. **Multi-process** (separate SENSE / THINK / ACT processes connected by IPC) — overkill for the throughput we target; adds serialization cost (frame bytes between processes are ~8 MB each), failure modes, and deployment complexity.
2. **Pure-threaded** (no asyncio) — possible but timers, cancellation, and structured concurrency are clumsier than asyncio for the orchestrator role.
3. **Multi-threaded with explicit pipelines** — possible, but the throughput we target (≤ 5 FPS, ≤ 30 actions/min) does not require parallelism beyond what GIL-releasing CV gets us for free.

### Consequences

- Crash blast radius is the whole framework; the **external watchdog** (ADR-11) restarts it on fault.
- Memory is one shared address space, simplifying frame ownership.
- Adding parallelism later is a refactor, not a rewrite, because the SENSE/THINK/ACT subsystems are already isolated behind interfaces.

### Risks

- Long-running CV operations that *don't* release the GIL (rare, but possible in user code) will starve the loop. Mitigation: lint rule and runtime guard that warns when an asyncio task exceeds a budget (50 ms by default).

### Rejected alternatives — why

- **Multi-process**: serialization cost of frames > the gain at our tick rate, and the operational complexity (process supervision inside the framework) duplicates what the external watchdog already does.

---

## ADR-08 — State machine: hand-rolled finite-state engine, no framework

### Context

The orchestrator is a state machine of moderate size (target ~10–20 states in v1, ~30–50 by maturity). Existing libraries (`transitions`, `python-statemachine`, `automat`) exist but introduce a dependency whose lifecycle and idioms must be learned and maintained.

### Decision

Hand-roll the state machine as a small (estimated 200–400 LOC core) module: explicit `State` enum, transitions as a declarative table, entry/exit hooks, per-state timeout, retry counter, and a recovery pointer. No external state-machine library.

### Consequences

- Zero added dependency.
- The state model is fully readable in one file. There is no abstraction layer hiding what "transition" means.
- Visualization tooling (Mermaid export of the transition table) is small enough to write in 50 LOC.

### Risks

- "Why not a library" is a common reviewer question. Mitigation: the implementation is small enough to audit, and a switch to `transitions` later is a contained refactor.

### Rejected alternatives — why

- **`transitions` library**: adds a dependency, its callback model couples poorly with asyncio without adapters, and the transition table format is verbose for our needs.
- **`python-statemachine`**: similar reasoning; smaller than `transitions` but still a framework where one file would do.

---

## ADR-09 — Coordinate handling: normalized [0,1] internally, integer device pixels at the edge

### Context

Coordinates flow through three frames of reference: device pixels (variable per device), reference-resolution pixels (1080×1920), and normalized [0,1]. Picking the *primary* internal frame matters for clarity and correctness.

### Decision

Inside THINK and the state machine, coordinates are **normalized floats in [0,1]**. Templates record their match anchor as normalized coordinates. The action engine is the *only* component that converts normalized → device-pixel integers, applying inverse-remap (ADR-04) and optional jitter (ADR-15).

### Consequences

- Templates and rules are resolution-agnostic on the page.
- Bugs where a 1080-px coordinate is sent to a 1440-px device are structurally impossible because the type system (or a thin wrapper class) distinguishes the two.
- Logs are more readable: `tap at (0.50, 0.92)` survives device swaps in a way `(540, 1766)` does not.

### Risks

- Floating-point inexactness in the normalized form is invisible at one decimal place but real at six. Mitigation: normalized coords are rounded to three decimal places when logged, and the action engine rounds-to-nearest at conversion time.

### Rejected alternatives — why

- **Reference pixels internal**: still resolution-locked from a *reasoning* standpoint, even if the remap exists.
- **Device pixels internal**: makes the framework device-specific in its core, defeating the purpose of ADR-04.

---

## ADR-10 — Asset / template library: content-addressed on-disk store with manifest

### Context

Templates are the most volatile asset in the system. They will be added, removed, retuned, and occasionally regenerated en masse when the target app updates. Their lifecycle must be auditable and reproducible.

### Decision

Templates live under `assets/templates/` as PNG files. Each template is paired with a TOML metadata file describing: ID, capture device, capture date, reference-resolution coordinates, match strategy (BGR/gray/channel), thresholds (`hard`, `soft`), optional mask, optional ROI hint. The on-disk tree is rendered into an in-memory `TemplateManifest` at startup; the manifest hashes each template's bytes for content addressing.

### Consequences

- Template regeneration is a *file operation*, not a code change. Non-engineers can contribute.
- The manifest is a single source of truth for "what does the framework currently believe about the UI."
- Hashing enables a clean cache key for any preprocessing (grayscale conversion, edge maps) keyed to the template content.

### Risks

- Drift between template metadata and actual file. Mitigation: the manifest loader validates metadata against file dimensions and channel count at startup, refusing to start on mismatch.

### Rejected alternatives — why

- **Templates in code as base64 blobs**: unmaintainable, defeats reviewability.
- **Database-backed asset store**: overkill for what is fundamentally a small set of files under version control.

---

## ADR-11 — Recovery & watchdog: external process, not in-process supervisor

### Context

The framework can fault in ways the framework cannot itself recover from: a process-wide segfault inside a C extension, a hung asyncio loop, an OS-level OOM kill. Recovery from these requires something *outside* the failing process.

### Decision

Recovery has **two layers**:

1. **In-process recovery** for soft faults: ADB disconnect, screencap timeout, state-machine stuck-state. Implemented as recovery transitions in the state machine itself.
2. **External watchdog process** for hard faults: runs as a `systemd --user` service (or a small supervisor script), monitors a heartbeat file the framework writes every N seconds, restarts the framework process when the heartbeat is stale beyond a configured threshold. The watchdog is dumb on purpose — it cannot, itself, decide *what* state to recover to; it only restarts.

### Consequences

- The framework can crash hard and the operator wakes up to "service restarted N times overnight" instead of "service has been dead for 8 hours."
- The watchdog is itself a small, audited piece of code with no dependencies beyond the standard library — small enough not to need its own watchdog.

### Risks

- A pathological state could put the framework in a crash-loop. Mitigation: the watchdog tracks restart frequency; if restarts exceed a threshold within a window, it halts and emits a notification rather than continuing to flap.

### Rejected alternatives — why

- **In-process supervisor only**: cannot recover from segfaults or hung loops, which are exactly the cases where supervision is most needed.
- **`systemd` system unit**: would require root for installation; user-level systemd is sufficient and respects the principle of least privilege.

---

## ADR-12 — Observability: structured JSON logs + metrics file + artifact store

### Context

Long-run automation either succeeds invisibly or fails subtly. Diagnosing subtle failures days after the fact requires logs you can grep, metrics you can graph, and screenshots you can look at.

### Decision

Three concurrent observability surfaces:

1. **Structured JSON logs** — every state transition, every match attempt, every ADB call, with timestamps, durations, and correlation IDs (one per tick).
2. **Metrics file** — a Prometheus-text-format file the framework rewrites every N seconds, exposing counters, gauges, and histograms (tick latency, match counts by template, ADB error rate, watchdog restarts). Prometheus is *optional*; the file is human-readable on its own.
3. **Artifact store** — on every failed match below a configured criticality, the framework saves the captured frame, the offending template, and a side-by-side debug image to a rotating directory.

### Consequences

- Disk usage from artifacts grows under failure conditions. Mitigation: rotation policy (max N artifacts, total cap in MB) enforced by the artifact writer itself.
- The logs are voluminous in JSON form. Mitigation: log level controls verbosity; the structured form is greppable and aggregatable.

### Rejected alternatives — why

- **`print` debugging**: not auditable after the fact.
- **Logs only**: cannot answer "is the match rate decaying over time" without metrics.
- **Metrics only**: cannot answer "what specifically went wrong at 03:17 last night" without logs and artifacts.

---

## ADR-13 — Configuration: layered TOML + environment overrides, no runtime mutation

### Context

The framework has tunable knobs (thresholds, timeouts, paths, feature flags). Configuration discipline matters because misconfiguration is one of the more common operational failure modes.

### Decision

Configuration is a layered merge: built-in defaults → project TOML file → `~/.config/...` user TOML → environment variable overrides for select keys. The resolved config is **immutable** after load; nothing in the runtime mutates it. Hot reload is explicitly out of scope for v1.

### Consequences

- Configuration is reproducible — operators can serialize the resolved config to a file as a side-output for incident postmortems.
- "Why did it behave differently today" is answerable from the resolved-config artifact.

### Risks

- Operators may expect hot reload. Mitigation: documented as out-of-scope; a restart cycles config and the watchdog (ADR-11) makes that cheap.

### Rejected alternatives — why

- **YAML**: more powerful but its sharp edges (string vs number coercion, indentation sensitivity) introduce bugs that TOML's strict typing avoids.
- **JSON**: no comments, less readable for configuration with explanations.
- **Python files as config**: lets users execute arbitrary code in their config and conflates the config schema with the import system; rejected on safety and reproducibility grounds.

---

## ADR-14 — Testing: recorded-trace replay harness as the primary integration surface

### Context

Integration testing automation that talks to a real device requires a real device, which is expensive in CI and slow in development. Most integration bugs are in THINK (vision) logic and the state machine, both of which can be exercised offline if SENSE produces realistic data.

### Decision

Build a **recorded-trace replay harness**. A "trace" is a captured sequence of frames (with timestamps, optional UI events, and the actions taken at the time). The harness replays SENSE events into the framework, captures the actions THINK + ACT would emit, and diffs them against the recorded actions. Real-device tests remain, but on a tight smoke-test scope.

### Consequences

- CV regression suite runs in seconds against a corpus of recorded traces.
- New game/app screens captured during operation become future test fixtures automatically.
- The action engine is mocked in the harness; only its *intent* (action type, normalized coords, timing) is validated.

### Risks

- A trace captured on device X may not exercise behavior on device Y. Mitigation: traces are tagged with their capture device profile; the matrix of trace × device profile is part of the test plan.
- The harness can drift from reality if not updated. Mitigation: a sampling of traces are re-captured monthly on real devices.

### Rejected alternatives — why

- **Live device CI only**: slow, flaky, expensive, and the device becomes a maintenance burden.
- **Mocked everything**: misses the integration surface that matters most (CV against realistic frame data).

---

## ADR-15 — Anti-fragility: bounded behavioral jitter, opt-in per action class

### Context

Deterministic timing and identical-pixel taps over many hours are a signature of automated interaction. They are also operationally fragile: a UI animation that completes 50 ms later than expected can cause a deterministic loop to mis-tap. Bounded randomness improves *robustness*, independent of any detection considerations.

### Decision

Every action class declares a jitter envelope: a `delay_ms` distribution (uniform within a bounded range) and a `coord_px` dispersion (Gaussian within a bounded radius around the target). The envelope is per-action-class so that a "high precision" action (e.g. a tap near a small toggle) can opt for tighter bounds than a "broad area" action (e.g. dismissing a modal).

### Consequences

- The action engine is no longer pure-deterministic. Tests assert *intent* (target normalized coords + jitter envelope) rather than *exact* outgoing coordinates.
- Loop timing is also jittered, with a small bounded delay between observation cycles. This prevents accidental beat-locking with on-device animation cycles.

### Risks

- Excessive jitter can cause misses on tight targets. Mitigation: per-action envelope is bounded explicitly, defaults conservative.
- Non-determinism complicates debugging. Mitigation: the RNG is seeded from a logged value per session; replay reproduces the exact sequence.

### Rejected alternatives — why

- **Pure determinism**: structurally fragile against UI timing variance, regardless of detection considerations.
- **Unbounded randomness**: makes targets unreliable and is a footgun.

---

## ADR-16 — Python version & dependency strategy

### Context

Python projects rot quickly when version and dependency hygiene is neglected.

### Decision

- **Target Python 3.11+** as the minimum. 3.11's exception groups and improved asyncio diagnostics are leveraged.
- **Use `uv` or `pip-tools` with a pinned lockfile**. No bare `pip install`, no system Python.
- **Project lives in a virtualenv** under the repo. The bootstrap script (Phase 1) creates and validates it.
- **OpenCV is `opencv-python-headless`** (no GUI deps); rendering of debug artifacts uses the headless image-encoding path.
- **No dependencies on packages that have not seen a release in the last 18 months** unless explicitly justified in a new ADR.

### Consequences

- A fresh clone reproduces the environment deterministically.
- Security updates are observable: the lockfile is the audit surface.

### Risks

- `opencv-python-headless` and `opencv-python` are mutually exclusive on PYTHONPATH. Mitigation: the bootstrap script refuses to proceed if both are present.

### Rejected alternatives — why

- **System Python**: cross-distro variance and surprise upgrades break us.
- **No lockfile**: nondeterministic builds, "works on my machine" support burden.

---

## End of ADR

Future decisions append to this document with the next ADR number. Reversed decisions are recorded as new ADRs that **supersede** the prior decision; the prior ADR is annotated `Status: Superseded by ADR-NN` but is not deleted.
