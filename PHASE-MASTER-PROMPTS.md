# Phase Master Prompts

> **Document type:** Implementation prompts for a coding agent (Claude CLI or equivalent)
> **Audience:** A senior engineer or coding agent executing one phase at a time
> **Companion documents:** [SYSTEM-ROADMAP.md](./SYSTEM-ROADMAP.md), [ADR.md](./ADR.md), [ARCHITECTURE-DIAGRAMS.md](./ARCHITECTURE-DIAGRAMS.md), [DESIGN-REVIEW.md](./DESIGN-REVIEW.md)

Each phase prompt below is **independently executable** — it does not assume continuity from a prior conversation. It assumes the executor has local repository access, a Linux desktop, an Android device with USB debugging, and the four companion documents above. The prompts embed quality bars, prohibitions, and validation criteria; reading the companion documents is mandatory but not sufficient — the prompt itself is authoritative for what to deliver.

**Hard rules common to every phase:**

- **No placeholder code.** No `TODO`, no `pass  # implement later`, no `raise NotImplementedError`. Every code path either works or is explicitly out of scope and not introduced.
- **No shortcuts.** Do not skip tests "for now," do not commit dead code, do not import a dependency you do not use.
- **Read before writing.** Always read [SYSTEM-ROADMAP.md](./SYSTEM-ROADMAP.md), [ADR.md](./ADR.md), and [ARCHITECTURE-DIAGRAMS.md](./ARCHITECTURE-DIAGRAMS.md) before writing code. Cite the ADR you are implementing when introducing a new module.
- **Asyncio discipline.** No blocking calls on the event loop. ADB subprocesses go through the bounded thread pool. CV work runs inline (acceptable per ADR-07) but profile any operation > 50 ms.
- **Typing is mandatory.** All public functions are annotated. Run `mypy --strict` clean on the modules you touch.
- **Formatting.** `ruff format` + `ruff check` clean. No exceptions silenced without a comment that names the reason.
- **Logging discipline.** Use the structured logger introduced in Phase 6. Before Phase 6, log via `logging.getLogger(__name__)` with one-line messages — do not use `print`.
- **Configuration discipline.** No magic numbers in code. Tunables go through the config system. Constants that *are* part of the architecture (e.g. the reference resolution `(1080, 1920)`) live in a single `constants.py`.
- **Tests live with code.** Each module gets a sibling test module. Run the test suite before claiming completion.
- **Commits.** Small, atomic, with descriptive messages. One module per commit is the default.
- **Documentation.** Every public class and function has a one-paragraph docstring explaining *intent and contract*, not mechanism. No docstrings on private functions unless non-obvious.
- **Honest reporting.** At phase end, write the phase report (deliverable) covering what was done, what was *not* done and why, and any deviation from the dossier with rationale.

---

## Phase 0 — Research & feasibility

### Master prompt

You are implementing **Phase 0 — Research & feasibility** of the Android UI Automation Framework. Before you begin, read the following companion documents in full:

1. `SYSTEM-ROADMAP.md` — focus on §3 (NFRs), §5.1 (ADB), §5.2 (SENSE), §7 (costs), §8 (accuracy).
2. `ADR.md` — focus on ADR-01, ADR-02, ADR-03, ADR-04, ADR-15, ADR-16.
3. `ARCHITECTURE-DIAGRAMS.md` — focus on §7 (SENSE pipeline detail).

**Goal.** Validate the engineering assumptions embedded in the dossier against the operator's actual hardware. Produce measurements that either confirm or revise the ADRs. Do **not** start building the framework — that is Phase 1's work.

**Scope.** Build a throwaway bench harness under `bench/` and produce `phase-0-report.md` at the repo root. The harness need not be production-quality; the report must be. Both artifacts ship in the repo for later regression and audit.

**Concrete responsibilities.**

1. Verify the operator's host meets the prerequisites (Python 3.11+, `adb` 34.0+, a connected device). If not, document the gap and stop.
2. Build a small Python script (`bench/screencap_bench.py`) that takes N captures (default 200) across the four modes — `screencap+pull`, `exec-out screencap -p` (PNG), `exec-out screencap` (raw), and an optional `minicap` mode if the operator opts in. Measure end-to-end latency from request to a fully-decoded NumPy `ndarray`. Report mean, median, p95, p99, and standard deviation per mode. Capture the device profile (`adb shell getprop ro.build.version.release`, `ro.product.model`, resolution from `dumpsys window | grep mUnrestricted`).
3. Build `bench/match_bench.py` that loads a representative seed template (one PNG you create from a captured frame), runs `cv2.matchTemplate` against the captured frame at 1080×1920 baseline, both full-frame and ROI-restricted, in BGR and grayscale. Report timing percentiles over ≥ 500 iterations.
4. Build `bench/adb_overhead_bench.py` that measures `adb shell echo hi` round-trip latency over 200 invocations as a proxy for subprocess + JNI bootstrap cost.
5. Verify the **raw screencap header layout** on the operator's device. Capture one raw screencap to a file, dump the first 16 bytes in hex in your report, and identify the width / height / pixel-format fields against the documented layout. If the layout differs from the documented `uint32 width, uint32 height, uint32 format[, uint32 colorSpace]`, escalate in the report.
6. Test USB autosuspend behavior: leave the device plugged in idle for 5 minutes and confirm `adb devices` continues to list the device. If not, document the kernel/USB power-management setting needed (`/sys/bus/usb/devices/.../power/control`).
7. Optionally — only if the operator has both USB 2.0 and USB 3.0 ports — bench the screencap pipelines on each and tabulate.

**Deliverables.**

- `bench/screencap_bench.py`, `bench/match_bench.py`, `bench/adb_overhead_bench.py` — each callable as `python -m bench.<name>` and producing a CSV under `bench/results/`.
- `bench/results/*.csv` — committed for reproducibility.
- `phase-0-report.md` — a markdown report including a one-paragraph executive summary, a section per benchmark with tabulated results, a section confirming or proposing revisions to each of ADR-01, ADR-02, ADR-03, ADR-04, ADR-16, and an explicit checklist of which NFRs from §3 of SYSTEM-ROADMAP are achievable on this hardware, which require revision, and which require further investigation.

