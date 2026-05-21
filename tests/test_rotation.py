"""RotationPolicy tests — cap enforcement, deterministic order."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from automation.errors import RotationError
from automation.rotation import (
    DEFAULT_ARTIFACTS_MAX_BYTES,
    DEFAULT_LOG_KEEP_FILES,
    DEFAULT_LOG_MAX_BYTES,
    RotationPolicy,
)


# ---- construction validation ------------------------------------------------


def test_default_constants() -> None:
    assert DEFAULT_LOG_MAX_BYTES == 10 * 1024 * 1024
    assert DEFAULT_LOG_KEEP_FILES == 5
    assert DEFAULT_ARTIFACTS_MAX_BYTES == 500 * 1024 * 1024


def test_construct_with_paths(tmp_path: Path) -> None:
    pol = RotationPolicy(
        logs_dir=tmp_path / "logs",
        artifacts_dir=tmp_path / "art",
    )
    assert pol.log_max_bytes == DEFAULT_LOG_MAX_BYTES
    assert pol.log_keep_files == 5
    assert pol.artifacts_max_bytes == 500 * 1024 * 1024


def test_non_path_logs_dir_rejected(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="logs_dir must be Path"):
        RotationPolicy(logs_dir="logs", artifacts_dir=tmp_path)  # type: ignore[arg-type]


def test_non_path_artifacts_dir_rejected(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="artifacts_dir must be Path"):
        RotationPolicy(logs_dir=tmp_path, artifacts_dir="art")  # type: ignore[arg-type]


def test_non_int_log_max_bytes_rejected(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="log_max_bytes must be int"):
        RotationPolicy(
            logs_dir=tmp_path, artifacts_dir=tmp_path,
            log_max_bytes=1.5,  # type: ignore[arg-type]
        )


def test_rotate_logs_missing_dir_returns_empty(tmp_path: Path) -> None:
    """No logs_dir on disk → rotation is a no-op."""
    pol = RotationPolicy(
        logs_dir=tmp_path / "missing", artifacts_dir=tmp_path,
    )
    assert pol.rotate_logs() == {}


def test_zero_cap_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        RotationPolicy(
            logs_dir=tmp_path, artifacts_dir=tmp_path,
            log_max_bytes=0,
        )


def test_negative_cap_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        RotationPolicy(
            logs_dir=tmp_path, artifacts_dir=tmp_path,
            artifacts_max_bytes=-1,
        )


# ---- log rotation -----------------------------------------------------------


def test_rotate_logs_no_files_returns_empty(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    pol = RotationPolicy(logs_dir=logs, artifacts_dir=tmp_path / "art")
    assert pol.rotate_logs() == {}


def test_rotate_logs_under_cap_is_noop(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "ticks.jsonl").write_bytes(b"x" * 100)
    pol = RotationPolicy(
        logs_dir=logs, artifacts_dir=tmp_path / "art",
        log_max_bytes=10_000,
    )
    assert pol.rotate_logs() == {}
    assert (logs / "ticks.jsonl").exists()


def test_rotate_logs_above_cap_shifts(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    # 200 bytes; cap is 100 bytes — rotation triggers.
    (logs / "ticks.jsonl").write_bytes(b"x" * 200)
    pol = RotationPolicy(
        logs_dir=logs, artifacts_dir=tmp_path / "art",
        log_max_bytes=100, log_keep_files=3,
    )
    out = pol.rotate_logs()
    assert "ticks" in out
    # After rotation: ticks.jsonl gone (we did not start a new one),
    # ticks.jsonl.1 exists.
    assert not (logs / "ticks.jsonl").exists()
    assert (logs / "ticks.jsonl.1").is_file()
    assert (logs / "ticks.jsonl.1").stat().st_size == 200


def test_rotate_logs_shifts_existing_rotated(tmp_path: Path) -> None:
    """Pre-existing .1, .2 shift to .2, .3."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "ticks.jsonl").write_bytes(b"new" * 200)
    (logs / "ticks.jsonl.1").write_bytes(b"old1")
    (logs / "ticks.jsonl.2").write_bytes(b"old2")
    pol = RotationPolicy(
        logs_dir=logs, artifacts_dir=tmp_path / "art",
        log_max_bytes=100, log_keep_files=5,
    )
    pol.rotate_logs()
    assert (logs / "ticks.jsonl.1").read_bytes().startswith(b"new")
    assert (logs / "ticks.jsonl.2").read_bytes() == b"old1"
    assert (logs / "ticks.jsonl.3").read_bytes() == b"old2"


