"""External L2 supervision package (Phase 8A).

Two pieces:

- `watchdog.heartbeat.HeartbeatWriter` — runs inside the framework
  process; atomically writes a per-tick liveness beacon to JSON.
- `watchdog.watchdog.ExternalWatchdog` — runs *outside* the
  framework process; reads the heartbeat, classifies freshness,
  emits a `WatchdogStatus` with an escalation recommendation.

Phase 8A delivers the **observation + classification** half of
ADR-11's L2 watchdog. It does NOT (per the Phase 8A prompt's
prohibitions): kill the framework, send signals, restart
processes, run as a systemd unit, or invoke `adb kill-server`.
Those side effects are Phase 8B / future scope.

The package is stdlib-only by construction. `watchdog/watchdog.py`
must not import from `automation/*` — the process boundary is
the whole point. `watchdog/heartbeat.py` is also stdlib-only;
it duck-types the runtime-health argument (any object with
`to_debug_dict()`) to avoid a runtime import.
"""
__all__ = ["heartbeat", "watchdog"]