**Quality bar.**

- Bench scripts must be deterministic in their data layout: same script run twice produces same CSV columns, sorted same way.
- Each bench writes its CSV atomically (write to `.tmp`, rename) to prevent partial files on Ctrl-C.
- Timing uses `time.perf_counter_ns()` only; no `time.time()`, no microsecond truncation.
- A bench run should be reproducible from the CSV alone: include columns for `device_serial`, `device_model`, `usb_speed` (best-effort detection), `host_cpu`, `host_kernel`, `bench_version`.
- The report must distinguish *verified facts* from *uncertain estimates* using the label conventions in §0 of SYSTEM-ROADMAP.

**Testing.**

- Bench scripts do not need unit tests; they are throwaway and the CSV is the truth surface.
- The report includes the actual CSVs (or links to them) so any claim is auditable.

**Failure handling.**

- If a bench cannot run (e.g. device disconnects mid-run), the script must exit non-zero with a clear stderr message naming the failed iteration count. The CSV must end on a complete row, not a partial.
- If a benchmark reveals a fact that invalidates an ADR, **stop and propose a new ADR** in `phase-0-report.md`. Do **not** silently proceed with implementation against the invalidated assumption.

**Prohibitions.**

- Do not start implementing the framework proper.
- Do not introduce dependencies beyond what the benches require (`opencv-python-headless`, `numpy`).
- Do not write production-quality wrappers around `subprocess.run` — bench code can be ugly; production code starts in Phase 1.
- Do not record measurements on a different device than the operator's target.

**Reporting.** Phase report (`phase-0-report.md`) must answer: *Are the NFRs achievable on this hardware? Which ADRs hold and which need revision? What is the recommended primary screenshot pipeline, with measured numbers?* If the answer is uncomfortable, write that down honestly.

**Exit criteria.** All items in §Deliverables present; report explicitly accepts or proposes revisions for each ADR listed above.

---

## Phase 1 — Environment & ADB foundation

> **Phase 0.5 reality sync (2026-05-20):** this prompt was updated to
> reflect Phase 0 measurements, the frozen v1.0 NFRs, ADR-01a's
> USB-link-speed validation requirement, and the revised latency
> envelopes. The original prompt's scope, deliverables, and exit
> criteria are intact; load-bearing additions are clearly marked
> **(Phase 0.5)**.

### Master prompt

You are implementing **Phase 1 — Environment & ADB foundation**. Read in full:

- `SYSTEM-ROADMAP.md` §3.1 (frozen NFRs), §5.1 (ADB incl. §5.1.7 USB link-speed validation), §6 (setup), §11 (FSM — for BOOTSTRAP, CONNECTING, FAULTED).
- `ADR.md` ADR-01a (Phase-0.5 screenshot-pipeline revision; load-bearing for the bootstrap USB check), ADR-07, ADR-11, ADR-13, ADR-16.
- `phase-0-report.md` from the prior phase — the measurements.
- `docs/frozen_nfrs_v1.md` — the v1.0 NFR targets. Build against these, not the pre-Phase-0 estimates that may still appear in some sections of SYSTEM-ROADMAP for historical reference.
- `docs/phase0_consistency_audit.md` — the catalogue of stale claims and where they were resolved.
- `docs/phase1_readiness.md` — the gate review. **If this document does not say "READY", stop and resolve.**

**Goal.** Produce a reproducible Python environment, a clean `ADBClient` wrapper, a device fingerprinting routine, systemd user units (framework + watchdog stub), and a `scripts/bootstrap.sh` that brings a fresh checkout to "device-ready" in one command. **No CV, no FSM logic beyond BOOTSTRAP and CONNECTING, no action engine, no observability beyond standard logging.**

**Repository layout you will create.**

```
.
├── ADR.md                         (already exists)
├── ARCHITECTURE-DIAGRAMS.md       (already exists)
├── SYSTEM-ROADMAP.md              (already exists)
├── PHASE-MASTER-PROMPTS.md        (already exists)
├── DESIGN-REVIEW.md               (already exists)
├── phase-0-report.md              (already exists)
├── pyproject.toml
├── uv.lock                        (or requirements.lock if pip-tools)
├── scripts/
│   └── bootstrap.sh
├── systemd/
│   ├── automation.service
│   └── automation-watchdog.service
├── config/
│   └── runtime.toml
├── automation/
│   ├── __init__.py
│   ├── constants.py
│   ├── config.py
│   ├── adb.py
│   ├── fingerprint.py
│   ├── cli/
│   │   ├── __init__.py
│   │   └── probe.py
│   └── errors.py
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── test_adb.py
│   └── test_config.py
└── var/                           (created by bootstrap; .gitignore'd)
```

**Concrete responsibilities.**

1. `pyproject.toml` declaring the project, with `[project.dependencies]` initially listing only `numpy` (CV comes Phase 2). Python 3.11+. Use `uv` if available; fall back to `pip-tools`. Produce a deterministic lockfile committed alongside.
2. `scripts/bootstrap.sh` — bash, idempotent. Verifies Python and `adb` versions; creates `./.venv/`; installs locked dependencies; creates `var/log`, `var/metrics`, `var/artifacts`, `var/run`; refuses to proceed on version mismatch with a clear error. **(Phase 0.5)** Also performs USB link-speed validation per `SYSTEM-ROADMAP.md §5.1.7`:
   - After `adb devices` confirms a connected device with state `device`, walk `/sys/bus/usb/devices/*` to find the entry whose `serial` attribute matches the adb serial.
   - Read that entry's `speed` file.
   - If `speed` is `480` or higher → log INFO `USB link speed: ${speed} Mbps OK` and proceed.
   - If `speed` is `12` (USB 1.1 FS) or `1.5` (USB LS) → log ERROR `USB link speed: ${speed} Mbps too slow — device is likely plugged through a full-speed hub. Replug directly into a USB 2.0 high-speed (or 3.x) port.` and exit non-zero.
   - If the sysfs entry cannot be located → log WARN `cannot verify USB link speed (sysfs path not resolvable for serial ${serial})` and proceed.
   - The check is wrapped so an absence of `/sys/bus/usb/devices` (e.g. an unusual container environment) does not break bootstrap.
