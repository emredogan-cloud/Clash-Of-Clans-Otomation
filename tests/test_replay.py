"""replay_tick CLI tests — metadata parsing + output assertions."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

# The replay tool lives under scripts/; import it directly.
import importlib.util
import sys

_REPLAY_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "replay_tick.py"
)
_SPEC = importlib.util.spec_from_file_location("replay_tick", _REPLAY_PATH)
assert _SPEC is not None and _SPEC.loader is not None
replay_tick = importlib.util.module_from_spec(_SPEC)
sys.modules["replay_tick"] = replay_tick
_SPEC.loader.exec_module(replay_tick)


def _make_metadata(
    *,
    correlation_id: str = "tick_20260521T140000_abc123",
    tier: str = "validated",
    state_after: str = "IDLE",
    success: bool = True,
    found: bool = True,
    action_ran: bool = True,
    validation_found: bool = False,
    retries_used: int = 0,
    action_latency_ms: float | None = 60.0,
) -> dict:
    sm = {
        "found": found,
        "confidence": 0.99 if found else 0.05,
        "template_name": "demo",
        "search_mode": "full_gray",
        "capture_latency_ms": 940.0,
        "match_latency_ms": 50.0,
        "x": 500 if found else None,
        "y": 600 if found else None,
        "width": 64 if found else None,
        "height": 64 if found else None,
        "center": [532, 632] if found else None,
    }
    return {
        "correlation_id": correlation_id,
        "tier": tier,
        "tick": {
            "state_before": "IDLE",
            "state_after": state_after,
            "success": success,
            "tick_latency_ms": 2500.0,
            "capture_latency_ms": 940.0,
            "match_latency_ms": 50.0,
            "action_latency_ms": action_latency_ms,
            "ts": "2026-05-21T14:00:00+00:00",
        },
        "template": {"name": "demo", "width": 64, "height": 64,
                     "threshold": 0.9},
        "search_match": sm,
        "action_result": (
            {
                "action_type": "tap",
                "device_x": 532,
                "device_y": 730,
                "latency_ms": action_latency_ms or 0.0,
                "success": True,
                "ts": "2026-05-21T14:00:00+00:00",
            }
            if action_ran
            else None
        ),
        "validation_match": (
            {
                "found": validation_found,
                "confidence": 0.03 if not validation_found else 0.99,
                "template_name": "demo",
                "search_mode": "full_gray",
                "capture_latency_ms": 940.0,
                "match_latency_ms": 50.0,
                "x": None, "y": None, "width": None, "height": None,
                "center": None,
            }
            if action_ran
            else None
        ),
        "retries_used": retries_used,
    }


def _write_metadata(tmp_path: Path, payload: dict) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / "metadata.json"
    p.write_text(json.dumps(payload, indent=2))
    return p


# ---- parsing ----------------------------------------------------------------


def test_load_happy_path(tmp_path: Path) -> None:
    p = _write_metadata(tmp_path, _make_metadata())
    payload = replay_tick.load_tick_metadata(p)
    assert payload["correlation_id"] == "tick_20260521T140000_abc123"
    assert payload["tier"] == "validated"


def test_load_missing_top_level_key_raises(tmp_path: Path) -> None:
    bad = _make_metadata()
    del bad["correlation_id"]
    p = _write_metadata(tmp_path, bad)
    with pytest.raises(ValueError, match="missing required top-level keys"):
        replay_tick.load_tick_metadata(p)


def test_load_missing_tick_key_raises(tmp_path: Path) -> None:
    bad = _make_metadata()
    del bad["tick"]["state_after"]
    p = _write_metadata(tmp_path, bad)
    with pytest.raises(ValueError, match="tick.*missing required keys"):
        replay_tick.load_tick_metadata(p)


def test_load_root_not_object_raises(tmp_path: Path) -> None:
    p = tmp_path / "metadata.json"
    p.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(ValueError, match="root must be an object"):
        replay_tick.load_tick_metadata(p)


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        replay_tick.load_tick_metadata(tmp_path / "missing.json")


# ---- formatting -------------------------------------------------------------


def test_format_replay_includes_correlation_and_tier(tmp_path: Path) -> None:
    text = replay_tick.format_replay(_make_metadata())
    assert "tick_20260521T140000_abc123" in text
    assert "validated" in text


def test_format_happy_path_shows_ok_and_hit() -> None:
    text = replay_tick.format_replay(_make_metadata(success=True))
    assert "OK" in text
    assert "HIT" in text
    assert "MISS" not in text


def test_format_search_miss_shows_miss() -> None:
    text = replay_tick.format_replay(_make_metadata(
        tier="search_only",
        state_after="FAILED",
        success=False,
        found=False,
        action_ran=False,
        action_latency_ms=None,
    ))
    assert "FAIL" in text
    assert "MISS" in text
    assert "no action ran" in text
    assert "validation did not run" in text


def test_format_validation_fail_shows_template_still_present() -> None:
    text = replay_tick.format_replay(_make_metadata(
        tier="validated_retry",
        state_after="FAILED",
        success=False,
        validation_found=True,  # template still there
        retries_used=1,
    ))
    assert "template still present" in text


def test_format_validation_success_shows_template_gone() -> None:
    text = replay_tick.format_replay(_make_metadata(
        tier="validated_retry",
        success=True,
        validation_found=False,
        retries_used=1,
    ))
    assert "template gone" in text


def test_format_includes_all_latencies() -> None:
    text = replay_tick.format_replay(_make_metadata())
    assert "tick total:" in text
    assert "capture:" in text
    assert "match:" in text
    assert "action:" in text
    assert "2500.00" in text  # tick total
    assert "940.00" in text   # capture
    assert "50.00" in text    # match
    assert "60.00" in text    # action


def test_format_includes_retries() -> None:
    text = replay_tick.format_replay(_make_metadata(retries_used=1))
    assert "retries used:   1" in text


def test_format_includes_state_flow() -> None:
    text = replay_tick.format_replay(_make_metadata(
        state_after="FAILED",
        success=False,
    ))
    assert "IDLE → FAILED" in text


# ---- CLI entry point --------------------------------------------------------


def test_main_with_file_argument(tmp_path: Path) -> None:
    p = _write_metadata(tmp_path, _make_metadata())
    out = io.StringIO()
    rc = replay_tick.main([str(p)], out=out)
    assert rc == 0
    assert "tick_20260521T140000_abc123" in out.getvalue()


def test_main_with_directory_argument(tmp_path: Path) -> None:
    """If a directory is passed, the tool looks for metadata.json inside."""
    p = _write_metadata(tmp_path, _make_metadata())
    out = io.StringIO()
    rc = replay_tick.main([str(p.parent)], out=out)
    assert rc == 0


def test_main_missing_file_returns_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = replay_tick.main([str(tmp_path / "does_not_exist.json")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "replay_tick:" in err


def test_main_directory_without_metadata_returns_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    empty = tmp_path / "empty_dir"
    empty.mkdir()
    rc = replay_tick.main([str(empty)])
    assert rc == 1


def test_main_malformed_json_returns_nonzero(tmp_path: Path) -> None:
    p = tmp_path / "metadata.json"
    p.write_text("{this is not json")
    rc = replay_tick.main([str(p)])
    assert rc == 1
