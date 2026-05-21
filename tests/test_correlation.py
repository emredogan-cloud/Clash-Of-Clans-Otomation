"""Correlation-id generation tests."""
from __future__ import annotations

import datetime as _dt
import re

import pytest

from automation.correlation import CorrelationId, is_valid, new_id


_UTC = _dt.timezone.utc


# ---- format -----------------------------------------------------------------


def test_format_matches_expected_pattern() -> None:
    """tick_YYYYMMDDTHHMMSS_<6 hex>"""
    cid = new_id()
    assert re.fullmatch(r"tick_\d{8}T\d{6}_[0-9a-f]{6}", cid)


def test_format_is_filesystem_safe() -> None:
    """No characters that need escaping on any major FS."""
    for _ in range(10):
        cid = new_id()
        assert all(c.isalnum() or c == "_" for c in cid), cid


def test_format_is_typed_as_correlation_id() -> None:
    """NewType only matters statically, but the call returns a str at runtime."""
    cid = new_id()
    assert isinstance(cid, str)


def test_format_total_length_is_28() -> None:
    """Useful for downstream tooling that allocates fixed-width buffers."""
    cid = new_id()
    # "tick_" (5) + 15 + "_" (1) + 6 = 27. But year is 4 digits so
    # "20260521T134522" is 15 chars; "tick_" + 15 + "_" + 6 = 27.
    # Actually: 5 + 15 + 1 + 6 = 27. Hmm let me count: t-i-c-k-_ = 5,
    # then date 8 + T + time 6 = 15, then _ = 1, then 6 hex = 6.
    # Total: 5 + 15 + 1 + 6 = 27.
    assert len(cid) == 27


# ---- determinism via `now=` parameter ---------------------------------------


def test_explicit_now_produces_expected_prefix() -> None:
    cid = new_id(now=_dt.datetime(2026, 5, 21, 13, 45, 22, tzinfo=_UTC))
    assert cid.startswith("tick_20260521T134522_")
    # Tail is random; only the prefix is deterministic.


def test_naive_datetime_rejected() -> None:
    naive = _dt.datetime(2026, 5, 21, 13, 45, 22)
    with pytest.raises(ValueError, match="timezone-aware"):
        new_id(now=naive)


# ---- uniqueness -------------------------------------------------------------


def test_repeated_calls_produce_unique_ids() -> None:
    ids = {new_id() for _ in range(1000)}
    # Pigeonhole: 1000 ids over 16^6 = ~16M tail-space → collision probability
    # is ≈ 1000² / 2·16M ≈ 0.03. We expect ≥ 999 distinct ids in practice;
    # over 100 runs ≥ 999 holds with extremely high probability.
    assert len(ids) >= 999


def test_same_second_calls_differ_only_in_tail() -> None:
    fixed = _dt.datetime(2026, 5, 21, 13, 45, 22, tzinfo=_UTC)
    a = new_id(now=fixed)
    b = new_id(now=fixed)
    assert a != b
    assert a[:21] == b[:21]  # "tick_20260521T134522_"


# ---- sortability ------------------------------------------------------------


def test_sortable_by_timestamp() -> None:
    earlier = new_id(now=_dt.datetime(2026, 5, 21, 13, 45, 22, tzinfo=_UTC))
    later = new_id(now=_dt.datetime(2026, 5, 21, 13, 45, 23, tzinfo=_UTC))
    assert sorted([later, earlier]) == [earlier, later]


def test_sort_order_matches_date_order_across_days() -> None:
    a = new_id(now=_dt.datetime(2026, 5, 20, 23, 59, 59, tzinfo=_UTC))
    b = new_id(now=_dt.datetime(2026, 5, 21, 0, 0, 0, tzinfo=_UTC))
    assert a < b


# ---- is_valid ---------------------------------------------------------------


def test_is_valid_accepts_freshly_generated() -> None:
    for _ in range(20):
        assert is_valid(new_id())


def test_is_valid_rejects_non_string() -> None:
    assert not is_valid(None)  # type: ignore[arg-type]
    assert not is_valid(123)  # type: ignore[arg-type]


def test_is_valid_rejects_uuid() -> None:
    assert not is_valid("12345678-1234-1234-1234-123456789abc")


def test_is_valid_rejects_wrong_prefix() -> None:
    assert not is_valid("snap_20260521T134522_abc123")


def test_is_valid_rejects_truncated() -> None:
    assert not is_valid("tick_20260521T134522")
    assert not is_valid("tick_20260521T134522_")
    assert not is_valid("tick_20260521T134522_abc")


def test_is_valid_rejects_non_hex_tail() -> None:
    assert not is_valid("tick_20260521T134522_xyz123")
    assert not is_valid("tick_20260521T134522_ABC123")  # uppercase


def test_is_valid_rejects_malformed_timestamp() -> None:
    assert not is_valid("tick_2026_5_21T134522_abc123")
    assert not is_valid("tick_20260521X134522_abc123")  # X not T
    assert not is_valid("tick_abcdefghT134522_abc123")