3. `automation/constants.py` — single source of truth for `REFERENCE_RESOLUTION = (1080, 1920)` and other architectural constants.
4. `automation/config.py` — TOML loader implementing the layered config (ADR-13). Resolved config is a frozen dataclass. Includes a serializer that writes `var/run/effective-config.toml`.
5. `automation/errors.py` — exception hierarchy: `AutomationError` (base), `ADBError`, `DeviceUnauthorizedError`, `DeviceNotFoundError`, `ConfigError`, `FingerprintError`.
6. `automation/adb.py` — `ADBClient` class. Methods: `devices()`, `shell(cmd: list[str])`, `exec_out(cmd: list[str]) -> bytes`, `pull(remote: str, local: Path)`, `push(local: Path, remote: str)`, `wait_for_device(timeout: float)`, `kill_server()`, `start_server()`, plus a private `_run(args: list[str]) -> CompletedProcess`. All subprocess calls go through a bounded thread pool (`asyncio.to_thread` is fine). Output is parsed defensively; `adb devices` parsing tolerates extra header/footer lines.
7. `automation/fingerprint.py` — `DeviceFingerprint` dataclass and a `fingerprint(adb: ADBClient) -> DeviceFingerprint` async function. Fields: `serial`, `model`, `android_version`, `sdk_int`, `resolution` (W, H), `orientation` (portrait/landscape), **(Phase 0.5)** `usb_speed_mbps: int | None` (None when sysfs unavailable). Resolution is parsed from `dumpsys window` and orientation from `dumpsys input` (or `settings get system user_rotation`). USB speed is read from `/sys/bus/usb/devices/<path>/speed` resolved by matching the device's `serial` sysfs attribute. Includes a sanity check: refuses to fingerprint if `resolution` is missing or zero.
8. `automation/cli/probe.py` — a CLI entry point: `python -m automation.cli probe`. Loads config, instantiates `ADBClient`, runs `devices()`, fingerprints, prints a human-readable summary **including the USB link speed**, exits 0 on success and non-zero with a diagnostic on failure. The summary explicitly highlights link speed below 480 Mbps in red/with a warning prefix.
9. `systemd/automation.service` — user unit, type=`simple`, no `Restart=` (watchdog owns restart). Working directory = repo root. ExecStart = the framework's main entrypoint (stub for Phase 1; emits a "Phase 1 stub" log line and exits).
10. `systemd/automation-watchdog.service` — user unit, type=`simple`. Phase 1 stub watchdog that simply logs once per minute that it is alive. Real watchdog logic lands in Phase 7.
11. Tests:
    - `tests/test_config.py` — covers default load, layered overrides, environment overrides, malformed TOML rejection.
    - `tests/test_adb.py` — covers `devices()` parsing against fixtures of stdout (authorized, unauthorized, no permissions, no devices); covers exit-code handling. Live-device tests are marked `@pytest.mark.live` and skip if `ADB_LIVE` env var is not set.
12. `config/runtime.toml` — committed minimal config with sane defaults, top-level comments explaining each section.

**Quality bar.**

- `ADBClient` is small (< 300 LOC). No clever inheritance. One subprocess wrapper, methods around it.
- Every subprocess call has a configurable timeout; default 10 s. Exceeded timeout raises `ADBError`.
- Every parsing function fails loud — no silent return of `None` when something is missing.
- `mypy --strict` clean.
- `ruff check` clean.
- All tests pass without a connected device (the live tests are explicitly skipped).

**Testing.**

- Unit tests use fixture strings for ADB output. Place fixtures in `tests/fixtures/adb/`.
- **(Phase 0.5)** Unit tests for USB link-speed parsing use synthetic sysfs trees (e.g. via `tmp_path` fixtures that create a fake `/sys/bus/usb/devices/<x>/speed` and `serial` pair). Cover: `480` accepted, `12` rejected, missing `speed` file warned-then-proceeded, multiple-device sysfs disambiguated by serial.
- Live tests (when `ADB_LIVE=1`): probe a real device, fingerprint, log results.
- Tests run in CI as well as locally; CI invocation in a follow-up phase, but the tests must be CI-shaped today.

**Failure handling.**

- The probe CLI exits with a structured error message and a non-zero exit code when the device is unauthorized / missing / unauthorized.
- The bootstrap script's failures all produce one-line, actionable error messages (e.g. "ERROR: adb not found in PATH. Install with 'sudo apt install android-tools-adb' on Debian/Ubuntu.").

**Prohibitions.**

- No CV code yet — do not even import `cv2`.
- No real watchdog implementation.
- No framework "main loop" — only the stub entry point.
- No `print` statements in production code (CLI may use `print` for human-readable output only).
- No `requests`, `aiohttp`, or any networking lib.

**Reporting.** Phase report `phase-1-report.md` covering: what was delivered, deviations from the dossier (with rationale), open questions for Phase 2, the `mypy`/`ruff`/test output as appendices.

**Exit criteria.**

- `git clone` → `bash scripts/bootstrap.sh` → `source .venv/bin/activate` → `python -m automation.cli probe` correctly prints device fingerprint **including USB link speed** on a connected device at ≥ 480 Mbps.
- `bootstrap.sh` against a device at 12 Mbps (simulate by plugging through a USB 1.1 hub or by injecting a sysfs fixture in a unit test) exits non-zero with the documented remediation message.
- All tests pass.
- `mypy --strict` and `ruff check` clean.
- systemd units install (`bootstrap.sh --install-service`) and uninstall (`--uninstall-service`) cleanly.

---

## Phase 2 — Screenshot pipeline

### Master prompt

