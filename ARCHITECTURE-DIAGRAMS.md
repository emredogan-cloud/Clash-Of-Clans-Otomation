# Architecture Diagrams

> **Document type:** Visual architecture reference
> **System:** Android UI Automation Framework (Python + OpenCV + ADB)
> **Companion documents:** [ADR.md](./ADR.md), [SYSTEM-ROADMAP.md](./SYSTEM-ROADMAP.md), [DESIGN-REVIEW.md](./DESIGN-REVIEW.md)

All diagrams are Mermaid. They are the canonical visualization of the architecture; if a diagram and prose disagree, the prose in `SYSTEM-ROADMAP.md` wins and the diagram is updated to match. Diagrams are intentionally minimal — they encode *structure*, not exhaustive detail.

---

## 1. Component diagram — subsystem boundaries

The framework decomposes into clearly bounded subsystems aligned with SENSE → THINK → ACT, plus the cross-cutting concerns (observability, recovery, configuration, asset management).

```mermaid
flowchart TB
    subgraph HOST["Linux host"]
        subgraph BOOT["Bootstrap & Config"]
            CFG[Config loader<br/>TOML + env]
            BOOT_S[Bootstrap script<br/>venv, deps, fingerprint]
        end

        subgraph CORE["Framework core (single process)"]
            ORCH["Orchestrator<br/>(State machine, ADR-08)"]
            SENSE["SENSE subsystem<br/>Screenshot pipeline (ADR-01/02)"]
            THINK["THINK subsystem<br/>Vision engine (ADR-03/04/05)"]
            ACT["ACT subsystem<br/>Action engine (ADR-06)"]
            REC["In-process recovery handlers<br/>(ADR-11 layer 1)"]
        end

        subgraph ASSETS["Asset & Template store (ADR-10)"]
            TPL[Templates +<br/>metadata + masks]
            MAN[Template manifest<br/>content-addressed]
        end

        subgraph OBS["Observability (ADR-12)"]
            LOG[Structured JSON logs]
            MET[Metrics file<br/>Prometheus text]
            ART[Artifact store<br/>frames + debug images]
        end

        WDOG["External watchdog<br/>(systemd --user, ADR-11 layer 2)"]
        ADBCLI[adb client<br/>Linux host binary]
    end

    DEV["Android device<br/>(USB debug enabled)"]

    CFG --> ORCH
    BOOT_S --> CORE
    BOOT_S --> ASSETS

    ORCH --> SENSE
    ORCH --> THINK
    ORCH --> ACT
    ORCH --> REC
    REC --> ORCH

    SENSE --> ADBCLI
    ACT --> ADBCLI
    ADBCLI <==> DEV

    TPL --> MAN
    MAN --> THINK

    ORCH -.->|emits| LOG
    ORCH -.->|emits| MET
    THINK -.->|on low conf.| ART

    WDOG -.->|reads heartbeat| CORE
    WDOG -.->|restarts on stale| CORE
```

**Reading guide.** Solid arrows are data flow on the hot path. Dotted arrows are observability or supervisory side-effects. The Linux host contains everything except the device. The framework core is a single OS process (ADR-07). The watchdog and bootstrap are *outside* the core and supervise it.

---

## 2. Data flow diagram — one tick of the automation loop

A "tick" is one SENSE → THINK → ACT pass. Multiple ticks may occur per second, or seconds may elapse between ticks depending on state (e.g. waiting for an animation).

```mermaid
flowchart LR
    A[Tick start<br/>tick_id assigned] --> B["SENSE: request frame<br/>adb exec-out screencap raw"]
    B --> C[Parse framebuffer header]
    C --> D[Convert RGBA→BGR<br/>NumPy ndarray]
    D --> E["Resample to reference<br/>resolution (ADR-04)"]
    E --> F["THINK: match active<br/>template set"]
    F --> G{Best match<br/>≥ hard threshold?}
    G -->|yes| H[Resolve action plan<br/>via state machine]
    G -->|no| I{Above soft<br/>threshold?}
    I -->|yes| J[Log soft match<br/>emit metric]
    I -->|no| K[Log miss<br/>save artifact]
    J --> H
    K --> L[State machine:<br/>increment retry / timeout]
    H --> M[Apply jitter envelope<br/>ADR-15]
    M --> N["ACT: send adb input<br/>or no-op if observe-only"]
    N --> O[Post-action wait<br/>jittered]
    L --> O
    O --> P[Update metrics<br/>tick duration etc.]
    P --> Q[Tick end<br/>heartbeat written]
```