def test_rotate_logs_keeps_only_n_files(tmp_path: Path) -> None:
    """log_keep_files = 2 → after rotation, only .1 and .2 exist."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "ticks.jsonl").write_bytes(b"a" * 200)
    (logs / "ticks.jsonl.1").write_bytes(b"b")
    (logs / "ticks.jsonl.2").write_bytes(b"c")
    (logs / "ticks.jsonl.3").write_bytes(b"d")  # should be deleted
    pol = RotationPolicy(
        logs_dir=logs, artifacts_dir=tmp_path / "art",
        log_max_bytes=100, log_keep_files=2,
    )
    pol.rotate_logs()
    assert (logs / "ticks.jsonl.1").exists()
    assert (logs / "ticks.jsonl.2").exists()
    assert not (logs / "ticks.jsonl.3").exists()
    assert not (logs / "ticks.jsonl.4").exists()


def test_rotate_logs_two_streams(tmp_path: Path) -> None:
    """Each *.jsonl stream rotates independently."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "ticks.jsonl").write_bytes(b"x" * 200)
    (logs / "errors.jsonl").write_bytes(b"y" * 200)
    pol = RotationPolicy(
        logs_dir=logs, artifacts_dir=tmp_path / "art",
        log_max_bytes=100, log_keep_files=3,
    )
    out = pol.rotate_logs()
    assert set(out.keys()) == {"ticks", "errors"}
    assert (logs / "ticks.jsonl.1").exists()
    assert (logs / "errors.jsonl.1").exists()


# ---- artifact rotation ------------------------------------------------------


def test_rotate_artifacts_no_dir_returns_zeros(tmp_path: Path) -> None:
    pol = RotationPolicy(
        logs_dir=tmp_path, artifacts_dir=tmp_path / "missing",
    )
    result = pol.rotate_artifacts()
    assert result == {"deleted": 0, "retained": 0, "total_bytes_after": 0}


def test_rotate_artifacts_under_cap_is_noop(tmp_path: Path) -> None:
    art = tmp_path / "art"
    art.mkdir()
    (art / "tick_1").mkdir()
    (art / "tick_1" / "metadata.json").write_bytes(b"x" * 100)
    pol = RotationPolicy(
        logs_dir=tmp_path, artifacts_dir=art,
        artifacts_max_bytes=1000,
    )
    out = pol.rotate_artifacts()
    assert out["deleted"] == 0
    assert out["retained"] == 1
    assert (art / "tick_1").exists()


def test_rotate_artifacts_deletes_oldest_first(tmp_path: Path) -> None:
    """Three subdirs, cap = 100 bytes (per subdir), one fits."""
    art = tmp_path / "art"
    art.mkdir()
    for name in ("tick_1", "tick_2", "tick_3"):
        d = art / name
        d.mkdir()
        (d / "metadata.json").write_bytes(b"x" * 100)
    # Set mtimes so tick_1 is the oldest, tick_3 the newest.
    t0 = time.time()
    os.utime(art / "tick_1", (t0 - 30, t0 - 30))
    os.utime(art / "tick_2", (t0 - 20, t0 - 20))
    os.utime(art / "tick_3", (t0 - 10, t0 - 10))

    pol = RotationPolicy(
        logs_dir=tmp_path, artifacts_dir=art,
        artifacts_max_bytes=150,  # only one subdir (~100 bytes) fits
    )
    out = pol.rotate_artifacts()
    assert out["deleted"] == 2
    assert out["retained"] == 1
    # The retained one is tick_3 (the newest).
    assert (art / "tick_3").exists()
    assert not (art / "tick_1").exists()
    assert not (art / "tick_2").exists()


def test_rotate_artifacts_tie_break_on_name(tmp_path: Path) -> None:
    """When mtimes tie, names sort ascending → 'a' deleted before 'z'."""
    art = tmp_path / "art"
    art.mkdir()
    for name in ("tick_a", "tick_z"):
        d = art / name
        d.mkdir()
        (d / "metadata.json").write_bytes(b"x" * 100)
    t0 = time.time()
    os.utime(art / "tick_a", (t0, t0))
    os.utime(art / "tick_z", (t0, t0))
    pol = RotationPolicy(
        logs_dir=tmp_path, artifacts_dir=art,
        artifacts_max_bytes=150,
    )
    pol.rotate_artifacts()
    # tick_a deleted (oldest by name tie-break), tick_z kept.
    assert (art / "tick_z").exists()
    assert not (art / "tick_a").exists()


