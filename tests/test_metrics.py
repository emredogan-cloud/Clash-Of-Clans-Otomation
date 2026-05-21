"""MetricsCollector tests — counters, histograms, tiers, persistence."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from automation.errors import MetricsError
from automation.metrics import (
    COUNTER_NAMES,
    MATCH_BUCKETS_MS,
    MetricsCollector,
    TAP_BUCKETS_MS,
    TICK_BUCKETS_MS,
    TICK_TIERS,
    derive_tier,
)


# ---- counter shape -----------------------------------------------------------


def test_initial_counters_are_zero() -> None:
    m = MetricsCollector()
    cv = m.counters_view()
    assert set(cv.keys()) == set(COUNTER_NAMES)
    for v in cv.values():
        assert v == 0


# ---- tier derivation ---------------------------------------------------------


def test_derive_tier_search_only_when_no_action() -> None:
    assert derive_tier(action_ran=False, validation_ran=False, retries_used=0) == "search_only"


def test_derive_tier_search_only_when_action_but_no_validation() -> None:
    """ACTING fail → action_ran=True, validation_ran=False → search_only cost
    profile (only one capture happened)."""
    assert derive_tier(action_ran=True, validation_ran=False, retries_used=0) == "search_only"


def test_derive_tier_validated_no_retry() -> None:
    assert derive_tier(action_ran=True, validation_ran=True, retries_used=0) == "validated"


def test_derive_tier_validated_with_retry() -> None:
    assert derive_tier(action_ran=True, validation_ran=True, retries_used=1) == "validated_retry"


# ---- observe_tick ------------------------------------------------------------


def test_observe_tick_increments_total_and_success() -> None:
    m = MetricsCollector()
    m.observe_tick(latency_ms=1200.0, tier="search_only", success=True, retries_used=0)
    cv = m.counters_view()
    assert cv["ticks_total"] == 1
    assert cv["ticks_success"] == 1
    assert cv["ticks_failed"] == 0


def test_observe_tick_increments_failed() -> None:
    m = MetricsCollector()
    m.observe_tick(latency_ms=950.0, tier="search_only", success=False, retries_used=0)
    cv = m.counters_view()
    assert cv["ticks_failed"] == 1
    assert cv["ticks_success"] == 0


def test_observe_tick_validation_counter_only_for_validated_tiers() -> None:
    m = MetricsCollector()
    m.observe_tick(latency_ms=1200.0, tier="search_only", success=False, retries_used=0)
    m.observe_tick(latency_ms=2100.0, tier="validated", success=True, retries_used=0)
    m.observe_tick(latency_ms=2900.0, tier="validated_retry", success=True, retries_used=1)
    cv = m.counters_view()
    assert cv["ticks_total"] == 3
    assert cv["validation_ticks"] == 2  # validated + validated_retry
    assert cv["retries_total"] == 1


def test_observe_tick_invalid_tier_raises() -> None:
    m = MetricsCollector()
    with pytest.raises(MetricsError, match="tier must be one of"):
        m.observe_tick(latency_ms=1.0, tier="not_a_tier", success=True, retries_used=0)


def test_observe_tick_negative_latency_raises() -> None:
    m = MetricsCollector()
    with pytest.raises(MetricsError, match=">= 0"):
        m.observe_tick(latency_ms=-1.0, tier="search_only", success=True, retries_used=0)


def test_observe_tick_negative_retries_raises() -> None:
    m = MetricsCollector()
    with pytest.raises(MetricsError, match=">= 0"):
        m.observe_tick(latency_ms=1.0, tier="search_only", success=True, retries_used=-1)


def test_observe_tick_non_int_retries_rejected() -> None:
    m = MetricsCollector()
    with pytest.raises(MetricsError, match="retries_used must be int"):
        m.observe_tick(latency_ms=1.0, tier="search_only", success=True,
                       retries_used=0.5)  # type: ignore[arg-type]


def test_observe_tick_non_number_latency_rejected() -> None:
    m = MetricsCollector()
    with pytest.raises(MetricsError, match="must be a number"):
        m.observe_tick(latency_ms="1.0", tier="search_only", success=True,  # type: ignore[arg-type]
                       retries_used=0)


def test_observe_match_non_number_latency_rejected() -> None:
    m = MetricsCollector()
    with pytest.raises(MetricsError, match="must be a number"):
        m.observe_match(latency_ms=True)  # type: ignore[arg-type]


def test_bucket_validation_empty_rejected() -> None:
    """_validate_buckets is exercised via _Histogram construction;
    empty buckets aren't possible through the public API, but the
    validator is defensively typed."""
    from automation.metrics import _validate_buckets
    with pytest.raises(MetricsError, match="at least one bucket"):
        _validate_buckets(())


def test_bucket_validation_non_monotonic_rejected() -> None:
    from automation.metrics import _validate_buckets
    with pytest.raises(MetricsError, match="strictly increasing"):
        _validate_buckets((50.0, 100.0, 50.0))


def test_bucket_validation_non_number_rejected() -> None:
    from automation.metrics import _validate_buckets
    with pytest.raises(MetricsError, match="must be a number"):
        _validate_buckets((50.0, "100"))  # type: ignore[arg-type]


# ---- bucket placement --------------------------------------------------------


def test_tick_bucket_placement_at_boundary() -> None:
    """sample == edge[i] → goes into bucket i (le=<=); not the next one."""
    m = MetricsCollector()
    # Bucket layout: [50, 100, 200, 400, 800, 1600, 3200, 6400]
    # A 200 ms sample lands in the "le=200" bucket (index 2).
    m.observe_tick(latency_ms=200.0, tier="search_only", success=True, retries_used=0)
    snap = m.snapshot()
    counts = snap["tick_histograms"]["search_only"]["counts"]
    assert counts == [0, 0, 1, 0, 0, 0, 0, 0, 0]


def test_tick_bucket_overflow() -> None:
    """sample > all edges → overflow bucket (last index)."""
    m = MetricsCollector()
    m.observe_tick(latency_ms=10_000.0, tier="validated_retry", success=False, retries_used=1)
    snap = m.snapshot()
    counts = snap["tick_histograms"]["validated_retry"]["counts"]
    # 8 buckets + 1 overflow = 9 slots.
    assert len(counts) == 9
    assert counts[-1] == 1
    assert sum(counts) == 1


def test_tick_histograms_are_independent_per_tier() -> None:
    m = MetricsCollector()
    m.observe_tick(latency_ms=1200.0, tier="search_only", success=False, retries_used=0)
    m.observe_tick(latency_ms=2100.0, tier="validated", success=True, retries_used=0)
    snap = m.snapshot()
    so = snap["tick_histograms"]["search_only"]
    v = snap["tick_histograms"]["validated"]
    assert so["count"] == 1
    assert v["count"] == 1
    # search_only landed in 1600 ms bucket (1200 ≤ 1600).
    # validated landed in 3200 ms bucket (2100 ≤ 3200).
    # Bucket indices: [50, 100, 200, 400, 800, 1600, 3200, 6400, +inf].
    assert so["counts"][5] == 1  # le=1600
    assert v["counts"][6] == 1   # le=3200


def test_three_phase5_observations_land_correctly() -> None:
    """Phase-5 measurements: 1211, 2956, 2584 ms."""
    m = MetricsCollector()
    m.observe_tick(latency_ms=1211.0, tier="search_only", success=False, retries_used=0)
    m.observe_tick(latency_ms=2956.0, tier="validated_retry", success=False, retries_used=1)
    m.observe_tick(latency_ms=2584.0, tier="validated_retry", success=True, retries_used=1)
    snap = m.snapshot()
    # All three land at le=3200.
    so_counts = snap["tick_histograms"]["search_only"]["counts"]
    vr_counts = snap["tick_histograms"]["validated_retry"]["counts"]
    assert so_counts[5] == 1   # 1211 → le=1600 bucket
    assert vr_counts[6] == 2   # both 2956 and 2584 → le=3200 bucket


def test_bucket_layouts_match_spec() -> None:
    assert TICK_BUCKETS_MS == (50, 100, 200, 400, 800, 1600, 3200, 6400)
    assert TAP_BUCKETS_MS == (10, 25, 50, 100, 200, 500)
    assert MATCH_BUCKETS_MS == (1, 2, 5, 10, 25, 50, 100)


def test_tick_tiers_match_spec() -> None:
    assert TICK_TIERS == ("search_only", "validated", "validated_retry")


# ---- observe_action / observe_match ------------------------------------------


def test_observe_action_creates_keyed_histogram() -> None:
    m = MetricsCollector()
    m.observe_action(action_type="tap", latency_ms=58.0)
    m.observe_action(action_type="swipe", latency_ms=370.0)
    snap = m.snapshot()
    assert set(snap["action_histograms"].keys()) == {"tap", "swipe"}
    # tap@58 → le=100 bucket (boundary [10, 25, 50, 100, 200, 500, +inf]).
    assert snap["action_histograms"]["tap"]["counts"][3] == 1
    # swipe@370 → le=500 bucket.
    assert snap["action_histograms"]["swipe"]["counts"][5] == 1


def test_observe_action_empty_type_rejected() -> None:
    m = MetricsCollector()
    with pytest.raises(MetricsError, match="non-empty string"):
        m.observe_action(action_type="", latency_ms=1.0)


def test_observe_match() -> None:
    m = MetricsCollector()
    m.observe_match(latency_ms=2.3)  # le=5
    m.observe_match(latency_ms=47.0)  # le=50
    snap = m.snapshot()
    counts = snap["match_histogram"]["counts"]
    # Buckets [1, 2, 5, 10, 25, 50, 100, +inf]
    assert counts[2] == 1  # le=5
    assert counts[5] == 1  # le=50
    assert m.counters_view()["matches_total"] == 2


def test_match_negative_latency_rejected() -> None:
    m = MetricsCollector()
    with pytest.raises(MetricsError, match=">= 0"):
        m.observe_match(latency_ms=-0.1)


# ---- persistence -------------------------------------------------------------


def test_persist_writes_atomic_json(tmp_path: Path) -> None:
    m = MetricsCollector(metrics_dir=tmp_path)
    m.observe_tick(latency_ms=1200.0, tier="search_only", success=True, retries_used=0)
    out = m.persist()
    assert out == tmp_path / "metrics.json"
    assert out.is_file()
    blob = json.loads(out.read_text())
    assert blob["counters"]["ticks_total"] == 1
    # No partial .tmp file left behind.
    assert not any(p.suffix == ".tmp" for p in tmp_path.iterdir())


def test_persist_schema_keys(tmp_path: Path) -> None:
    m = MetricsCollector(metrics_dir=tmp_path)
    m.observe_tick(latency_ms=1.0, tier="search_only", success=True, retries_used=0)
    m.observe_action(action_type="tap", latency_ms=50.0)
    m.observe_match(latency_ms=5.0)
    blob = json.loads(m.persist().read_text())
    assert set(blob.keys()) == {
        "counters",
        "tick_histograms",
        "action_histograms",
        "match_histogram",
    }
    assert set(blob["tick_histograms"].keys()) == set(TICK_TIERS)


def test_snapshot_is_json_safe() -> None:
    m = MetricsCollector()
    m.observe_tick(latency_ms=1234.5, tier="validated", success=True, retries_used=0)
    m.observe_action(action_type="tap", latency_ms=58.0)
    m.observe_match(latency_ms=2.3)
    snap = m.snapshot()
    # Round-trip through json without errors.
    re_decoded = json.loads(json.dumps(snap))
    assert re_decoded["counters"]["ticks_total"] == 1


def test_persist_is_atomic_no_temp_leak(tmp_path: Path) -> None:
    m = MetricsCollector(metrics_dir=tmp_path)
    for i in range(10):
        m.observe_tick(latency_ms=1000.0 + i, tier="search_only", success=True, retries_used=0)
        m.persist()
    assert (tmp_path / "metrics.json").is_file()
    assert not any(p.suffix == ".tmp" for p in tmp_path.iterdir())


def test_persist_overwrites(tmp_path: Path) -> None:
    """Sequential persists overwrite cleanly."""
    m = MetricsCollector(metrics_dir=tmp_path)
    m.observe_tick(latency_ms=1.0, tier="search_only", success=True, retries_used=0)
    m.persist()
    m.observe_tick(latency_ms=2.0, tier="search_only", success=True, retries_used=0)
    m.persist()
    blob = json.loads((tmp_path / "metrics.json").read_text())
    assert blob["counters"]["ticks_total"] == 2