You are implementing **Phase 2 — Screenshot pipeline**. Read in full:

- `SYSTEM-ROADMAP.md` §5.2.
- `ADR.md` ADR-01, ADR-02, ADR-04, ADR-05.
- `ARCHITECTURE-DIAGRAMS.md` §7.
- `phase-0-report.md` for the measured numbers your implementation must meet.

**Goal.** A `Sensor` abstraction delivering `Frame` objects at reference resolution, with primary (raw screencap) and fallback (PNG) backends and a remap step. **No CV matching, no actions, no FSM beyond CALIBRATING and READY.**

**Concrete responsibilities.**

1. Add `opencv-python-headless` to the project deps. Rebuild the lockfile. **Reject** if `opencv-python` (the GUI variant) is also installed — bootstrap checks for this.
2. `automation/sense/__init__.py`
3. `automation/sense/frame.py` — `Frame` dataclass (immutable). Fields: `pixels` (NumPy `ndarray`, shape `(H_ref, W_ref, 3)`, dtype `uint8`, BGR), `tick_id` (str), `captured_at` (datetime UTC), `capture_duration_ms` (float), `source_resolution` (W, H), `device_serial` (str). `Frame` is the *only* shape passed to THINK; downstream cannot reach into the raw bytes.
4. `automation/sense/raw_parser.py` — pure function `parse_raw_screencap(buf: bytes) -> ParsedFrame`. Parses the documented header (`width: uint32 LE, height: uint32 LE, pixel_format: uint32 LE[, colorSpace: uint32 LE on Android 9+]`). Returns the pixels and shape. Supports `PIXEL_FORMAT_RGBA_8888` (1) at minimum; other formats raise `UnsupportedPixelFormatError`. Unit-tested against fixtures captured during Phase 0.
5. `automation/sense/png_decoder.py` — thin wrapper around `cv2.imdecode` for the fallback path.
6. `automation/sense/remap.py` — `Remap` dataclass. Given device resolution and reference resolution, computes the affine transform (scale + offset for letterboxing). Methods: `to_reference(frame_pixels: ndarray) -> ndarray`, `from_reference_coords(x_norm: float, y_norm: float) -> (x_dev: int, y_dev: int)`, `to_reference_coords(x_dev: int, y_dev: int) -> (x_norm: float, y_norm: float)`.
7. `automation/sense/sensor.py` — `Sensor` abstract base + `ADBSensor` concrete implementation. Public method: `async capture(tick_id: str) -> Frame`. Internally:
   - First call per session: probe both modes, prefer raw, fall back to PNG. The chosen mode is sticky for the session.
   - Per call: invoke ADB, parse, convert RGBA→BGR via array indexing (`buf[:, :, [2, 1, 0]]`), remap, build `Frame`.
   - Records `capture_duration_ms` precisely.
   - On exception inside the parser, logs at WARN, switches mode to PNG for the rest of the session, and emits a metric event (metric infra will come Phase 6 — for now, use the standard logger with a structured extra field).
8. `MockSensor` in `automation/sense/mock.py` — for tests. Accepts a directory of PNGs and returns them in sequence as `Frame` objects.
9. FSM extension: add `CALIBRATING` and `READY` states (per §11 of SYSTEM-ROADMAP). The orchestrator is still skeletal; just enough that after `CONNECTING` succeeds, the system fingerprints, builds the Remap, takes one successful capture, and reaches READY.
10. Tests:
    - `tests/sense/test_raw_parser.py` — fixtures from Phase 0 captures; parametrized by device profile.
    - `tests/sense/test_remap.py` — round-trip coordinate tests; letterboxing edge cases; rejection of zero or negative resolutions.
    - `tests/sense/test_sensor.py` — MockSensor sequence test; fallback-on-parse-error test.
    - Live test (skipped by default): 200-capture soak on a real device; asserts median latency within Phase 0's measured numbers + 50% headroom.

**Quality bar.**

- `Sensor` is async; capture latency is measured at the `Sensor` boundary, not deeper, to keep the measurement honest about asyncio scheduling.
- Memory: `Frame` objects own their pixel buffer; no shared views.
- Frames are short-lived; one frame's lifetime ends when the next tick begins. Do not cache frames beyond the current tick.
- Reference-resolution conversion uses `cv2.resize` with `INTER_AREA` for downsampling and `INTER_LINEAR` for upsampling. Code must select the right one based on source size.

**Failure handling.**

- Parser fails: log + auto-fallback to PNG + emit event.
- ADB returns empty bytes: `EmptyCaptureError` raised; orchestrator counts toward capture-error budget.
- ADB exits non-zero: `ADBError` raised, escalated to the recovery cascade in later phases.

**Prohibitions.**

- No template matching code.
- No actions.
- No image transformations beyond RGBA→BGR and resize (no histogram equalization, no gamma correction).
- Do not couple `Sensor` to FSM internals; `Sensor` is consumed *by* the orchestrator, not aware of it.

**Reporting.** `phase-2-report.md`: deliverables, soak-test results, deviations, open questions for Phase 3.

**Exit criteria.**

- Soak test (1 hour @ 2 Hz capture) passes with zero unrecovered failures.
- `mypy --strict`, `ruff check`, tests all clean.
- Probe CLI now ends with "READY" instead of stopping at "CONNECTED."

---

## Phase 3 — Vision engine

### Master prompt

You are implementing **Phase 3 — Vision engine**. Read in full:

- `SYSTEM-ROADMAP.md` §5.3.
- `ADR.md` ADR-03, ADR-04, ADR-05, ADR-10.
- `ARCHITECTURE-DIAGRAMS.md` §8.

**Goal.** A `Matcher` that produces calibrated `MatchResult`s for a set of templates against a `Frame`, with ROI restriction, masking, optional multi-scale fallback, and a content-addressed `TemplateManifest`. Plus the replay harness scaffold (ADR-14). **No actions, no FSM beyond what already exists.**

**Concrete responsibilities.**