def test_rotate_artifacts_handles_recursive_size(tmp_path: Path) -> None:
    """A subdir with multiple files: total size counted correctly."""
    art = tmp_path / "art"
    art.mkdir()
    d = art / "tick_big"
    d.mkdir()
    (d / "metadata.json").write_bytes(b"x" * 200)
    (d / "extra.bin").write_bytes(b"y" * 200)
    pol = RotationPolicy(
        logs_dir=tmp_path, artifacts_dir=art,
        artifacts_max_bytes=100,  # 400 bytes used; over cap → delete
    )
    out = pol.rotate_artifacts()
    assert out["deleted"] == 1
    assert not d.exists()


def test_rotate_artifacts_ignores_loose_files(tmp_path: Path) -> None:
    """Files directly under artifacts_dir aren't enumerated; only dirs."""
    art = tmp_path / "art"
    art.mkdir()
    (art / "loose.txt").write_bytes(b"y" * 100)
    (art / "tick_1").mkdir()
    (art / "tick_1" / "metadata.json").write_bytes(b"x" * 100)
    pol = RotationPolicy(
        logs_dir=tmp_path, artifacts_dir=art,
        artifacts_max_bytes=50,
    )
    out = pol.rotate_artifacts()
    assert out["deleted"] == 1  # only the dir, not the loose file
    assert (art / "loose.txt").exists()


def test_rotate_artifacts_nested_directories(tmp_path: Path) -> None:
    """Subdirectories with nested files are sized and deleted recursively."""
    art = tmp_path / "art"
    art.mkdir()
    d = art / "tick_nested"
    d.mkdir()
    (d / "sub").mkdir()
    (d / "sub" / "deep.bin").write_bytes(b"a" * 200)
    (d / "metadata.json").write_bytes(b"b" * 200)
    pol = RotationPolicy(
        logs_dir=tmp_path, artifacts_dir=art,
        artifacts_max_bytes=100,
    )
    out = pol.rotate_artifacts()
    assert out["deleted"] == 1
    assert not d.exists()


def test_rmtree_helper_on_missing_path(tmp_path: Path) -> None:
    """The internal _rmtree helper is a no-op on missing paths."""
    from automation.rotation import _rmtree
    _rmtree(tmp_path / "does_not_exist")  # must not raise


def test_rotate_logs_oserror_wraps_into_rotation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError during the rotation shift surfaces as RotationError."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "ticks.jsonl").write_bytes(b"x" * 200)
    pol = RotationPolicy(
        logs_dir=logs, artifacts_dir=tmp_path / "art",
        log_max_bytes=100, log_keep_files=3,
    )

    real_rename = Path.rename
    def _broken_rename(self, target):
        raise OSError("synthetic rename failure")
    monkeypatch.setattr(Path, "rename", _broken_rename)

    with pytest.raises(RotationError, match="log rotation failed"):
        pol.rotate_logs()


def test_rotate_artifacts_oserror_wraps_into_rotation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError during artifact enumeration surfaces as RotationError."""
    art = tmp_path / "art"
    art.mkdir()
    (art / "tick_1").mkdir()
    pol = RotationPolicy(
        logs_dir=tmp_path, artifacts_dir=art,
        artifacts_max_bytes=1,
    )

    def _broken_iterdir(self):
        raise OSError("synthetic enumeration failure")
    monkeypatch.setattr(Path, "iterdir", _broken_iterdir)

    with pytest.raises(RotationError, match="artifact rotation failed"):
        pol.rotate_artifacts()


def test_rotate_artifacts_determinism(tmp_path: Path) -> None:
    """Same FS state in → same rotation out, across two calls."""
    art = tmp_path / "art"
    art.mkdir()
    for i in range(4):
        d = art / f"tick_{i}"
        d.mkdir()
        (d / "metadata.json").write_bytes(b"x" * 100)
    t0 = time.time()
    for i in range(4):
        os.utime(art / f"tick_{i}", (t0 - (10 - i), t0 - (10 - i)))
    # Snapshot the FS state by counting directories.
    pol = RotationPolicy(
        logs_dir=tmp_path, artifacts_dir=art,
        artifacts_max_bytes=120,  # ~1 subdir fits
    )
    out1 = pol.rotate_artifacts()
    # A second call should be a no-op (already under cap).
    out2 = pol.rotate_artifacts()
    assert out2["deleted"] == 0
    assert out2["retained"] == out1["retained"]
