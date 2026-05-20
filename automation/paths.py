"""Single source of truth for runtime filesystem layout.

Resolves the repository root from this module's location and exposes the
`var/` runtime tree. Any other module that touches a runtime path must
import from here; do not hardcode paths elsewhere.

Layout:

    <ROOT>/
        automation/
        var/              ← runtime tree (gitignored, created by bootstrap)
            logs/
            metrics/
            artifacts/
            tmp/

Use `ensure_runtime_dirs()` once at process start to create the tree.
"""
from __future__ import annotations

from pathlib import Path

ROOT: Path = Path(__file__).resolve().parent.parent
VAR: Path = ROOT / "var"
LOGS: Path = VAR / "logs"
METRICS: Path = VAR / "metrics"
ARTIFACTS: Path = VAR / "artifacts"
TMP: Path = VAR / "tmp"

RUNTIME_DIRS: tuple[Path, ...] = (LOGS, METRICS, ARTIFACTS, TMP)


def ensure_runtime_dirs() -> tuple[Path, ...]:
    """Create the runtime directory tree if absent. Idempotent."""
    for d in RUNTIME_DIRS:
        d.mkdir(parents=True, exist_ok=True)
    return RUNTIME_DIRS