**Reading guide.** A tick has three exits — clean match, soft match (logged, still acts), miss (recovery escalation in the state machine). The artifact write on miss is the diagnostic surface for debugging silent drift. Heartbeat is written *unconditionally* at tick end so the watchdog distinguishes "framework is doing work" from "framework is hung."

---

## 3. Sequence diagram — typical tick across components

This view shows the same tick as in §2 but from a component-interaction perspective, useful for reasoning about latency budget.

```mermaid
sequenceDiagram
    autonumber
    participant ORCH as Orchestrator
    participant SENSE as SENSE
    participant ADB as adb client
    participant DEV as Device
    participant THINK as THINK
    participant TPL as TemplateManifest
    participant ACT as ACT
    participant OBS as Observability

    ORCH->>OBS: tick_start(tick_id)
    ORCH->>SENSE: capture_frame()
    SENSE->>ADB: exec-out screencap
    ADB->>DEV: USB request
    DEV-->>ADB: raw framebuffer bytes
    ADB-->>SENSE: stdout bytes
    SENSE->>SENSE: parse + convert + resample
    SENSE-->>ORCH: Frame
    ORCH->>THINK: match(Frame, active_templates)
    THINK->>TPL: lookup(template_ids)
    TPL-->>THINK: templates + metadata
    THINK->>THINK: matchTemplate per template
    THINK-->>ORCH: MatchResults
    ORCH->>ORCH: state transition
    ORCH->>ACT: tap(normalized_coords)
    ACT->>ACT: apply jitter, denormalize
    ACT->>ADB: shell input tap X Y
    ADB->>DEV: USB request
    DEV-->>ADB: ack
    ADB-->>ACT: exit 0
    ACT-->>ORCH: ActionResult
    ORCH->>OBS: tick_end(tick_id, duration, results)
```

**Latency budget — OLD (pre-Phase-0 estimates) vs NEW (Phase-0
measured, operator hardware):**

| Step | OLD estimate | NEW measured (operator) | Source of cost |
|------|--------------|--------------------------|----------------|
| Capture round trip (steps 3–7) | 80–250 ms | **947 ms (raw, content-deterministic)** / 578–1311 ms (PNG, content-dependent) | USB transport + device-side `screencap` composition |
| Parse + convert + resample (step 8) | 5–15 ms | UE — within range; Phase 2 microbench will confirm | CPU |
| Template matching (step 12) | 5–50 ms × N templates | **2.2 ms (ROI gray) / 7.0 (ROI BGR) / 33.6 (full gray) / 137.9 (full BGR)** per template | CPU |
| Action round trip (steps 17–22) | 80–200 ms | UE — Phase 4 measures; ADB shell overhead alone is 28 ms median (VF) | USB + `input` JVM bootstrap |
| **Total per tick (default templates, ROI discipline)** | **~200–500 ms** typical | **~1.0–1.5 s** typical (UE) | — |

Source: [phase-0-report.md](../phase-0-report.md) §3, §4, §5;
[docs/frozen_nfrs_v1.md](../docs/frozen_nfrs_v1.md). USB 2.0 host
(operator does not have a USB 3.x device).

The OLD column is preserved for historical traceability. The NEW
column is what an implementer or operator should plan against. v1.0
frozen NFRs live in `docs/frozen_nfrs_v1.md`; v1.0 tick rate is
0.5–1 Hz, not the pre-Phase-0 2–5 Hz.

---

## 4. Formal state diagram — orchestrator state machine

```mermaid
stateDiagram-v2
    [*] --> BOOTSTRAP

    BOOTSTRAP --> CONNECTING : config loaded
    BOOTSTRAP --> FAULTED : config invalid

    CONNECTING --> CALIBRATING : adb device authorized
    CONNECTING --> RECONNECTING : adb error
    CONNECTING --> FAULTED : auth refused

    CALIBRATING --> READY : fingerprint OK,<br/>remap computed
    CALIBRATING --> FAULTED : unsupported device

    READY --> OBSERVING : run() requested

    OBSERVING --> MATCHING : frame captured
    OBSERVING --> RECONNECTING : capture timeout

    MATCHING --> ACTING : decisive match
    MATCHING --> WAITING : soft match,<br/>retries remain
    MATCHING --> RECOVERING : zero match,<br/>retries exhausted

    WAITING --> OBSERVING : delay elapsed
    WAITING --> RECOVERING : retry budget exhausted

    ACTING --> VALIDATING : action sent

    VALIDATING --> OBSERVING : post-action frame OK
    VALIDATING --> RECOVERING : expected state not reached

    RECOVERING --> RESET_LITE : soft recovery
    RECOVERING --> RESET_HARD : hard recovery
    RECOVERING --> FAULTED : recovery exhausted

    RESET_LITE --> OBSERVING : back button / dismiss
    RESET_HARD --> RECONNECTING : kill-adb / re-handshake

    RECONNECTING --> CONNECTING : adb server restarted
    RECONNECTING --> FAULTED : reconnect exhausted

    FAULTED --> [*]
```

