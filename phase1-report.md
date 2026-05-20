# Phase 1 Report — Environment, Bootstrap, Device Foundation

> **Phase:** 1 — Environment & ADB foundation
> **Date:** 2026-05-20
> **Host:** Ubuntu 24.04.4 LTS, kernel 6.17, AMD Ryzen 5 5500, Python 3.12.3, adb 35.0.0
> **Device:** Xiaomi 22095RA98C (Redmi Note 11R), Android 13, USB 2.0 @ 480 Mbps
> **Companion documents:** [phase-0-report.md](./phase-0-report.md), [docs/frozen_nfrs_v1.md](./docs/frozen_nfrs_v1.md), [ADR.md ADR-01a](./ADR.md#adr-01a--screenshot-pipeline-phase-0-reality-content-dependent-ordering-usb-link-speed-prerequisite)

---

## 1. What was built

**Python package** (`automation/`):

| File | Purpose | LOC |
|------|---------|----:|
| `automation/__init__.py` | Package marker + `__version__` | ~10 |
| `automation/errors.py` | Typed exception hierarchy: `AutomationError`, `BootstrapError`, `ADBError`, `DeviceNotFoundError`, `USBValidationError` | ~40 |
| `automation/paths.py` | Single source of truth for `ROOT` / `VAR` / `LOGS` / `METRICS` / `ARTIFACTS` / `TMP`; idempotent `ensure_runtime_dirs()` | ~35 |
| `automation/adb.py` | Thin `ADB` wrapper around the `adb` CLI: `adb_version`, `get_state`, `get_serialno`, `devices`, `shell`, `exec_out`. Timeout-aware, fails loud, no business logic | ~155 |
| `automation/fingerprint.py` | `DeviceFingerprint` dataclass + `fingerprint(adb)` + `find_usb_speed(serial, base)` sysfs walker | ~155 |
| `automation/bootstrap.py` | Python-side bootstrap, callable as `python -m automation.bootstrap`. Validates env, fingerprints the device, gates USB speed, ensures runtime dirs, prints summary | ~190 |

**Bootstrap script** (`scripts/bootstrap.sh`):

- Bash strict mode (`set -Eeuo pipefail`).
- Idempotent host-side checks: Python ≥ 3.11, adb ≥ 34, connected device (state `device`), USB link speed ≥ 480 Mbps (with sysfs walk).
- Creates `./.venv/` if missing.
- Installs locked deps (`requirements-lock.txt`) with `--no-deps` for determinism.
- Creates `var/{logs,metrics,artifacts,tmp}` (mkdir -p, idempotent).
- Prints a colourised checklist summary.
- Flags: `--strict-usb` (reject unverifiable sysfs), `--skip-deps` (skip pip install), `--no-color`.
- Distinct exit codes for distinct failure modes (2/3/4/5/6).

**Project metadata**:

- `pyproject.toml` — Python 3.11+, runtime deps (`numpy`, `opencv-python-headless`) with floor/ceiling pins, dev deps (`pytest`, `pytest-cov`), `automation-bootstrap` entry point, pytest config.
- `requirements-lock.txt` — exact pins: `numpy==2.4.6`, `opencv-python-headless==4.13.0.92`.
- `.env.example` — declared env-var overrides per ADR-13 (currently `AUTOMATION_LOG_LEVEL`, `AUTOMATION_ADB_BINARY`, `AUTOMATION_ADB_DEFAULT_TIMEOUT`, `AUTOMATION_ALLOW_UNVERIFIED_USB`).
- `.gitignore` extended for `.venv*/`, `var/`, `__pycache__/`, `.pytest_cache/`, `*.egg-info/`, `.env`.

**Test suite** (`tests/`):

| File | Coverage |
|------|---------|
| `tests/conftest.py` | Shared fixtures: `SubprocessRecorder` (mocks `subprocess.run`), `fake_sysfs` (tmp-path sysfs tree), `make_usb_entry` helper |
| `tests/test_errors.py` | Exception hierarchy + multi-base catch |
| `tests/test_paths.py` | Layout invariants + idempotent `ensure_runtime_dirs()` |
| `tests/test_adb.py` | ADB version parsing, `get_state`, `devices` (authorized / unauthorized / empty), `shell`, `exec_out`, timeout translation, missing-binary detection |
| `tests/test_fingerprint.py` | USB sysfs walk (480 / 12 / missing / multi-device / unparseable), `fingerprint()` end-to-end, dumpsys parsing, `to_human_summary()` |
| `tests/test_bootstrap.py` | Happy path, `main()` exit codes 0/2/3/4/5, the 12 Mbps rejection, the unverified-USB strict/lenient branches |

---

## 2. What was validated

### 2.1 Test suite

`pytest -ra` against the project venv:

```
============================= test session starts ==============================
platform linux -- Python 3.12.3, pytest-8.4.2, pluggy-1.6.0
rootdir: /home/emre/Downloads/Clash-Of-Clans-Otomation
configfile: pyproject.toml
testpaths: tests
plugins: cov-6.3.0
collected 38 items

tests/test_adb.py .............                                          [ 34%]
tests/test_bootstrap.py ........                                         [ 55%]
tests/test_errors.py ..                                                  [ 60%]
tests/test_fingerprint.py .............                                  [ 94%]
tests/test_paths.py ..                                                   [100%]

============================== 38 passed in 0.05s ==============================
```

All 38 tests pass. No real-device dependency; the suite runs cleanly in an
offline environment.

### 2.2 Live `scripts/bootstrap.sh` against the operator device

```
[INFO]  Phase 1 bootstrap starting (repo: /home/emre/Downloads/Clash-Of-Clans-Otomation)
[OK]    python 3.12.3
[OK]    adb 35.0.0-11411520
[OK]    device jfzxugsgnnvsrsg6 (state=device)
[OK]    usb 480 Mbps (path: /sys/bus/usb/devices/7-2)
[OK]    venv /home/emre/Downloads/Clash-Of-Clans-Otomation/.venv (present)
[INFO]  installing locked dependencies from /home/emre/Downloads/Clash-Of-Clans-Otomation/requirements-lock.txt
[OK]    dependencies installed (numpy, opencv-python-headless)
[OK]    runtime dirs (var/logs, var/metrics, var/artifacts, var/tmp)

Bootstrap summary
=================
  ✓ python          3.12.3
  ✓ adb             35.0.0-11411520
  ✓ device          jfzxugsgnnvsrsg6
  ✓ usb             480 Mbps
  ✓ venv            /home/emre/Downloads/Clash-Of-Clans-Otomation/.venv
  ✓ deps            10 locked
  ✓ runtime_dirs    var/{logs,metrics,artifacts,tmp}

Next: source .venv/bin/activate && python -m automation.bootstrap
```

Exit code: **0**. Second run is idempotent (no re-creation of venv, no re-mkdir, deps re-confirmed).

### 2.3 Live `python -m automation.bootstrap`

```
2026-05-20T11:53:13 INFO automation.fingerprint: fingerprinted device jfzxugsgnnvsrsg6 (Xiaomi 22095RA98C, Android 13, 1080x2408, USB 480 Mbps)
2026-05-20T11:53:13 INFO automation.bootstrap: USB link speed OK: 480 Mbps
Bootstrap summary
=================
Environment
  python:           3.12.3
  adb:              35.0.0
Device
  serial:           jfzxugsgnnvsrsg6
  manufacturer:     Xiaomi
  model:            22095RA98C
  android_version:  13
  sdk:              33
  resolution:       1080x2408
  usb_speed:        480 Mbps
  adb_version:      35.0.0
Runtime directories
  /home/emre/Downloads/Clash-Of-Clans-Otomation/var/logs  (present)
  /home/emre/Downloads/Clash-Of-Clans-Otomation/var/metrics  (present)
  /home/emre/Downloads/Clash-Of-Clans-Otomation/var/artifacts  (present)
  /home/emre/Downloads/Clash-Of-Clans-Otomation/var/tmp  (present)

Status: READY
```

Exit code: **0**.

### 2.4 Negative path — bad device state (live test)

Verified by injecting a fake `adb` on `$PATH` that returns `unauthorized`. `bootstrap.sh` exited **3** (`EXIT_NO_DEVICE`) with the documented remediation message:

```
[INFO]  Phase 1 bootstrap starting (repo: /home/emre/Downloads/Clash-Of-Clans-Otomation)
[OK]    python 3.12.3
[OK]    adb 35.0.0-test
[ERROR] device is in state 'unauthorized'; expected 'device'.
For 'unauthorized', accept the USB-debugging prompt on the phone.
For 'no permissions', install udev rules and re-plug.
```

### 2.5 Negative path — 12 Mbps USB (test fixture)

The mandatory 12 Mbps rejection is verified in `tests/test_bootstrap.py::test_bootstrap_rejects_12_mbps_usb` and `::test_bootstrap_main_exits_4_on_too_slow_usb`. The test populates a fake sysfs tree with `speed=12`, runs `bootstrap.run()`, and asserts that `USBValidationError` is raised (and that `main()` exits 4). This is the "frozen Phase-0.5 requirement" called out in the Phase 1 prompt.

---

## 3. Device fingerprint output

The live fingerprint matches Phase 0 measurements exactly:

| Field | Value |
|---|---|
| serial | `jfzxugsgnnvsrsg6` |
| manufacturer | `Xiaomi` |
| model | `22095RA98C` (Redmi Note 11R) |
| android_version | `13` |
| sdk | `33` |
| resolution | `1080 × 2408` |
| usb_speed | `480 Mbps` |
| adb_version | `35.0.0` |

The resolution matches the native frame measured in Phase 0 (10.4 MB raw screencap). Phase 2's `Sensor` will resample this into the 1080×1920 reference per ADR-04.

---

## 4. USB validation result

| Check | Outcome |
|---|---|
| sysfs path resolution | `/sys/bus/usb/devices/7-2` (resolved by matching `serial` attribute) |
| measured link speed | `480` Mbps |
| frozen NFR | `≥ 480 Mbps` (from `docs/frozen_nfrs_v1.md §5`) |
| verdict | **PASS** — link is at USB 2.0 HS |

The bootstrap correctly distinguishes three USB states:

1. ≥ 480 Mbps → INFO + proceed.
2. < 480 Mbps (typically 12 Mbps via a USB 1.1 hub) → ERROR + exit 4 with operator remediation.
3. sysfs unreadable → WARN + proceed (default), or → exit 5 with `--strict-usb`.

State 2 is the failure mode Phase 0 surfaced (the operator's device was initially observed at 12 Mbps because the cable was plugged through a keyboard's built-in USB hub). The test suite covers all three states without requiring real hardware.

---

## 5. Test results

| File | Tests | Result |
|---|---:|---|
| `tests/test_adb.py` | 13 | ✅ all pass |
| `tests/test_bootstrap.py` | 8 | ✅ all pass |
| `tests/test_errors.py` | 2 | ✅ all pass |
| `tests/test_fingerprint.py` | 13 | ✅ all pass |
| `tests/test_paths.py` | 2 | ✅ all pass |
| **total** | **38** | **✅ 38 / 38** |

Wall-clock: 0.05 s on the operator's host.

The suite has no real-device dependency. `SubprocessRecorder` mocks `subprocess.run`; `fake_sysfs` is a tmp-path tree; `monkeypatch` rewires `automation.paths.RUNTIME_DIRS` for the bootstrap tests so the suite never touches the actual `var/` tree.

---

## 6. Phase 2 readiness

| Requirement | Status |
|---|---|
| Reproducible Python venv (`uv` or `pip` compatible) | ✅ `.venv/` created by `bootstrap.sh`; deps locked in `requirements-lock.txt` |
| `ADB` wrapper available | ✅ `automation.adb.ADB` with all needed primitives for SENSE |
| Device fingerprinting | ✅ `automation.fingerprint.DeviceFingerprint` carries everything Phase 2's `Remap` needs (native resolution, etc.) |
| USB link-speed gate | ✅ enforced at bootstrap; matches ADR-01a §Decision (5) |
| Runtime directories | ✅ `var/{logs,metrics,artifacts,tmp}` |
| Error hierarchy | ✅ `AutomationError` base + subsystem subclasses; Phase 2 will add `SensorError`/`UnsupportedPixelFormatError` |
| `paths` single source of truth | ✅ `automation.paths` |
| Production-quality (type hints, docstrings, logging) | ✅ `from __future__ import annotations` everywhere; `logging.getLogger(__name__)`; no `print` outside the CLI summary |

**Phase 2 can begin.** The screenshot pipeline implementation (Phase 2)
will consume:

- `ADB.exec_out([...])` for `screencap` calls.
- `DeviceFingerprint.resolution` for the Phase 2 `Remap` construction.
- The Phase 0 measured numbers (frozen in `docs/frozen_nfrs_v1.md`) as
  its soak-test acceptance criterion.

---

## 7. Files created

```
.env.example                                  20 lines
.gitignore                                    13 lines  (was 6)
automation/__init__.py                        10 lines
automation/adb.py                            155 lines
automation/bootstrap.py                      193 lines
automation/errors.py                          36 lines
automation/fingerprint.py                    155 lines
automation/paths.py                           36 lines
pyproject.toml                                40 lines
requirements-lock.txt                         11 lines
scripts/bootstrap.sh                         219 lines
tests/__init__.py                              0 lines
tests/conftest.py                            108 lines
tests/test_adb.py                            122 lines
tests/test_bootstrap.py                      138 lines
tests/test_errors.py                          26 lines
tests/test_fingerprint.py                    158 lines
tests/test_paths.py                           45 lines
phase1-report.md                            (this file)
```

Total: 16 new files (15 source + 1 report).

---

## 8. Unresolved risks

None blocking. Documented:

- **Single-device assumption** carried through (`automation.adb.ADB`
  talks to the single connected device implicitly). Multi-device is
  out of scope per SYSTEM-ROADMAP §2.2 and DESIGN-REVIEW §1.7.
- **The bootstrap's USB-speed check is one-shot at startup.** A
  mid-run link degradation (cable wiggling, hub re-negotiation) is
  not detected. Captured in DESIGN-REVIEW v1.1 backlog row #1.
- **`ADB` wrapper uses blocking `subprocess.run`.** The Phase 5
  orchestrator will need an asyncio-friendly version; Phase 5 will
  introduce a thin `asyncio.to_thread` wrapper rather than rewriting
  this module. Noted in the file's docstring.
- **No structured logging yet.** Phase 1 uses plaintext via
  `logging.getLogger(__name__)`. Phase 6 introduces structured JSON
  logs (ADR-12).

---

## 9. Readiness verdict

**Phase 1: COMPLETE. Phase 2 may begin.**

Validation summary:

- 38 / 38 tests pass.
- `scripts/bootstrap.sh` runs end-to-end with exit 0 against the real device.
- `python -m automation.bootstrap` runs end-to-end with exit 0.
- 12 Mbps USB hub failure mode is rejected (exit 4) — verified in tests.
- Unauthorized-device state is rejected (exit 3) — verified live with a fake adb.
- Bootstrap is idempotent; the second run does no destructive work.
- `var/{logs,metrics,artifacts,tmp}` created.
- All frozen NFRs that Phase 1 is responsible for are implemented and verified.

The Phase 2 implementer should next read `PHASE-MASTER-PROMPTS.md` Phase 2, `ADR.md` ADR-01a, and `phase-0-report.md` §3 for the screencap latency budget they need to meet.