1. `automation/think/__init__.py`.
2. `automation/think/template.py` — `Template` dataclass (frozen). Fields: `id` (str), `image` (ndarray, BGR or single-channel depending on `match_strategy`), `mask` (ndarray | None), `roi_norm` (tuple of 4 floats | None), `match_strategy` (enum: BGR / GRAYSCALE / CHANNEL_R / CHANNEL_G / CHANNEL_B), `hard_threshold` (float), `soft_threshold` (float), `multi_scale` (bool), `captured_metadata` (dict, opaque). Validators in `__post_init__` enforce: `0 < soft_threshold < hard_threshold ≤ 1`, mask shape matches image, ROI components in `[0, 1]` with `x0 < x1 ∧ y0 < y1`.
3. `automation/think/manifest.py` — `TemplateManifest`. Loads from `assets/templates/`. Each entry is `{id}.png` + `{id}.toml`. Optional mask: `{id}.mask.png`. Computes SHA-256 of each PNG for cache keying. Refuses to load on malformed TOML or PNG/metadata dimension mismatch.
4. `automation/think/preprocess.py` — `FramePreprocessor`. Methods to lazily compute and cache (per tick) grayscale, channel-extracted, and ROI-cropped views of the frame.
5. `automation/think/matcher.py` — `Matcher` abstract base, `OpenCVMatcher` concrete. Public method: `match(frame: Frame, templates: list[Template]) -> list[MatchResult]`.
    - For each template: select preprocessed source per `match_strategy`; restrict to ROI if set; `cv2.matchTemplate` with `TM_CCOEFF_NORMED` + mask; locate peak; gate against thresholds; build `MatchResult`.
    - Multi-scale fallback: if `multi_scale = True` and primary scale MISSes, retry at 0.9× and 1.1× scaled template (via `cv2.resize` on the *template*, not the frame); take best.
    - `MatchResult` fields: `template_id`, `status` (HIT / SOFT / MISS), `score` (float), `location_norm` (tuple[float, float] | None — center of match, in normalized coords), `duration_ms`, `scale_used` (float).
6. `automation/think/non_max_suppression.py` — optional NMS utility for "find all instances of X." Not used by default; provided as a building block.
7. Replay harness:
    - `tests/replay/harness.py` — given a trace directory (`{frame_001.png, frame_002.png, ..., trace.toml}`), drives the Matcher and the FSM (when integrated) and produces a comparison report.
    - `tests/replay/test_seed_traces.py` — pytest entry point.
    - `traces/seed/` — at least 3 traces with 5+ frames each, generated by capturing real interactions and hand-annotating the expected match outcomes per template per frame.
8. Seed template corpus: at minimum 5 templates representative of the target UI. Include one with a mask, one with a ROI, one in grayscale mode, one multi-scale.
9. Tests:
    - `tests/think/test_template.py` — validators, malformed metadata rejection.
    - `tests/think/test_manifest.py` — load corpus, hash stability, dimension mismatch rejection.
    - `tests/think/test_matcher.py` — synthetic frames with known templates at known locations; HIT/SOFT/MISS classification; multi-scale exercise.
    - `tests/think/test_preprocess.py` — cache correctness, lazy evaluation.

**Quality bar.**

- The matcher does not retain references to frame pixels beyond the call.
- All preprocessing is cached per-frame (per-tick), invalidated when a new frame arrives.
- Multi-scale fallback is opt-in *per template*; never global.
- Per-template latency tested in microbench (a `bench/match_corpus.py` script that runs every template in the manifest against a sample frame).

**Failure handling.**

- A single template's match exception does not abort the batch; the offending template returns `MatchResult(status=MISS, score=0, location_norm=None, duration_ms=elapsed)` and an error is logged at WARN with the template ID.
- Manifest load failure is fatal (refuse to start). This is a configuration error, not a runtime fault.

**Prohibitions.**

- No actions, no input.
- No FSM logic beyond what Phase 5 will introduce.
- Do not introduce ORB / SIFT / feature matching in v1. Multi-scale `matchTemplate` is the only scale tolerance.
- Do not call `cv2.matchTemplate` on the full frame when a template has a ROI hint. Cropping is mandatory in that case.

**Reporting.** `phase-3-report.md`: deliverables, microbench results, replay harness output, seed corpus description.

**Exit criteria.**

- Replay harness exercises ≥ 50 frames × 5+ templates with 100% expected classifications.
- Per-template median match latency ≤ 25 ms on the operator's host.
- Tests clean. `mypy --strict` clean. `ruff` clean.

---

## Phase 4 — Action engine

### Master prompt

You are implementing **Phase 4 — Action engine**. Read in full:

- `SYSTEM-ROADMAP.md` §5.4.
- `ADR.md` ADR-06, ADR-09, ADR-15.
- `ARCHITECTURE-DIAGRAMS.md` §9.

**Goal.** An `Actuator` that emits ADB inputs with proper jitter envelopes and coordinate denormalization. **No FSM yet; the actuator is exercised by direct test scripts.**

**Concrete responsibilities.**

1. `automation/act/__init__.py`.
2. `automation/act/classes.py` — `ActionRequest` dataclass + the action class enum. Subtypes: `TapRequest(coords_norm: tuple[float, float], class_id: str = "default")`, `SwipeRequest(start_norm, end_norm, duration_ms, class_id)`, `LongPressRequest(coords_norm, hold_ms, class_id)`, `KeyRequest(keycode: str)`, `TextRequest(text: str)`.
3. `automation/act/jitter.py` — `JitterEnvelope` dataclass; `Jitter.sample_tap(...)`, `Jitter.sample_swipe(...)` etc. RNG is seeded per session from a logged value (so replay is reproducible). Distributions are uniform for delays and Gaussian (truncated) for coordinate dispersion.
4. `automation/act/actuator.py` — `Actuator` abstract base + `ADBActuator`. Public method: `async execute(request: ActionRequest) -> ActionResult`. Internally: sample jitter envelope per the request's `class_id` (lookup table in config), denormalize coordinates via the session's `Remap`, await the pre-action delay, dispatch the right `adb shell input` command, await the post-action delay, build `ActionResult`. `ActionResult` includes `requested_norm`, `actuated_pixel`, `elapsed_ms`, `exit_code`, `error: str | None`.
5. `automation/act/policy.py` — `ActionPolicyRegistry` mapping `class_id` to envelope parameters loaded from config.
6. Config: extend `runtime.toml` with an `[action_classes]` section. At minimum: `default`, `precise`, `broad`.
7. Live test scripts:
    - `scripts/test_actions/tap_grid.py` — issue a 3×3 grid of taps with `precise` envelope and visually verify on-device.
    - `scripts/test_actions/swipe_set.py` — exercise 4 swipe directions and inspect the device.
