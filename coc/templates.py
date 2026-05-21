"""CoC template pack — declarative spec + loader.

Six hand-curated templates that together describe the
recognition surface of one trophy-drop loop:

    home_attack_button     — "Attack" button on the home (village)
                             screen. Triggers entry to matchmaking.
    find_match_button      — "Find a Match" button in the attack
                             options screen. Triggers matchmaking.
    battle_ui_indicator    — a UI element visible only when an
                             enemy village has loaded and the
                             battle is live (e.g., the troop bar,
                             the timer, the surrender button
                             region). Detection-only (no tap).
    surrender_button       — the "End Battle" / surrender button
                             during a battle.
    surrender_confirm      — the "OK" / confirm button in the
                             surrender confirmation dialog.
    return_home_button     — the "Return Home" button on the
                             battle-result screen.

Files live under `templates/<filename>.png`, loaded as grayscale
uint8 by `cv2.imread(..., IMREAD_GRAYSCALE)`. Operators capture
and crop them by hand via `scripts/coc_template_capture.py`.

No OCR. No ML. No segmentation. Pure literal template matching
per ADR-03 and the v1.0 frozen NFRs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np

from automation.errors import AutomationError
from automation.template import Template

_LOG = logging.getLogger(__name__)


DEFAULT_TEMPLATE_DIR: Path = Path("templates")


# Declarative pack — one row per logical template name. Threshold
# is the matcher's `TM_CCOEFF_NORMED` minimum confidence. 0.85 is
# the v1.0 default; operators tune downward if their captures are
# noisy (rarely below 0.75) or upward if they want strictness.
@dataclass(frozen=True)
class TemplateSpec:
    name: str
    filename: str
    threshold: float
    purpose: str


TEMPLATE_SPECS: tuple[TemplateSpec, ...] = (
    TemplateSpec(
        name="home_attack_button",
        filename="home_attack_button.png",
        threshold=0.85,
        purpose="HOME → ATTACK: tap to open attack options",
    ),
    TemplateSpec(
        name="find_match_button",
        filename="find_match_button.png",
        threshold=0.85,
        purpose="ATTACK → FIND_MATCH: tap to start matchmaking",
    ),
    TemplateSpec(
        name="battle_ui_indicator",
        filename="battle_ui_indicator.png",
        threshold=0.85,
        purpose="FIND_MATCH → WAIT_VILLAGE: detect that the enemy "
                "village has loaded (no tap)",
    ),
    TemplateSpec(
        name="surrender_button",
        filename="surrender_button.png",
        threshold=0.85,
        purpose="END_BATTLE → CONFIRM: tap surrender during battle",
    ),
    TemplateSpec(
        name="surrender_confirm",
        filename="surrender_confirm.png",
        threshold=0.85,
        purpose="CONFIRM → RETURN_HOME: tap the surrender dialog OK",
    ),
    TemplateSpec(
        name="return_home_button",
        filename="return_home_button.png",
        threshold=0.85,
        purpose="RETURN_HOME → COMPLETE: tap return home on result",
    ),
)


# Fast lookup. `EXPECTED_NAMES` is used by tests + the live
# validation script to know what to check for.
EXPECTED_NAMES: frozenset[str] = frozenset(s.name for s in TEMPLATE_SPECS)
SPECS_BY_NAME: dict[str, TemplateSpec] = {s.name: s for s in TEMPLATE_SPECS}


class TemplatePackError(AutomationError):
    """The template pack is incomplete or malformed.

    Raised by `load_template_pack` when one or more expected
    templates are missing on disk, cannot be decoded, or fail
    `automation.template.Template` validation (e.g., wrong dtype,
    non-grayscale, empty). The exception's message lists every
    missing or malformed entry so the operator can act in one
    pass.
    """


@dataclass(frozen=True)
class TemplatePack:
    """Loaded `Template` instances indexed by logical name.

    Construct via `load_template_pack(dir)`. The pack is
    immutable after construction; the underlying ndarrays are
    write-locked by `Template.__post_init__`.
    """

    templates: dict[str, Template]

    def get(self, name: str) -> Template:
        """Return the `Template` for `name`. Raises `TemplatePackError`
        if absent."""
        if name not in self.templates:
            raise TemplatePackError(
                f"template {name!r} not in pack "
                f"(have: {sorted(self.templates.keys())})"
            )
        return self.templates[name]

    def __contains__(self, name: str) -> bool:
        return name in self.templates

    def __len__(self) -> int:
        return len(self.templates)

    def __iter__(self) -> Iterator[str]:
        return iter(self.templates)

    def names(self) -> list[str]:
        return sorted(self.templates.keys())


def load_template_pack(
    template_dir: Path | str = DEFAULT_TEMPLATE_DIR,
) -> TemplatePack:
    """Load all expected templates from `template_dir`.

    Returns a `TemplatePack` keyed by the logical names declared
    in `TEMPLATE_SPECS`. Raises `TemplatePackError` if:

    - `template_dir` does not exist;
    - any expected PNG is missing on disk;
    - any PNG cannot be decoded (corrupt / unsupported format);
    - any decoded image is not uint8 grayscale.

    On TemplatePackError, the message lists every missing or
    malformed file so the operator's next pass can address them
    all at once. The script `scripts/coc_template_capture.py`
    walks the operator through capture + crop.
    """
    template_dir = Path(template_dir)
    if not template_dir.is_dir():
        raise TemplatePackError(
            f"template_dir does not exist: {template_dir.resolve()}"
        )

    loaded: dict[str, Template] = {}
    missing: list[str] = []
    bad: list[str] = []

    for spec in TEMPLATE_SPECS:
        path = template_dir / spec.filename
        if not path.is_file():
            missing.append(spec.filename)
            continue
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            bad.append(f"{spec.filename} (could not decode)")
            continue
        if img.dtype != np.uint8:
            bad.append(f"{spec.filename} (dtype={img.dtype}, expected uint8)")
            continue
        if img.ndim != 2:
            bad.append(f"{spec.filename} (ndim={img.ndim}, expected 2)")
            continue
        if img.size == 0:
            bad.append(f"{spec.filename} (empty)")
            continue
        try:
            template = Template(
                name=spec.name,
                image_gray=img,
                width=int(img.shape[1]),
                height=int(img.shape[0]),
                threshold=spec.threshold,
                roi=None,
            )
        except Exception as exc:  # noqa: BLE001 — surface any validation fault
            bad.append(f"{spec.filename} (Template ctor: {exc})")
            continue
        loaded[spec.name] = template
        _LOG.debug(
            "loaded template %s from %s (%dx%d)",
            spec.name, path, template.width, template.height,
        )

    if missing or bad:
        parts: list[str] = []
        if missing:
            parts.append(f"missing: {missing}")
        if bad:
            parts.append(f"malformed: {bad}")
        raise TemplatePackError(
            "template pack incomplete: "
            + "; ".join(parts)
            + ". Capture via `python -m scripts.coc_template_capture <label>` "
              "then crop the displayed PNG to the named files under "
              f"{template_dir.resolve()}/."
        )

    return TemplatePack(templates=loaded)


def required_filenames() -> list[str]:
    """List of PNG filenames the pack expects under `templates/`."""
    return [s.filename for s in TEMPLATE_SPECS]


def required_specs() -> list[TemplateSpec]:
    """Full specs (name, filename, threshold, purpose)."""
    return list(TEMPLATE_SPECS)


__all__ = [
    "TemplateSpec",
    "TEMPLATE_SPECS",
    "EXPECTED_NAMES",
    "SPECS_BY_NAME",
    "DEFAULT_TEMPLATE_DIR",
    "TemplatePack",
    "TemplatePackError",
    "load_template_pack",
    "required_filenames",
    "required_specs",
]