**State semantics (summary; full table in `SYSTEM-ROADMAP.md` §11):**

| State | Purpose | Timeout (default) | On timeout |
|-------|---------|-------------------|-----------|
| BOOTSTRAP | load config, verify environment | 10 s | → FAULTED |
| CONNECTING | establish ADB device authorization | 30 s | → RECONNECTING |
| CALIBRATING | device fingerprint, compute remap | 15 s | → FAULTED |
| READY | warm idle, awaiting `run()` | ∞ | — |
| OBSERVING | request a frame | 2 s | → RECONNECTING |
| MATCHING | run THINK on the frame | 500 ms | → RECOVERING |
| WAITING | scheduled delay before next tick | per spec | → OBSERVING |
| ACTING | send one or more ADB inputs | 2 s | → RECOVERING |
| VALIDATING | confirm the action took effect | 2 s | → RECOVERING |
| RECOVERING | choose a recovery path | 5 s | → FAULTED |
| RESET_LITE | back-button / dismiss-modal recovery | 5 s | → OBSERVING (or HARD on fail) |
| RESET_HARD | restart adb + reconnect | 30 s | → FAULTED |
| RECONNECTING | adb server bounce | 30 s | → FAULTED |
| FAULTED | terminal, watchdog will restart process | — | watchdog handles |

`RESET_LITE` and `RESET_HARD` are explicit recovery states rather than transitions because they have their own timeouts and observability footprint; collapsing them into transitions would hide a place where the system spends real time.

---

## 5. Recovery flow — what happens when something goes wrong

```mermaid
flowchart TD
    F[Fault detected<br/>in any state] --> C{Fault category?}

    C -->|adb command failed<br/>device unreachable| R1[Increment ADB error counter]
    C -->|capture timeout<br/>or empty frame| R2[Increment capture error counter]
    C -->|match below soft threshold<br/>N ticks in a row| R3[Increment match-drift counter]
    C -->|action sent but state unchanged| R4[Increment action-validation counter]
    C -->|exception in core<br/>process unresponsive| R5[Heartbeat goes stale]

    R1 --> R1a{counter > threshold?}
    R1a -->|no| OBS[Return to OBSERVING<br/>after backoff]
    R1a -->|yes| HARD[RESET_HARD]

    R2 --> R2a{counter > threshold?}
    R2a -->|no| OBS
    R2a -->|yes| HARD

    R3 --> R3a{counter > threshold?}
    R3a -->|no| OBS
    R3a -->|yes| LITE[RESET_LITE]

    R4 --> R4a{counter > threshold?}
    R4a -->|no| OBS
    R4a -->|yes| LITE

    LITE --> LITE_OK{worked?}
    LITE_OK -->|yes| OBS
    LITE_OK -->|no| HARD

    HARD --> HARD_OK{adb back up?}
    HARD_OK -->|yes| OBS
    HARD_OK -->|no| FAULT[Enter FAULTED<br/>exit process]

    R5 --> WDOG["External watchdog (ADR-11 L2)<br/>detects stale heartbeat"]
    WDOG --> KILL[SIGTERM core<br/>then SIGKILL if needed]
    KILL --> RESTART{restart budget OK?}
    RESTART -->|yes| START[Start fresh process]
    RESTART -->|no| HALT[Halt + notify operator]

    FAULT --> WDOG
```

**Reading guide.** The recovery system is a *cascade*: light recovery first, escalating only when light recovery doesn't resolve the symptom. Each escalation is gated by a counter so that transient faults (a single dropped frame) do not trigger a full reset. The watchdog (L2) is the floor: if everything else fails, the process dies and is restarted from scratch.

---

## 6. Process & thread topology