8. Unit tests:
    - `tests/act/test_jitter.py` — distribution shape, bounds enforcement, seed reproducibility.
    - `tests/act/test_actuator.py` — mock ADBClient; assert correct command shapes for each action; assert jitter applied; assert denormalization correct against a known Remap.

**Quality bar.**

- Jitter is always applied; an `ActionRequest` with `jitter=False` requires an explicit override in code, not a default.
- Denormalization is one function — the place where the system stops being device-independent. Cover it in tests heavily.
- `Actuator` is async and respects cancellation: if the calling task is cancelled mid-delay, the in-flight delay aborts cleanly without leaving an orphan subprocess.

**Failure handling.**

- ADB exits non-zero: `ActionResult.error` is populated; the orchestrator (Phase 5) decides recovery.
- Coordinates outside `[0, 1]` raise `ValueError` *before* hitting ADB. Defensive.

**Prohibitions.**

- No minitouch / sendevent / scrcpy in v1. The interface accommodates them; the implementation does not.
- No "macro" recording features. The actuator handles one request at a time.
- No retry inside the actuator. Retry is the FSM's responsibility (Phase 5).

**Reporting.** `phase-4-report.md`.

**Exit criteria.**

- All action classes round-trip through the actuator and produce the expected ADB command shape (unit tests).
- 200 actions issued against a real device with 100% successful issuance (live test).
- Tests clean.

---

## Phase 5 — State machine

### Master prompt

You are implementing **Phase 5 — State machine**. Read in full:

- `SYSTEM-ROADMAP.md` §11.
- `ADR.md` ADR-07, ADR-08, ADR-11, ADR-15.
- `ARCHITECTURE-DIAGRAMS.md` §4, §5, §6.

**Goal.** A hand-rolled finite-state engine realizing §11 of SYSTEM-ROADMAP, integrated with Sensor, Matcher, and Actuator, capable of running a simple end-to-end interaction script. Recovery states implemented. Heartbeat file written at tick end.

**Concrete responsibilities.**

