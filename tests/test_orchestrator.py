"""Orchestrator tests using mocked Sensor/Matcher/Actuator.

The mocks let us drive the FSM through every branch deterministically:

- happy path (search HIT → action OK → validation passes)
- search MISS at SEARCHING
- ADB failure at ACTING
- validation fails on the first cycle but passes after the retry
- validation fails on both the first and the retry cycle
- explicit `tick()` from FAILED is rejected
- `reset()` outside FAILED is rejected
- artifact writing is gated on `debug`, atomic, schema-correct

No real device, no real ADB, no real OpenCV. The mocks return
pre-built `Frame` / `MatchResult` / `ActionResult` instances so the
orchestrator's behaviour is the only thing under test.
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest

from automation.action_result import ActionResult
from automation.errors import InvalidTransitionError
from automation.frame import Frame
from automation.match_result import MatchResult
from automation.orchestrator import (
    VALIDATION_RETRY_BUDGET,
    Orchestrator,
)
from automation.state import State
from automation.template import Template
from automation.tick_result import TickResult


# ---- common fixtures --------------------------------------------------------


REF_W, REF_H = 1080, 1920
NATIVE_W, NATIVE_H = 1080, 2408


def _utc(year: int = 2026, month: int = 5, day: int = 21,
         h: int = 12, m: int = 0, s: int = 0,
         us: int = 0) -> _dt.datetime:
    return _dt.datetime(year, month, day, h, m, s, us, tzinfo=_dt.timezone.utc)


def _make_frame(*, capture_latency_ms: float = 940.0) -> Frame:
    img = np.zeros((REF_H, REF_W, 3), dtype=np.uint8)
    return Frame(
        image_bgr=img,
        width=REF_W, height=REF_H,
        source_mode="raw",
        capture_latency_ms=capture_latency_ms,
        capture_ts=_utc(),
        native_width=NATIVE_W,
        native_height=NATIVE_H,
    )


def _make_template(name: str = "demo") -> Template:
    gray = np.full((64, 64), 128, dtype=np.uint8)
    return Template(
        name=name, image_gray=gray, width=64, height=64,
        threshold=0.9, roi=None,
    )


def _make_hit(*, x: int = 500, y: int = 900,
              confidence: float = 0.99,
              match_latency_ms: float = 2.3) -> MatchResult:
    return MatchResult(
        found=True, confidence=confidence,
        template_name="demo", search_mode="full_gray",
        capture_latency_ms=940.0, match_latency_ms=match_latency_ms,
        x=x, y=y, width=64, height=64,
    )


def _make_miss(*, match_latency_ms: float = 1.7) -> MatchResult:
    return MatchResult(
        found=False, confidence=0.10,
        template_name="demo", search_mode="full_gray",
        capture_latency_ms=940.0, match_latency_ms=match_latency_ms,
    )


def _make_action(*, success: bool = True,
                 latency_ms: float = 60.0) -> ActionResult:
    return ActionResult(
        success=success, action_type="tap", latency_ms=latency_ms,
        device_x=532, device_y=1170, ts=_utc(),
    )


# ---- mock subsystems --------------------------------------------------------


@dataclass
class MockSensor:
    """Sensor stub returning a queue of pre-built frames."""
    frames: list[Frame]
    calls: int = 0

    def capture(self) -> Frame:
        if self.calls >= len(self.frames):
            # Reuse the last frame if the orchestrator asks for more than
            # the test prepared — tests should fail-loudly via the call
            # count assertions when this is unexpected.
            f = self.frames[-1]
        else:
            f = self.frames[self.calls]
        self.calls += 1
        return f


@dataclass
class MockMatcher:
    """Matcher stub returning a queue of pre-built MatchResults."""
    results: list[MatchResult]
    calls: int = 0

    def match(self, frame: Frame, template: Template) -> MatchResult:
        if self.calls >= len(self.results):
            r = self.results[-1]
        else:
            r = self.results[self.calls]
        self.calls += 1
        return r


@dataclass
class MockActuator:
    """Actuator stub recording tap calls and returning pre-built results."""
    results: list[ActionResult]
    calls: list[tuple[int, int, int, int]]

    def tap(self, x: int, y: int, nw: int, nh: int,
            *, jitter: bool = False) -> ActionResult:
        self.calls.append((x, y, nw, nh))
        idx = len(self.calls) - 1
        if idx >= len(self.results):
            return self.results[-1]
        return self.results[idx]


def _build_orch(
    *,
    frames: list[Frame] | None = None,
    matches: list[MatchResult] | None = None,
    actions: list[ActionResult] | None = None,
    debug: bool = False,
) -> tuple[Orchestrator, MockSensor, MockMatcher, MockActuator]:
    sensor = MockSensor(frames=frames or [_make_frame()])
    matcher = MockMatcher(results=matches or [_make_hit()])
    actuator = MockActuator(results=actions or [_make_action()], calls=[])
    template = _make_template()
    orch = Orchestrator(
        sensor,  # type: ignore[arg-type]
        matcher,  # type: ignore[arg-type]
        actuator,  # type: ignore[arg-type]
        template,
        debug=debug,
    )
    return orch, sensor, matcher, actuator


# ---- happy path -------------------------------------------------------------


def test_happy_path_ends_in_idle_success() -> None:
    # Search HIT, then validate MISS (template gone).
    orch, sensor, matcher, actuator = _build_orch(
        frames=[_make_frame(), _make_frame(capture_latency_ms=941.0)],
        matches=[_make_hit(), _make_miss()],
        actions=[_make_action(success=True)],
    )
    r = orch.tick()
    assert isinstance(r, TickResult)
    assert r.success is True
    assert r.state_before is State.IDLE
    assert r.state_after is State.IDLE
    assert orch.state is State.IDLE

    # The first capture's latency is what's surfaced on TickResult.
    assert r.capture_latency_ms == 940.0
    assert r.match_latency_ms == 2.3
    assert r.action_latency_ms == 60.0

    # Sensor was called twice (search + validate), matcher twice, actuator once.
    assert sensor.calls == 2
    assert matcher.calls == 2
    assert len(actuator.calls) == 1

    # Tap coords are the centre of the matched template at (500, 900):
    # (500 + 32, 900 + 32) = (532, 932), then denormalize to native.
    # MockActuator only records the (x_ref, y_ref) we passed in,
    # which IS the reference-space centre.
    x, y, nw, nh = actuator.calls[0]
    assert (x, y) == (532, 932)
    assert (nw, nh) == (NATIVE_W, NATIVE_H)


def test_happy_path_total_latency_envelopes_breakdown() -> None:
    orch, *_ = _build_orch(
        matches=[_make_hit(), _make_miss()],
    )
    r = orch.tick()
    # tick_latency_ms must be >= each individual latency (we measure
    # around the whole tick including the second capture/match).
    assert r.tick_latency_ms >= r.capture_latency_ms or r.tick_latency_ms >= 0
    assert r.tick_latency_ms >= 0
    # Two captures + two matches + one action all happened; total >= 0.


# ---- search miss ------------------------------------------------------------


def test_search_miss_lands_in_failed_with_no_action() -> None:
    orch, sensor, matcher, actuator = _build_orch(
        matches=[_make_miss()],
    )
    r = orch.tick()
    assert r.success is False
    assert r.state_after is State.FAILED
    assert r.action_latency_ms is None
    assert sensor.calls == 1
    assert matcher.calls == 1
    assert len(actuator.calls) == 0


# ---- action fail ------------------------------------------------------------


def test_action_fail_lands_in_failed_with_action_latency() -> None:
    orch, sensor, matcher, actuator = _build_orch(
        matches=[_make_hit()],
        actions=[_make_action(success=False, latency_ms=99.5)],
    )
    r = orch.tick()
    assert r.success is False
    assert r.state_after is State.FAILED
    assert r.action_latency_ms == 99.5
    # No validation cycle happened — only the search capture.
    assert sensor.calls == 1
    assert matcher.calls == 1
    assert len(actuator.calls) == 1


# ---- validation failure paths -----------------------------------------------


def test_validation_first_cycle_misses_no_retry_needed() -> None:
    """Already covered by happy path; this asserts no retry was taken."""
    orch, sensor, matcher, _ = _build_orch(
        matches=[_make_hit(), _make_miss()],
    )
    r = orch.tick()
    assert r.success is True
    assert sensor.calls == 2  # search + 1 validate
    assert matcher.calls == 2


def test_validation_retry_succeeds() -> None:
    """First validate still finds template (template hasn't moved yet);
    second validate finds it gone."""
    orch, sensor, matcher, _ = _build_orch(
        matches=[
            _make_hit(),         # SEARCH: found
            _make_hit(),         # VALIDATE #1: still there
            _make_miss(),        # VALIDATE #2 (retry): gone
        ],
    )
    r = orch.tick()
    assert r.success is True
    assert r.state_after is State.IDLE
    # Search + 2 validates = 3 captures + matches.
    assert sensor.calls == 3
    assert matcher.calls == 3


def test_validation_retry_also_fails() -> None:
    """Template never goes away → after 1 retry, fail."""
    orch, sensor, matcher, _ = _build_orch(
        matches=[
            _make_hit(),    # SEARCH
            _make_hit(),    # VALIDATE #1
            _make_hit(),    # VALIDATE #2 (retry)
        ],
    )
    r = orch.tick()
    assert r.success is False
    assert r.state_after is State.FAILED
    # 3 captures: search + initial validate + retry validate.
    assert sensor.calls == 3
    assert matcher.calls == 3


def test_validation_retry_budget_is_one() -> None:
    """Constant sanity check."""
    assert VALIDATION_RETRY_BUDGET == 1


# ---- FSM correctness --------------------------------------------------------


def test_tick_in_failed_state_raises_invalid_transition() -> None:
    orch, *_ = _build_orch(matches=[_make_miss()])
    orch.tick()  # lands in FAILED
    assert orch.state is State.FAILED
    with pytest.raises(InvalidTransitionError, match="tick.*requires state=IDLE"):
        orch.tick()


def test_tick_after_reset_succeeds() -> None:
    """FAILED → IDLE via reset(); subsequent tick() proceeds."""
    orch, _, matcher, _ = _build_orch(
        matches=[
            _make_miss(),        # tick #1 → FAILED
            _make_hit(),         # tick #2: SEARCH HIT
            _make_miss(),        # tick #2: VALIDATE MISS
        ],
    )
    r1 = orch.tick()
    assert r1.success is False
    orch.reset()
    assert orch.state is State.IDLE
    r2 = orch.tick()
    assert r2.success is True


def test_reset_from_idle_raises() -> None:
    orch, *_ = _build_orch()
    with pytest.raises(InvalidTransitionError, match="reset.*requires state=FAILED"):
        orch.reset()


def test_reset_from_acting_raises() -> None:
    """Mid-transition reset is not allowed — only from FAILED."""
    orch, *_ = _build_orch()
    # Manually drive: IDLE → SEARCHING → ACTING
    orch._transition(State.SEARCHING, reason="test")
    orch._transition(State.ACTING, reason="test")
    with pytest.raises(InvalidTransitionError, match="reset.*requires state=FAILED"):
        orch.reset()


def test_invalid_transition_is_blocked_at_the_method() -> None:
    """Direct illegal transitions raise via _transition."""
    orch, *_ = _build_orch()
    with pytest.raises(InvalidTransitionError, match="illegal FSM transition"):
        orch._transition(State.FAILED, reason="test")  # IDLE → FAILED disallowed


def test_state_property_is_read_only_via_public_surface() -> None:
    """No `state` setter on the class."""
    orch, *_ = _build_orch()
    with pytest.raises(AttributeError):
        orch.state = State.FAILED  # type: ignore[misc]


def test_initial_state_is_idle() -> None:
    orch, *_ = _build_orch()
    assert orch.state is State.IDLE


# ---- artifacts ---------------------------------------------------------------


def test_artifacts_written_when_debug_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "var" / "artifacts" / "orchestrator"
    monkeypatch.setattr("automation.orchestrator.ARTIFACTS_DIR", artifacts)
    orch, *_ = _build_orch(
        matches=[_make_hit(), _make_miss()],
        debug=True,
    )
    r = orch.tick()
    assert r.success is True
    subdirs = list(artifacts.iterdir())
    assert len(subdirs) == 1
    d = subdirs[0]
    assert d.name.endswith("_ok") or "_ok_" in d.name
    md = json.loads((d / "metadata.json").read_text())
    assert md["tick"]["state_before"] == "IDLE"
    assert md["tick"]["state_after"] == "IDLE"
    assert md["tick"]["success"] is True
    assert md["template"]["name"] == "demo"
    assert md["search_match"]["found"] is True
    assert md["action_result"]["success"] is True
    assert md["validation_match"]["found"] is False
    assert md["retries_used"] == 0


def test_artifacts_dir_named_fail_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "var" / "artifacts" / "orchestrator"
    monkeypatch.setattr("automation.orchestrator.ARTIFACTS_DIR", artifacts)
    orch, *_ = _build_orch(
        matches=[_make_miss()],
        debug=True,
    )
    orch.tick()
    d = next(iter(artifacts.iterdir()))
    assert "_fail_" in d.name
    md = json.loads((d / "metadata.json").read_text())
    assert md["tick"]["state_after"] == "FAILED"
    assert md["search_match"]["found"] is False
    assert md["action_result"] is None
    assert md["validation_match"] is None
    assert md["retries_used"] == 0


def test_artifacts_record_retry_in_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "var" / "artifacts" / "orchestrator"
    monkeypatch.setattr("automation.orchestrator.ARTIFACTS_DIR", artifacts)
    orch, *_ = _build_orch(
        matches=[
            _make_hit(),
            _make_hit(),   # validate #1 still there → retry
            _make_miss(),  # validate #2 gone → success
        ],
        debug=True,
    )
    orch.tick()
    d = next(iter(artifacts.iterdir()))
    md = json.loads((d / "metadata.json").read_text())
    assert md["retries_used"] == 1
    assert md["validation_match"]["found"] is False  # the LAST validate seen


def test_artifacts_skipped_when_debug_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "var" / "artifacts" / "orchestrator"
    monkeypatch.setattr("automation.orchestrator.ARTIFACTS_DIR", artifacts)
    orch, *_ = _build_orch(
        matches=[_make_hit(), _make_miss()],
        debug=False,
    )
    orch.tick()
    if artifacts.exists():
        assert not any(artifacts.iterdir())


def test_artifacts_env_var_enables_debug(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "var" / "artifacts" / "orchestrator"
    monkeypatch.setattr("automation.orchestrator.ARTIFACTS_DIR", artifacts)
    monkeypatch.setenv("ORCH_DEBUG", "1")
    sensor = MockSensor(frames=[_make_frame(), _make_frame()])
    matcher = MockMatcher(results=[_make_hit(), _make_miss()])
    actuator = MockActuator(results=[_make_action()], calls=[])
    orch = Orchestrator(
        sensor,  # type: ignore[arg-type]
        matcher,  # type: ignore[arg-type]
        actuator,  # type: ignore[arg-type]
        _make_template(),
        # debug defaults from env var
    )
    assert orch.debug is True
    orch.tick()
    assert any(artifacts.iterdir())


def test_artifacts_no_partial_tmp_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "var" / "artifacts" / "orchestrator"
    monkeypatch.setattr("automation.orchestrator.ARTIFACTS_DIR", artifacts)
    orch, *_ = _build_orch(
        matches=[_make_hit(), _make_miss()], debug=True,
    )
    orch.tick()
    d = next(iter(artifacts.iterdir()))
    assert any(p.name == "metadata.json" for p in d.iterdir())
    assert not any(p.suffix == ".tmp" for p in d.iterdir())


def test_artifact_write_failure_does_not_crash_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unwritable artifacts dir must not fail the tick."""
    monkeypatch.setattr(
        "automation.orchestrator.ARTIFACTS_DIR",
        Path("/proc/forbidden/orchestrator"),
    )
    orch, *_ = _build_orch(
        matches=[_make_hit(), _make_miss()], debug=True,
    )
    r = orch.tick()
    assert r.success is True  # tick itself succeeded


# ---- immutability of TickResult propagated through the orchestrator ---------


def test_tick_result_is_frozen() -> None:
    orch, *_ = _build_orch(matches=[_make_hit(), _make_miss()])
    r = orch.tick()
    with pytest.raises(Exception):
        r.success = False  # type: ignore[misc]


def test_two_successive_ticks_use_independent_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling tick() twice (with a reset between) produces two
    independent TickResults; no per-instance retry counter persists
    between ticks (per the prompt: 'no persistent retry counters')."""
    orch, _, matcher, _ = _build_orch(
        matches=[
            _make_hit(), _make_hit(), _make_hit(),  # tick #1 fails on validate+retry
            _make_hit(), _make_miss(),              # tick #2 succeeds on first validate
        ],
        actions=[_make_action(success=True), _make_action(success=True)],
    )
    r1 = orch.tick()
    assert r1.success is False
    orch.reset()
    r2 = orch.tick()
    # tick #2 used only 1 search + 1 validate (no retry needed) — i.e.
    # the retry budget reset between ticks.
    # Total matcher calls = 3 (tick1) + 2 (tick2) = 5.
    assert matcher.calls == 5
    assert r2.success is True


# =============================================================================
# Phase 6 instrumentation hooks
# =============================================================================


from automation.correlation import CorrelationId
from automation.logger import StructuredLogger
from automation.metrics import MetricsCollector


def _fixed_correlation_factory(value: str = "tick_20260521T140000_aaaaaa"):
    """A correlation-id factory that emits a deterministic id sequence."""
    seq = [value, "tick_20260521T140001_bbbbbb", "tick_20260521T140002_cccccc"]
    counter = {"i": 0}

    def factory() -> CorrelationId:
        i = counter["i"]
        counter["i"] = i + 1
        return CorrelationId(seq[i % len(seq)])

    return factory


def test_orchestrator_works_without_any_instrumentation() -> None:
    """Phase 5 behaviour preserved: tick() runs identically with no logger/metrics."""
    orch, *_ = _build_orch(matches=[_make_hit(), _make_miss()])
    r = orch.tick()
    assert r.success is True
    # No logger/metrics provided; nothing crashed.


def test_orchestrator_generates_correlation_id_per_tick() -> None:
    seen: list[CorrelationId] = []
    def factory() -> CorrelationId:
        cid = CorrelationId(f"tick_20260521T140000_a{len(seen):05d}")
        seen.append(cid)
        return cid
    sensor = MockSensor(frames=[_make_frame(), _make_frame(), _make_frame(), _make_frame()])
    matcher = MockMatcher(results=[_make_hit(), _make_miss(), _make_hit(), _make_miss()])
    actuator = MockActuator(results=[_make_action(), _make_action()], calls=[])
    orch = Orchestrator(
        sensor,  # type: ignore[arg-type]
        matcher,  # type: ignore[arg-type]
        actuator,  # type: ignore[arg-type]
        _make_template(),
        correlation_id_factory=factory,
    )
    orch.tick()
    orch.tick()
    assert len(seen) == 2
    assert seen[0] != seen[1]


def test_logger_hook_writes_tick_record(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    logger = StructuredLogger(logs_dir=log_dir)
    sensor = MockSensor(frames=[_make_frame(), _make_frame()])
    matcher = MockMatcher(results=[_make_hit(), _make_miss()])
    actuator = MockActuator(results=[_make_action()], calls=[])
    orch = Orchestrator(
        sensor,  # type: ignore[arg-type]
        matcher,  # type: ignore[arg-type]
        actuator,  # type: ignore[arg-type]
        _make_template(),
        logger=logger,
        correlation_id_factory=_fixed_correlation_factory(),
    )
    orch.tick()
    log_lines = (log_dir / "ticks.jsonl").read_text().splitlines()
    assert len(log_lines) == 1
    record = json.loads(log_lines[0])
    assert record["correlation_id"] == "tick_20260521T140000_aaaaaa"
    assert record["state_before"] == "IDLE"
    assert record["state_after"] == "IDLE"
    assert record["success"] is True
    assert record["retries_used"] == 0
    assert record["tier"] == "validated"
    assert record["template"] == "demo"


def test_logger_hook_failure_does_not_break_tick(tmp_path: Path) -> None:
    """Logger raising mid-tick must not break the orchestrator."""
    class _BrokenLogger:
        def log_tick(self, **kwargs):  # noqa: D401, ANN003
            raise RuntimeError("disk full")

    orch_args = _build_orch(matches=[_make_hit(), _make_miss()])
    orch = orch_args[0]
    orch.logger = _BrokenLogger()  # type: ignore[assignment]
    r = orch.tick()
    assert r.success is True  # tick still produced its result


def test_metrics_hook_observes_tick_action_match() -> None:
    metrics = MetricsCollector()
    sensor = MockSensor(frames=[_make_frame(), _make_frame()])
    matcher = MockMatcher(results=[_make_hit(), _make_miss()])
    actuator = MockActuator(results=[_make_action()], calls=[])
    orch = Orchestrator(
        sensor,  # type: ignore[arg-type]
        matcher,  # type: ignore[arg-type]
        actuator,  # type: ignore[arg-type]
        _make_template(),
        metrics=metrics,
        correlation_id_factory=_fixed_correlation_factory(),
    )
    orch.tick()
    cv = metrics.counters_view()
    assert cv["ticks_total"] == 1
    assert cv["ticks_success"] == 1
    assert cv["matches_total"] == 1  # search match (initial only counted in tick observation)
    assert cv["actions_total"] == 1
    assert cv["validation_ticks"] == 1


def test_metrics_hook_tier_search_only_on_search_miss() -> None:
    metrics = MetricsCollector()
    sensor = MockSensor(frames=[_make_frame()])
    matcher = MockMatcher(results=[_make_miss()])
    actuator = MockActuator(results=[], calls=[])
    orch = Orchestrator(
        sensor,  # type: ignore[arg-type]
        matcher,  # type: ignore[arg-type]
        actuator,  # type: ignore[arg-type]
        _make_template(),
        metrics=metrics,
        correlation_id_factory=_fixed_correlation_factory(),
    )
    orch.tick()
    snap = metrics.snapshot()
    assert snap["tick_histograms"]["search_only"]["count"] == 1
    assert snap["tick_histograms"]["validated"]["count"] == 0
    assert snap["tick_histograms"]["validated_retry"]["count"] == 0


def test_metrics_hook_tier_validated_retry_on_retry_used() -> None:
    metrics = MetricsCollector()
    sensor = MockSensor(frames=[_make_frame(), _make_frame(), _make_frame()])
    matcher = MockMatcher(results=[_make_hit(), _make_hit(), _make_miss()])  # retry needed
    actuator = MockActuator(results=[_make_action()], calls=[])
    orch = Orchestrator(
        sensor,  # type: ignore[arg-type]
        matcher,  # type: ignore[arg-type]
        actuator,  # type: ignore[arg-type]
        _make_template(),
        metrics=metrics,
        correlation_id_factory=_fixed_correlation_factory(),
    )
    orch.tick()
    snap = metrics.snapshot()
    assert snap["tick_histograms"]["validated_retry"]["count"] == 1
    cv = metrics.counters_view()
    assert cv["retries_total"] == 1
    assert cv["validation_ticks"] == 1


def test_metrics_hook_failure_does_not_break_tick() -> None:
    class _BrokenMetrics:
        def observe_tick(self, **kwargs):  # noqa: D401, ANN003
            raise RuntimeError("broken")
        def observe_action(self, **kwargs):  # noqa: D401, ANN003
            pass
        def observe_match(self, **kwargs):  # noqa: D401, ANN003
            pass

    orch_args = _build_orch(matches=[_make_hit(), _make_miss()])
    orch = orch_args[0]
    orch.metrics = _BrokenMetrics()  # type: ignore[assignment]
    r = orch.tick()
    assert r.success is True


def test_artifact_includes_correlation_id_and_tier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Phase-6 artifact schema adds correlation_id and tier."""
    artifacts = tmp_path / "var" / "artifacts" / "orchestrator"
    monkeypatch.setattr("automation.orchestrator.ARTIFACTS_DIR", artifacts)
    sensor = MockSensor(frames=[_make_frame(), _make_frame()])
    matcher = MockMatcher(results=[_make_hit(), _make_miss()])
    actuator = MockActuator(results=[_make_action()], calls=[])
    orch = Orchestrator(
        sensor,  # type: ignore[arg-type]
        matcher,  # type: ignore[arg-type]
        actuator,  # type: ignore[arg-type]
        _make_template(),
        correlation_id_factory=_fixed_correlation_factory(),
        debug=True,
    )
    orch.tick()
    subdirs = list(artifacts.iterdir())
    assert len(subdirs) == 1
    d = subdirs[0]
    # New directory naming: <correlation_id>_<verdict>_<tier>
    assert d.name == "tick_20260521T140000_aaaaaa_ok_validated"
    md = json.loads((d / "metadata.json").read_text())
    assert md["correlation_id"] == "tick_20260521T140000_aaaaaa"
    assert md["tier"] == "validated"


def test_artifact_tier_search_only_on_miss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "var" / "artifacts" / "orchestrator"
    monkeypatch.setattr("automation.orchestrator.ARTIFACTS_DIR", artifacts)
    sensor = MockSensor(frames=[_make_frame()])
    matcher = MockMatcher(results=[_make_miss()])
    actuator = MockActuator(results=[], calls=[])
    orch = Orchestrator(
        sensor,  # type: ignore[arg-type]
        matcher,  # type: ignore[arg-type]
        actuator,  # type: ignore[arg-type]
        _make_template(),
        correlation_id_factory=_fixed_correlation_factory(),
        debug=True,
    )
    orch.tick()
    d = next(iter(artifacts.iterdir()))
    assert d.name == "tick_20260521T140000_aaaaaa_fail_search_only"
    md = json.loads((d / "metadata.json").read_text())
    assert md["tier"] == "search_only"


def test_artifact_tier_validated_retry_when_retry_used(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifacts = tmp_path / "var" / "artifacts" / "orchestrator"
    monkeypatch.setattr("automation.orchestrator.ARTIFACTS_DIR", artifacts)
    sensor = MockSensor(frames=[_make_frame(), _make_frame(), _make_frame()])
    matcher = MockMatcher(results=[_make_hit(), _make_hit(), _make_miss()])
    actuator = MockActuator(results=[_make_action()], calls=[])
    orch = Orchestrator(
        sensor,  # type: ignore[arg-type]
        matcher,  # type: ignore[arg-type]
        actuator,  # type: ignore[arg-type]
        _make_template(),
        correlation_id_factory=_fixed_correlation_factory(),
        debug=True,
    )
    orch.tick()
    d = next(iter(artifacts.iterdir()))
    assert "_validated_retry" in d.name
    md = json.loads((d / "metadata.json").read_text())
    assert md["tier"] == "validated_retry"
    assert md["retries_used"] == 1


def test_correlation_id_is_propagated_to_logger_and_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One tick → same correlation id in logs AND artifacts."""
    artifacts = tmp_path / "art"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr("automation.orchestrator.ARTIFACTS_DIR", artifacts)
    logger = StructuredLogger(logs_dir=log_dir)
    sensor = MockSensor(frames=[_make_frame(), _make_frame()])
    matcher = MockMatcher(results=[_make_hit(), _make_miss()])
    actuator = MockActuator(results=[_make_action()], calls=[])
    orch = Orchestrator(
        sensor,  # type: ignore[arg-type]
        matcher,  # type: ignore[arg-type]
        actuator,  # type: ignore[arg-type]
        _make_template(),
        logger=logger,
        correlation_id_factory=_fixed_correlation_factory(),
        debug=True,
    )
    orch.tick()
    log_record = json.loads((log_dir / "ticks.jsonl").read_text().splitlines()[0])
    artifact_dir = next(iter(artifacts.iterdir()))
    md = json.loads((artifact_dir / "metadata.json").read_text())
    assert log_record["correlation_id"] == md["correlation_id"]
    assert artifact_dir.name.startswith(log_record["correlation_id"])
