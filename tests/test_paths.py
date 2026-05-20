"""Paths module: layout and idempotency."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def test_paths_resolve_under_repo_root() -> None:
    import automation.paths as paths

    assert paths.ROOT.is_dir()
    assert paths.VAR == paths.ROOT / "var"
    assert paths.LOGS == paths.VAR / "logs"
    assert paths.METRICS == paths.VAR / "metrics"
    assert paths.ARTIFACTS == paths.VAR / "artifacts"
    assert paths.TMP == paths.VAR / "tmp"
    assert set(paths.RUNTIME_DIRS) == {paths.LOGS, paths.METRICS, paths.ARTIFACTS, paths.TMP}


def test_ensure_runtime_dirs_creates_and_is_idempotent(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    import automation.paths as paths

    var = tmp_path / "var"
    logs = var / "logs"
    metrics = var / "metrics"
    artifacts = var / "artifacts"
    tmp = var / "tmp"
    monkeypatch.setattr(paths, "VAR", var)
    monkeypatch.setattr(paths, "LOGS", logs)
    monkeypatch.setattr(paths, "METRICS", metrics)
    monkeypatch.setattr(paths, "ARTIFACTS", artifacts)
    monkeypatch.setattr(paths, "TMP", tmp)
    monkeypatch.setattr(paths, "RUNTIME_DIRS", (logs, metrics, artifacts, tmp))

    out = paths.ensure_runtime_dirs()
    for d in out:
        assert d.is_dir()
    # Second call must not raise.
    paths.ensure_runtime_dirs()
    for d in out:
        assert d.is_dir()