1. `automation/orchestrator/__init__.py`.
2. `automation/orchestrator/state.py` — `State` enum. Exactly the states listed in §11.1 of SYSTEM-ROADMAP.
3. `automation/orchestrator/transitions.py` — declarative transition table. Format: `dict[State, list[Transition]]` where each Transition has `event_predicate`, `target_state`, `side_effect_name`. The transition table is a Python literal — readable in one screenful.
4. `automation/orchestrator/engine.py` — `Orchestrator` class. Holds the current state, the transition table, retry counters per (state, fault_kind), per-state timeout timers, and references to Sensor/Matcher/Actuator. Methods: `async run(script: Script)`. Loop: while not in terminal state, run the tick for the current state, evaluate transitions, possibly transition, repeat.
5. `automation/orchestrator/tick.py` — per-state tick implementations. One function per state. Each function returns an `Event` that the transition table consumes.
6. `automation/orchestrator/recovery.py` — `RESET_LITE` and `RESET_HARD` implementations. RESET_LITE: send a configurable "safe action" (default `adb shell input keyevent KEYCODE_BACK`), recapture, see if a known "home" template matches. RESET_HARD: `adb kill-server` → `start-server` → re-enter CONNECTING.
7. `automation/orchestrator/heartbeat.py` — `HeartbeatWriter`. At the end of each tick, write the current monotonic-clock timestamp to `var/run/heartbeat`. Atomic write.
8. `automation/orchestrator/script.py` — `Script` is the user-authored definition of *what to do*. Minimal v1: a `Script` is a set of `Screen` declarations, where each `Screen` is `{templates_to_match, on_match, on_miss}` rules. The FSM consumes the active `Screen` and treats the current matches as inputs.
9. CLI extension: `python -m automation.cli run <script.toml>`. Loads the script, builds the orchestrator, runs to completion or until SIGTERM.
10. FSM-to-Mermaid exporter: `automation/orchestrator/export.py`. Outputs the state diagram to verify the implementation matches the diagram in `ARCHITECTURE-DIAGRAMS.md` §4.
11. Tests:
    - `tests/orchestrator/test_transitions.py` — transition table sanity (every state has an outgoing edge or is terminal; every state's outgoing events are disjoint; timeouts present where required).
    - `tests/orchestrator/test_engine.py` — drive the FSM with mocked Sensor/Matcher/Actuator through happy-path and fault-path scenarios.
    - `tests/orchestrator/test_recovery.py` — RESET_LITE happy path and escalation; RESET_HARD with mocked ADB.
    - `tests/orchestrator/test_export.py` — exported Mermaid is byte-equal to a committed reference.

**Quality bar.**

- The engine's main loop is one function. Read it top to bottom and the FSM is comprehensible.
- Retry counters reset on a clean transition through `OBSERVING`. Stale counters from a prior recovery do not contaminate the next attempt.
- Per-state timeouts are *enforced* — a state that hangs past its timeout transitions automatically to its declared `on_timeout`. Implement via `asyncio.wait_for`.
- Heartbeat is written **only at end-of-tick**, never mid-tick. A hung tick will not deceive the watchdog.

**Failure handling.**

- `FAULTED` is terminal. On entry: write a final artifact set (frame snapshot of the last capture, state dump), log at ERROR with the fault category, exit the process with a structured exit code (table of exit codes in the report).
- Unexpected exception in any tick handler: log at ERROR with stack, transition to RECOVERING.

**Prohibitions.**

- No state-machine library. Hand-rolled per ADR-08.
- No "policies" that mutate the transition table at runtime. The table is loaded once.
- No threads beyond the bounded pool from ADBClient. The FSM runs on the event loop.

**Reporting.** `phase-5-report.md`.

**Exit criteria.**

- A 30-minute soak with a 2-Screen script ("observe → tap → observe → tap → ...") runs without unrecovered fault.
- Exported Mermaid matches the diagram in `ARCHITECTURE-DIAGRAMS.md` §4.
- All recovery paths exercised at least once in tests.
- Tests clean.

---

## Phase 6 — Observability

### Master prompt

You are implementing **Phase 6 — Observability**. Read in full:

- `SYSTEM-ROADMAP.md` §5.6.
- `ADR.md` ADR-12, ADR-13.
- `ARCHITECTURE-DIAGRAMS.md` §1.

**Goal.** Production-grade observability: structured JSON logs, a Prometheus-text metrics file, an artifact store with rotation, and a `replay` CLI subcommand. Instrument all existing subsystems.

**Concrete responsibilities.**

1. `automation/observability/__init__.py`.
2. `automation/observability/log.py` — structured JSON logger. Sets up the root logger to write JSONL to `var/log/automation.jsonl` with rotation. Provides `bind_correlation_id(tick_id)` for per-tick context. Levels configurable.
3. `automation/observability/metrics.py` — `MetricsRegistry`. Provides `Counter`, `Gauge`, `Histogram` primitives. Bucket layouts:
    - Tick duration: 50, 100, 200, 400, 800, 1600, 3200, 6400 (ms).
    - Capture duration: 25, 50, 100, 200, 400, 800, 1600, 3200 (ms).
    - Match duration: 1, 2, 5, 10, 25, 50, 100, 250, 500 (ms).
    - Action duration: 50, 100, 200, 400, 800, 1600, 3200 (ms).
    Writes the Prometheus text exposition format to `var/metrics/metrics.prom` every 10 s (configurable). Writes atomically.
4. `automation/observability/artifacts.py` — `ArtifactStore`. Methods: `save_failure(tick_id, frame, template, score, debug_image)`, `save_validation_failure(tick_id, frame, expected_template)`. Rotation: keep the last 500 artifacts up to 500 MB; whichever cap hits first. Disk-space circuit breaker: if `df` on the artifacts partition reports < 1 GB free, the store goes into a "drop" mode that logs but does not write, until space is reclaimed.
5. `automation/observability/correlation.py` — `correlation_id_var` (`ContextVar`). Every log line and every metric labeled with it.
6. Instrumentation:
    - `Sensor`: counters `screen_capture_total{mode,outcome}`, histogram `screen_capture_duration_seconds_bucket{mode}`.
    - `Matcher`: per-template counters and histograms; gauge `template_match_score{template_id}` with last score.
    - `Actuator`: counters `action_total{action_class,outcome}`, histogram `action_duration_seconds_bucket{action_class}`.
    - `Orchestrator`: counters `tick_total{state}`, `recovery_total{kind}`, `state_transition_total{from,to}`, histogram `tick_duration_seconds_bucket{state}`.
    - `ADBClient`: counter `adb_error_total{kind}`, histogram `adb_call_duration_seconds_bucket{command}`.
    - `HeartbeatWriter`: gauge `heartbeat_last_written_ts`.
7. `automation/cli/replay.py` — `python -m automation.cli replay <tick_id>` subcommand. Reconstructs a tick from artifacts and logs, re-runs THINK against the saved frame, prints a comparison report.
8. Tests:
    - `tests/observability/test_log.py` — JSONL shape, rotation, correlation ID.
    - `tests/observability/test_metrics.py` — counter/gauge/histogram semantics; bucket boundaries; atomic write.
    - `tests/observability/test_artifacts.py` — rotation policy, disk-space circuit breaker (mock `shutil.disk_usage`).

**Quality bar.**

- Logging overhead < 1% of per-tick time at default verbosity. Profile during the Phase 6 soak.
- Metrics file is valid Prometheus text (`promtool check metrics` if available, or a unit test that parses it back).
- Artifacts are reproducible: a saved frame + saved template should be sufficient for a replay run to produce the same match score, modulo OpenCV version drift.

**Failure handling.**

- Log write failures (disk full): drop logs, increment `log_drop_total`, emit a single WARN, do not let logging crash the framework.
- Metric write failures: same.
- Artifact write failures: degrade per the circuit breaker.

**Prohibitions.**

- No network observability. Files only.
- No log formatting that requires non-stdlib JSON encoding.
- No structured-log frameworks (`structlog`, `loguru`). Stdlib `logging` plus a small JSON formatter.

**Reporting.** `phase-6-report.md`.

**Exit criteria.**

- 1-hour soak produces correctly-shaped logs, metrics, and (under fault injection) artifacts.
- Replay CLI reproduces a tick from artifacts.
- Disk-space circuit breaker triggers under simulated low-disk.
- All tests clean.

---

## Phase 7 — Hardening

### Master prompt

You are implementing **Phase 7 — Hardening**. Read in full:

- `SYSTEM-ROADMAP.md` §5.7, §9 (risks), §13 (adversarial review).
- `ADR.md` ADR-11.
- `ARCHITECTURE-DIAGRAMS.md` §5, §6.

**Goal.** Long-run reliability: real external watchdog, fault-injection harness, soak tests, configuration sanity checks. The framework should now survive 24 hours of operation on real hardware with induced faults.

**Concrete responsibilities.**

1. `watchdog/watchdog.py` — replace the Phase 1 stub. Standalone Python script, stdlib only. Reads `var/run/heartbeat`, configurable stale threshold (default 30 s). On stale: `SIGTERM` the framework PID (from `var/run/automation.pid` written by the framework), wait `term_grace_s` (default 10 s), then `SIGKILL`. After restart-by-systemd, increment a counter file `var/run/watchdog-restarts.log`. On `> 5` restarts in 60 minutes (sliding window), enter `HALT` mode: stop signaling restart, emit a notification to `var/run/watchdog-alert.log`.
2. `systemd/automation-watchdog.service` — finalized unit, declares `Restart=on-failure` on itself only.
3. `systemd/automation.service` — finalized, no `Restart=` directive (watchdog owns restart). PID file declared.
4. `automation/main.py` — frames the framework's main entrypoint, writes the PID file, installs signal handlers (SIGTERM → graceful shutdown).
5. `tests/fault_injection/`:
    - `inject_adb_kill.py` — kills the adb server mid-tick; asserts RESET_HARD recovers.
    - `inject_usb_cycle.py` — simulates USB unplug-replug via udev event injection (or, if not feasible, a doc-only script with a manual checklist).
    - `inject_capture_corruption.py` — wraps the sensor to occasionally return malformed bytes; asserts auto-fallback path triggers.
    - `inject_disk_full.py` — fills the artifacts partition (in a sandbox); asserts circuit breaker triggers.
6. `tests/soak/` — a 24-hour soak script that runs a simple repeating script under randomized fault injection. Output is a structured report.
7. `automation/config.py` extension — configuration validator. Refuses startup if:
    - any `hard_threshold ≤ soft_threshold`.
    - artifact path on a filesystem with < 1 GB free.
    - logging level not in the allowed enum.
    - reference resolution not exactly `(1080, 1920)` in v1 (future versions may relax).
8. `OPERATIONS.md` — runbook covering: installation, start/stop, log inspection, template update workflow, common-failure diagnoses, alerts from the watchdog, recovery from `HALT`.

**Quality bar.**

- Watchdog is ≤ 200 LOC, no dependencies beyond stdlib.
- 24-hour soak completes with watchdog restart count under the ceiling.
- Fault injection harness produces a coverage matrix (fault × expected recovery × observed) in the soak report.

**Failure handling.**

- Watchdog detects framework PID is missing post-restart (e.g. SIGKILL race): falls back to scanning systemd for the unit's PID.
- HALT mode persists across systemd restart of the watchdog itself (via the alert file).

**Prohibitions.**

- No dependency on external process supervisors beyond systemd `--user`.
- No watchdog state in /tmp; persist in `var/run/` so a host reboot recovers cleanly.
- No "smart" recovery logic in the watchdog. Restart-only.

**Reporting.** `phase-7-report.md`, including the 24-hour soak report and fault-injection coverage matrix.

**Exit criteria.**

- 24-hour soak passes.
- All fault-injection scenarios produce expected recoveries.
- `OPERATIONS.md` reviewed by a second engineer (or the operator) and any feedback closed.

---

## Phase 8 — Deployment & long-run stability

### Master prompt

You are executing **Phase 8 — Deployment & long-run stability**. Read in full:

- `SYSTEM-ROADMAP.md` §6 (setup), §8 (accuracy), §12 (Phase 8).
- All companion documents — Phase 8 is a system test of the entire dossier.

**Goal.** Install the framework on the operator's host. Run a 7-day soak. Capture observations and feed them into the v1.1 backlog. **No new feature work in this phase.**

**Concrete responsibilities.**

1. On the operator's host (with the operator's permission): clone the repo, run `bootstrap.sh`, run `--install-service`. Verify the systemd units come up.
2. Configure the runtime TOML for the operator's specific device profile and the actual script they intend to run.
3. Walk the operator through `OPERATIONS.md` while seated next to them; capture every place they get confused and add a fix to a follow-up doc PR (do not edit `OPERATIONS.md` in this phase; capture issues in the v1.1 backlog).
4. Run the framework for 7 consecutive days. Each morning, inspect: `journalctl --user -u automation`, `var/log/automation.jsonl`, `var/metrics/metrics.prom`, `var/artifacts/`, watchdog restart counter.
5. Each evening, write a 1-paragraph daily log entry in `soak-7d.md`.
6. At the end of 7 days, produce `phase-8-report.md`:
    - reliability KPIs vs §3.3 of SYSTEM-ROADMAP (MTBF, MTTR-soft, MTTR-hard, watchdog restart count).
    - accuracy KPIs vs §8 of SYSTEM-ROADMAP (HIT / SOFT / MISS rates per template).
    - resource KPIs vs §3.2 (RAM, CPU, disk).
    - a list of every novel failure mode observed and its resolution status.
    - a v1.1 backlog of concrete improvements, sorted by priority.