```mermaid
flowchart LR
    subgraph WD["systemd --user unit"]
        WD_S[Watchdog process<br/>~50 LOC, stdlib only]
    end

    subgraph FRAMEWORK["Framework process"]
        EL["asyncio event loop<br/>thread"]
        subgraph TP["Thread pool (bounded, ~4 threads)"]
            T1[adb subprocess thread 1]
            T2[adb subprocess thread 2]
            T3[adb subprocess thread N]
        end
        CV["CV calls inline on event loop<br/>(OpenCV releases GIL)"]
    end

    subgraph ADB_BG["adb server"]
        ADBSERV["adb server daemon<br/>(spawned by client on first use)"]
    end

    WD_S -.->|monitors heartbeat file| FRAMEWORK
    WD_S -.->|signals on stale| FRAMEWORK
    EL --> T1
    EL --> T2
    EL --> T3
    EL --> CV
    T1 --> ADBSERV
    T2 --> ADBSERV
    T3 --> ADBSERV
```

**Reading guide.** OpenCV calls run on the event-loop thread because they release the GIL during heavy work. ADB calls are blocking subprocesses, so they run in a thread pool to keep the loop responsive. The adb *server* (started transparently by the adb *client* on first use) is itself a daemon process, owned by no one in particular — recovering it is part of `RESET_HARD`.

---

## 7. Subsystem internal — SENSE pipeline detail

```mermaid
flowchart TB
    REQ[capture_frame request] --> SEL{primary or<br/>fallback mode?}
    SEL -->|primary| EXR["adb exec-out screencap<br/>(raw bytes)"]
    SEL -->|fallback| EXP["adb exec-out screencap -p<br/>(PNG bytes)"]

    EXR --> PARSE[Parse framebuffer header<br/>width, height, format]
    PARSE --> VAL{header valid?}
    VAL -->|no| FB1[Fall back to PNG mode<br/>once; log event]
    FB1 --> EXP
    VAL -->|yes| RGBA[Read pixel bytes]
    RGBA --> CONV[Convert RGBA→BGR<br/>cv2.cvtColor]

    EXP --> DEC[PNG decode<br/>cv2.imdecode]

    CONV --> RES[Resample to ref. resolution<br/>cv2.resize (ADR-04)]
    DEC --> RES

    RES --> FR[Frame ndarray BGR<br/>(H_ref, W_ref, 3)]
    FR --> EMIT[Emit Frame +<br/>capture_duration metric]
```

**Reading guide.** Fallback from primary to PNG is automatic on header parse failure, but it is logged and exposed as a metric so that operators see when fallback is engaging frequently — a signal that the header parser needs an update for a new device profile.

**Phase 0.5 additions.**

