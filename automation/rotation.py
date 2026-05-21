"""Bounded-disk-growth rotation for logs and artifacts.

Phase 6 owns two on-disk surfaces that can grow indefinitely:

- `var/logs/*.jsonl` — structured logs written by `StructuredLogger`.
  Default cap: 10 MB per file, keep 5 rotated files per stream
  (`<stream>.jsonl`, `<stream>.jsonl.1`, …, `<stream>.jsonl.5`).

- `var/artifacts/orchestrator/<dir>/` — per-tick metadata.json
  directories written by `Orchestrator._write_artifacts`. Default
  cap: 500 MB total across all subdirectories. The cap is on
  cumulative size; the rotation policy deletes the *oldest*
  directories (by mtime) until the cap is met.

Both rotations are **deterministic** and **synchronous**:

- No background thread, no daemon, no asyncio task.
- The orchestrator (or any caller) invokes `rotate_logs()` and
  `rotate_artifacts()` explicitly. Per the Phase 6 prompt's
  prohibition on daemons and async, the caller decides cadence.
- Same inputs → same outputs. The selection of "oldest" uses mtime
  with a stable tie-break on filename, so two rotation calls
  against the same directory state produce the same result.

The module exposes one class — `RotationPolicy` — plus the typed
exception in `automation.errors.RotationError`. Pure stdlib.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .errors import RotationError

_LOG = logging.getLogger(__name__)


# Defaults sized for v1.0 single-device throughput; see
# `phase6-report.md` for the bench data that justifies them.
DEFAULT_LOG_MAX_BYTES: int = 10 * 1024 * 1024  # 10 MB
DEFAULT_LOG_KEEP_FILES: int = 5
DEFAULT_ARTIFACTS_MAX_BYTES: int = 500 * 1024 * 1024  # 500 MB


@dataclass(frozen=True)
class RotationPolicy:
    """Bounded-disk-growth rotation owner.

    All paths are absolute. The policy never creates files; it only
    moves and deletes them. Callers are responsible for ensuring
    the target directories exist (this typically falls out of the
    `automation.paths.ensure_runtime_dirs()` boilerplate).

    Field semantics:

    - `logs_dir`              : the directory holding `*.jsonl` files.
    - `log_max_bytes`         : per-file cap. When a stream's file
                                exceeds this, it is rotated.
    - `log_keep_files`        : how many rotated files to retain per
                                stream (1 → only `stream.jsonl.1`).
    - `artifacts_dir`         : the directory holding per-tick
                                subdirectories.
    - `artifacts_max_bytes`   : cumulative cap across the directory
                                tree. The policy deletes oldest-first
                                subdirectories until under the cap.
    """

    logs_dir: Path
    artifacts_dir: Path
    log_max_bytes: int = DEFAULT_LOG_MAX_BYTES
    log_keep_files: int = DEFAULT_LOG_KEEP_FILES
    artifacts_max_bytes: int = DEFAULT_ARTIFACTS_MAX_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.logs_dir, Path):
            raise TypeError(
                f"logs_dir must be Path, got {type(self.logs_dir).__name__}"
            )
        if not isinstance(self.artifacts_dir, Path):
            raise TypeError(
                f"artifacts_dir must be Path, got {type(self.artifacts_dir).__name__}"
            )
        for label, value in (
            ("log_max_bytes", self.log_max_bytes),
            ("log_keep_files", self.log_keep_files),
            ("artifacts_max_bytes", self.artifacts_max_bytes),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(
                    f"{label} must be int, got {type(value).__name__}"
                )
            if value <= 0:
                raise ValueError(f"{label} must be > 0, got {value}")

    # ---- logs --------------------------------------------------------

    def rotate_logs(self) -> dict[str, int]:
        """Rotate every `*.jsonl` stream in `logs_dir` whose size > cap.

        For each stream `<name>.jsonl` exceeding `log_max_bytes`:

        1. Shift the existing rotated files one slot down
           (`<name>.jsonl.N-1` → `<name>.jsonl.N`).
        2. Move the *current* `<name>.jsonl` to `<name>.jsonl.1`.
        3. Delete any file beyond `log_keep_files`.

        Returns a dict mapping stream name → number of files now
        retained (live `.jsonl` not counted; rotated `.jsonl.N` only).
        Streams that did not need rotation are absent from the return
        dict.
        """
        if not self.logs_dir.is_dir():
            return {}
        rotated: dict[str, int] = {}
        try:
            for live in sorted(self.logs_dir.glob("*.jsonl")):
                if not live.is_file():
                    continue
                size = live.stat().st_size
                if size <= self.log_max_bytes:
                    continue
                stream = live.stem  # "ticks", "errors", etc.
                self._rotate_one_stream(stream)
                rotated[stream] = self._count_rotated(stream)
        except OSError as exc:
            raise RotationError(f"log rotation failed: {exc}") from exc
        return rotated

    def _rotate_one_stream(self, stream: str) -> None:
        """Shift rotated files one slot down, then move live to .1.

        Order matters: rotate the highest-numbered file first so
        nothing is overwritten. After this pass:

            <stream>.jsonl     (does not exist after this call)
            <stream>.jsonl.1   (was <stream>.jsonl before)
            <stream>.jsonl.2   (was <stream>.jsonl.1 before)
            ...
        """
        live = self.logs_dir / f"{stream}.jsonl"
        # First: delete anything beyond log_keep_files. We do this
        # *before* the shift so the shift's "rename N to N+1" pass
        # has no slot to overflow into.
        for i in range(self.log_keep_files, self.log_keep_files * 4):
            stale = self.logs_dir / f"{stream}.jsonl.{i + 1}"
            if stale.exists():
                stale.unlink()
        # Now shift down: walk from log_keep_files down to 1.
        for i in range(self.log_keep_files, 0, -1):
            src = self.logs_dir / f"{stream}.jsonl.{i}"
            dst = self.logs_dir / f"{stream}.jsonl.{i + 1}"
            if src.exists():
                # i == log_keep_files means src is about to become
                # one beyond the budget; drop it instead of shifting.
                if i >= self.log_keep_files:
                    src.unlink()
                    continue
                src.rename(dst)
        # Finally: rename live → .1.
        if live.exists():
            live.rename(self.logs_dir / f"{stream}.jsonl.1")

    def _count_rotated(self, stream: str) -> int:
        return sum(
            1
            for p in self.logs_dir.glob(f"{stream}.jsonl.*")
            if p.is_file()
        )

    # ---- artifacts ---------------------------------------------------

    def rotate_artifacts(self) -> dict[str, int]:
        """Delete oldest-first subdirectories until under the byte cap.

        Returns a dict with keys:

        - `"deleted"` — count of subdirectories deleted this pass.
        - `"retained"` — count of subdirectories still present.
        - `"total_bytes_after"` — cumulative size still on disk.

        The policy operates on *immediate subdirectories* of
        `artifacts_dir`. Files directly under `artifacts_dir` are
        ignored (Phase 5/6 artifacts live one directory level deep).

        Selection of "oldest" is by `Path.stat().st_mtime`; ties
        broken by the directory name (sorted ascending). This makes
        the rotation deterministic across re-runs against the same
        FS state, which is what tests require.
        """
        if not self.artifacts_dir.is_dir():
            return {"deleted": 0, "retained": 0, "total_bytes_after": 0}

        try:
            entries: list[tuple[float, str, Path, int]] = []
            for sub in self.artifacts_dir.iterdir():
                if not sub.is_dir():
                    continue
                size = _tree_size_bytes(sub)
                mtime = sub.stat().st_mtime
                entries.append((mtime, sub.name, sub, size))
            total = sum(e[3] for e in entries)
            if total <= self.artifacts_max_bytes:
                return {
                    "deleted": 0,
                    "retained": len(entries),
                    "total_bytes_after": total,
                }
            # Oldest first; tie-break on name for determinism.
            entries.sort(key=lambda e: (e[0], e[1]))
            deleted = 0
            for mtime, name, path, size in entries:
                if total <= self.artifacts_max_bytes:
                    break
                _rmtree(path)
                total -= size
                deleted += 1
            return {
                "deleted": deleted,
                "retained": len(entries) - deleted,
                "total_bytes_after": total,
            }
        except OSError as exc:
            raise RotationError(f"artifact rotation failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tree_size_bytes(root: Path) -> int:
    """Cumulative byte size of `root` (recursive). Best-effort."""
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            # Race with concurrent deletion; ignore.
            continue
    return total


def _rmtree(path: Path) -> None:
    """`shutil.rmtree` equivalent, with no-op on missing path.

    Re-implemented locally to avoid the side-effects of `shutil` (it
    lazy-imports `os`, `stat`; and we want exact, auditable behavior).
    """
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir():
            _rmtree(child)
        else:
            try:
                child.unlink()
            except OSError:
                pass
    try:
        path.rmdir()
    except OSError:
        # Directory not empty (e.g. race) — try again with shutil.
        import shutil
        shutil.rmtree(path, ignore_errors=True)


__all__ = [
    "RotationPolicy",
    "DEFAULT_LOG_MAX_BYTES",
    "DEFAULT_LOG_KEEP_FILES",
    "DEFAULT_ARTIFACTS_MAX_BYTES",
]