**Quality bar.**

- Real measurements, not estimates, populate the report.
- Soak does **not** run unattended; the operator (or you) physically observes the device at least once per day.
- Any failure that puts the framework into HALT mode triggers a postmortem in the report.

**Failure handling.**

- If a critical issue surfaces (framework cannot recover, false positive on a destructive action), **stop the soak**, document, and either fix in Phase 8 (only if a one-line config change) or schedule for v1.1.

**Prohibitions.**

- No new features.
- No retroactive ADR amendments based on Phase 8 observations — feed those into v1.1 instead.
- No skipping daily log entries.

**Reporting.** `phase-8-report.md` plus the 7 daily entries in `soak-7d.md`.

**Exit criteria.**

- 7 days of operation completed.
- Reliability and accuracy NFRs either met or explicitly accepted as gaps in the v1.1 backlog.
- Operator signs off on `OPERATIONS.md`.

---

## Coordinating across phases

Each phase prompt above is callable independently. To run a full v1 build:

1. Execute prompts in order.
2. Each prompt's exit criteria gate the next prompt's entry criteria. Do not start Phase N+1 until Phase N's report is committed and the exit criteria are met.
3. Phase reports are part of the deliverable set. They are commit artifacts, not throwaway notes.
4. If a phase reveals a problem with the dossier itself, **stop**, file an ADR amendment (or a new ADR), and resume only after the dossier is consistent again.

The dossier is the source of truth. The prompts above implement it. If they diverge, the dossier wins and the prompts are updated to match.
