#!/usr/bin/env python3
"""Replay one orchestrator tick for human inspection.

Reads a `metadata.json` (the per-tick sidecar produced by
`Orchestrator._write_artifacts`) and prints a structured,
human-readable summary:

- correlation id
- state flow (state_before → state_after)
- success flag
- tier (search_only / validated / validated_retry)
- the four latency surfaces (tick, capture, match, action)
- retries used
- search-match coordinates + confidence
- action result (if any)
- validation outcome (if any)

This is *strictly* a replay tool. It does NOT contact the device,
does NOT re-run the matcher, does NOT execute any actions. It parses
the on-disk artifact, validates its shape, and prints it.

Usage:

    .venv/bin/python -m scripts.replay_tick PATH_TO_METADATA_JSON
    .venv/bin/python -m scripts.replay_tick PATH_TO_DIR_CONTAINING_METADATA

If a directory is passed, the script looks for `metadata.json`
directly inside. If a glob-pattern is convenient, the caller is
expected to expand it via shell.

Exit code 0 on a clean parse, non-zero on missing file or schema
violation.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO


REQUIRED_TOP_KEYS = {
    "correlation_id",
    "tier",
    "tick",
    "template",
    "search_match",
    "action_result",
    "validation_match",
    "retries_used",
}

REQUIRED_TICK_KEYS = {
    "state_before",
    "state_after",
    "success",
    "tick_latency_ms",
    "capture_latency_ms",
    "match_latency_ms",
    "action_latency_ms",
    "ts",
}


def _resolve_metadata_path(arg: str) -> Path:
    """If `arg` is a directory, look for `metadata.json` inside."""
    path = Path(arg)
    if path.is_dir():
        candidate = path / "metadata.json"
        if not candidate.is_file():
            raise FileNotFoundError(
                f"{path} is a directory but contains no metadata.json"
            )
        return candidate
    if not path.is_file():
        raise FileNotFoundError(f"not a file: {path}")
    return path


def load_tick_metadata(path: Path) -> dict[str, Any]:
    """Parse + validate the metadata.json schema. Raises on shape mismatch."""
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(
            f"metadata.json root must be an object, got {type(payload).__name__}"
        )
    missing = REQUIRED_TOP_KEYS - payload.keys()
    if missing:
        raise ValueError(
            f"metadata.json missing required top-level keys: {sorted(missing)}"
        )
    tick = payload["tick"]
    if not isinstance(tick, dict):
        raise ValueError("metadata.json `tick` must be an object")
    tick_missing = REQUIRED_TICK_KEYS - tick.keys()
    if tick_missing:
        raise ValueError(
            f"metadata.json `tick` missing required keys: {sorted(tick_missing)}"
        )
    return payload


def format_replay(payload: dict[str, Any]) -> str:
    """Return the human-readable replay text. Pure function."""
    tick = payload["tick"]
    correlation_id = payload["correlation_id"]
    tier = payload["tier"]
    retries = payload["retries_used"]

    verdict = "OK" if tick["success"] else "FAIL"
    state_flow = f"{tick['state_before']} → {tick['state_after']}"

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"Tick replay  {correlation_id}")
    lines.append("=" * 72)
    lines.append(f"  ts:             {tick['ts']}")
    lines.append(f"  verdict:        {verdict}")
    lines.append(f"  state flow:     {state_flow}")
    lines.append(f"  tier:           {tier}")
    lines.append(f"  retries used:   {retries}")
    lines.append("")
    lines.append("Latencies (ms):")
    lines.append(f"  tick total:     {tick['tick_latency_ms']:.2f}")
    lines.append(f"  capture:        {tick['capture_latency_ms']:.2f}")
    lines.append(f"  match:          {tick['match_latency_ms']:.2f}")
    action_ms = tick["action_latency_ms"]
    if action_ms is None:
        lines.append("  action:         — (no action)")
    else:
        lines.append(f"  action:         {action_ms:.2f}")
    lines.append("")

    template = payload["template"]
    lines.append("Template:")
    lines.append(
        f"  name={template['name']!r}  "
        f"size={template['width']}x{template['height']}  "
        f"threshold={template['threshold']:.3f}"
    )
    lines.append("")

    sm = payload["search_match"]
    lines.append("Search match:")
    if sm["found"]:
        center = sm.get("center")
        lines.append(
            f"  HIT  confidence={sm['confidence']:.4f}  "
            f"at ({sm['x']},{sm['y']})  "
            f"size={sm['width']}x{sm['height']}  "
            f"center={tuple(center) if center else '—'}  "
            f"mode={sm['search_mode']}"
        )
    else:
        lines.append(
            f"  MISS  confidence={sm['confidence']:.4f}  "
            f"mode={sm['search_mode']}"
        )
    lines.append("")

    ar = payload["action_result"]
    if ar is None:
        lines.append("Action result: — (no action ran)")
    else:
        ok = "OK" if ar["success"] else "FAIL"
        lines.append(
            f"Action result:  {ok}  type={ar['action_type']}  "
            f"device=({ar['device_x']},{ar['device_y']})  "
            f"latency={ar['latency_ms']:.2f} ms"
        )
    lines.append("")

    vm = payload["validation_match"]
    if vm is None:
        lines.append("Validation:    — (validation did not run)")
    else:
        if vm["found"]:
            lines.append(
                f"Validation:    FAIL — template still present  "
                f"confidence={vm['confidence']:.4f}"
            )
        else:
            lines.append(
                f"Validation:    OK — template gone  "
                f"confidence={vm['confidence']:.4f}"
            )

    lines.append("=" * 72)
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, out: TextIO | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="replay_tick",
        description=(
            "Print a human-readable replay of one orchestrator tick "
            "from its metadata.json artifact."
        ),
    )
    parser.add_argument(
        "metadata",
        help=(
            "path to metadata.json, or to a directory containing one "
            "(e.g. var/artifacts/orchestrator/<correlation>_*_*/ )"
        ),
    )
    args = parser.parse_args(argv)
    output = out if out is not None else sys.stdout

    try:
        path = _resolve_metadata_path(args.metadata)
        payload = load_tick_metadata(path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"replay_tick: {exc}", file=sys.stderr)
        return 1

    print(format_replay(payload), file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