- **Mode is configurable.** `sensor.mode = "raw" | "png" | "pull" | "auto"` (see [ADR-01a](../ADR.md#adr-01a--screenshot-pipeline-phase-0-reality-content-dependent-ordering-usb-link-speed-prerequisite)). Default `"raw"` because raw latency is content-deterministic; operators on low-entropy UIs may override to `"png"` for ~450 ms faster captures.
- **Content-dependent ordering.** Raw and PNG modes have measured medians that reverse with screen content. Raw is ~947 ms regardless of content; PNG is ~578 ms on low-entropy screens and ~1311 ms on high-entropy screens. The pipeline diagram is the same; the mode selection knob is in config.
- **USB link-speed prerequisite.** The pipeline assumes the device is connected at ≥ 480 Mbps. Phase 1's `bootstrap.sh` validates this; see [SYSTEM-ROADMAP §5.1.7](../SYSTEM-ROADMAP.md#517-usb-link-speed-validation-phase-05-addition). A USB 1.1 (12 Mbps) link would multiply capture latency by ~40× and is treated as a startup failure.
- **Device-side composition cost.** Raw screencap latency (~947 ms) is decomposed as ~324 ms USB transport (10.4 MB / 260 Mbps) + ~620 ms device-side `screencap` composition. The composition cost is *not* addressable by switching modes; it is the cost of the device's `screencap` binary rendering the framebuffer per call.

---

## 8. Subsystem internal — THINK pipeline detail

```mermaid
flowchart TB
    F[Frame in BGR] --> ACT{active template set<br/>from state machine}
    ACT --> PRE[Preprocess<br/>grayscale cache,<br/>ROI crop cache]

    PRE --> LOOP[For each active template]
    LOOP --> ROI{ROI<br/>declared?}
    ROI -->|yes| CROP[Crop to ROI]
    ROI -->|no| FULL[Full frame]
    CROP --> SCALE{Scale<br/>strategy?}
    FULL --> SCALE
    SCALE -->|single| MT1[matchTemplate at 1x]
    SCALE -->|multi-scale| MTM[matchTemplate at<br/>0.9x, 1.0x, 1.1x;<br/>take best]
    MT1 --> NORM[Read peak correlation +<br/>peak location]
    MTM --> NORM
    NORM --> THR{score vs<br/>thresholds}
    THR -->|≥ hard| RES1[MatchResult: HIT<br/>+ score + loc]
    THR -->|≥ soft| RES2[MatchResult: SOFT<br/>+ score + loc]
    THR -->|< soft| RES3[MatchResult: MISS]

    RES1 --> AGG[Aggregate per template]
    RES2 --> AGG
    RES3 --> AGG

    AGG --> RET[Return MatchResults to orchestrator]
```

**Reading guide.** ROI restriction is the single largest win for matching cost — a 200×200 ROI is 50× cheaper to match than full 1080×1920. Templates that *can* declare a ROI almost always do; the few that cannot (e.g. "find this anywhere on screen") fall back to full-frame.

---

## 9. Subsystem internal — ACT pipeline detail

```mermaid
flowchart TB
    REQ[Action request<br/>normalized coords +<br/>action class] --> CLS{action class}

    CLS -->|tap| JTAP[Sample jitter:<br/>delay + coord dispersion]
    CLS -->|swipe| JSWP[Sample jitter:<br/>start/end + duration]
    CLS -->|long_press| JLP[Sample jitter:<br/>coord + hold duration]
    CLS -->|key| KEY[No jitter applied]

    JTAP --> DENORM1[Denormalize coords<br/>using device remap]
    JSWP --> DENORM2[Denormalize<br/>start + end]
    JLP --> DENORM3[Denormalize coords]

    DENORM1 --> WAIT1[Pre-action delay]
    DENORM2 --> WAIT2[Pre-action delay]
    DENORM3 --> WAIT3[Pre-action delay]
    KEY --> WAIT4[Pre-action delay]

    WAIT1 --> SEND1[adb shell input tap X Y]
    WAIT2 --> SEND2[adb shell input swipe X1 Y1 X2 Y2 dur]
    WAIT3 --> SEND3[adb shell input touchscreen swipe<br/>X Y X Y hold_dur]
    WAIT4 --> SEND4[adb shell input keyevent KEYCODE]

    SEND1 --> RES[ActionResult:<br/>elapsed, exit_code]
    SEND2 --> RES
    SEND3 --> RES
    SEND4 --> RES

    RES --> POSTW[Optional post-action wait]
    POSTW --> EMIT[Emit metrics + log line]
```

**Reading guide.** Jitter is applied *before* denormalization so the dispersion is in *normalized* space (proportional to screen size), which is more semantically meaningful than a pixel radius that means different things on different devices.

---

## 10. Deployment topology — the full picture

```mermaid
flowchart TB
    subgraph DEV["Operator's Linux desktop"]
        subgraph FS["Filesystem layout"]
            REPO["/home/$USER/.../framework/<br/>(repo)"]
            CFG_F[".../config/runtime.toml"]
            ASSETS_F[".../assets/templates/"]
            LOGS_F[".../var/log/*.jsonl"]
            METRICS_F[".../var/metrics/metrics.prom"]
            ART_F[".../var/artifacts/"]
            HB[".../var/run/heartbeat"]
        end

        subgraph SYSD["systemd --user"]
            SVC[automation.service<br/>= Framework]
            WSVC[automation-watchdog.service<br/>= Watchdog]
        end

        ADB[adb client + server]
    end

    USB["USB cable (data-capable)"]

    DEVICE["Android device<br/>USB debug authorized<br/>screen-on enforced"]

    SVC --> CFG_F
    SVC --> ASSETS_F
    SVC --> LOGS_F
    SVC --> METRICS_F
    SVC --> ART_F
    SVC --> HB
    SVC --> ADB
    WSVC --> HB
    WSVC -.->|restart| SVC
    ADB --> USB
    USB --> DEVICE
```

**Reading guide.** Two systemd user units. The framework writes a heartbeat file. The watchdog reads it. Everything else is filesystem-mediated. There is no network surface in v1 — observability is local-file-only by design.

---

## End of diagrams

When the architecture changes, update both this document and the corresponding ADR. Diagrams that drift from the code are worse than no diagrams.
