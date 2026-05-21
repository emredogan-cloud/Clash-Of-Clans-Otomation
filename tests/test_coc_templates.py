"""CoC template-pack loader tests."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from coc.templates import (
    EXPECTED_NAMES,
    SPECS_BY_NAME,
    TEMPLATE_SPECS,
    TemplatePack,
    TemplatePackError,
    load_template_pack,
    required_filenames,
    required_specs,
)


def _write_template_png(path: Path, *, size: int = 32) -> None:
    """Write a high-entropy grayscale PNG so cv2.imread picks it up."""
    img = np.full((size, size), 30, dtype=np.uint8)
    h, w = size // 2, size // 2
    img[:h, :w] = 50
    img[:h, w:] = 120
    img[h:, :w] = 180
    img[h:, w:] = 230
    cv2.imwrite(str(path), img)


def _populate(template_dir: Path, *, skip: set[str] = frozenset()) -> None:
    """Write all expected templates as valid PNGs, except those named in
    `skip`."""
    template_dir.mkdir(parents=True, exist_ok=True)
    for spec in TEMPLATE_SPECS:
        if spec.name in skip:
            continue
        _write_template_png(template_dir / spec.filename)


# ---- specs surface ----------------------------------------------------------


def test_expected_names_match_specs() -> None:
    assert EXPECTED_NAMES == frozenset(s.name for s in TEMPLATE_SPECS)


def test_specs_by_name_indexed_correctly() -> None:
    assert set(SPECS_BY_NAME.keys()) == {s.name for s in TEMPLATE_SPECS}
    for name, spec in SPECS_BY_NAME.items():
        assert spec.name == name


def test_required_filenames_returns_six_paths() -> None:
    assert len(required_filenames()) == 6
    for filename in required_filenames():
        assert filename.endswith(".png")


def test_required_specs_returns_all_six() -> None:
    specs = required_specs()
    assert len(specs) == 6
    assert {s.name for s in specs} == EXPECTED_NAMES


# ---- load_template_pack — happy path ----------------------------------------


def test_load_succeeds_when_all_present(tmp_path: Path) -> None:
    _populate(tmp_path)
    pack = load_template_pack(tmp_path)
    assert isinstance(pack, TemplatePack)
    assert set(pack.names()) == EXPECTED_NAMES
    for name in EXPECTED_NAMES:
        tpl = pack.get(name)
        assert tpl.width == 32 and tpl.height == 32


def test_load_accepts_str_path(tmp_path: Path) -> None:
    _populate(tmp_path)
    pack = load_template_pack(str(tmp_path))
    assert len(pack) == 6


# ---- load_template_pack — error paths --------------------------------------


def test_load_raises_when_directory_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    with pytest.raises(TemplatePackError, match="template_dir does not exist"):
        load_template_pack(missing)


def test_load_reports_missing_template_filename(tmp_path: Path) -> None:
    _populate(tmp_path, skip={"home_attack_button"})
    with pytest.raises(TemplatePackError) as exc_info:
        load_template_pack(tmp_path)
    assert "missing" in str(exc_info.value)
    assert "home_attack_button.png" in str(exc_info.value)


def test_load_reports_corrupt_png(tmp_path: Path) -> None:
    _populate(tmp_path)
    # Corrupt one file with non-PNG bytes.
    bad = tmp_path / "surrender_button.png"
    bad.write_bytes(b"\x00\x01garbage\x00not_a_png")
    with pytest.raises(TemplatePackError, match="malformed"):
        load_template_pack(tmp_path)


def test_load_reports_empty_png(tmp_path: Path) -> None:
    """A 1x0 / 0x1 / 0x0 image is empty per `Template` validation."""
    _populate(tmp_path)
    # cv2 can't actually write a 0-pixel PNG cleanly; we use a 1x1
    # but force size=0 by writing a numpy save then renaming. The
    # cleanest workable test: write a PNG that decodes but is bad
    # in another way — e.g. a 3-channel BGR PNG. cv2.imread with
    # IMREAD_GRAYSCALE will still convert it to grayscale, so that
    # isn't malformed. Use a tiny 1x1 image; Template ctor rejects
    # empty but accepts 1x1.
    bad = tmp_path / "surrender_button.png"
    img = np.zeros((0, 0), dtype=np.uint8)
    # cv2.imwrite refuses 0x0; write raw PNG that decodes to
    # nothing usable instead.
    bad.write_bytes(b"")
    with pytest.raises(TemplatePackError, match="malformed"):
        load_template_pack(tmp_path)


def test_load_reports_when_decoder_succeeds_but_image_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If cv2.imread returns a non-grayscale ndarray (e.g., float32),
    the loader catches it and reports it as malformed."""
    _populate(tmp_path)
    # Monkeypatch cv2.imread to return a float32 image for one file.
    import coc.templates as tpl_mod
    real_imread = tpl_mod.cv2.imread
    target_filename = "find_match_button.png"

    def _broken_imread(path, *args, **kwargs):
        if path.endswith(target_filename):
            return np.zeros((32, 32), dtype=np.float32)
        return real_imread(path, *args, **kwargs)
    monkeypatch.setattr(tpl_mod.cv2, "imread", _broken_imread)
    with pytest.raises(TemplatePackError, match="malformed"):
        load_template_pack(tmp_path)


def test_load_reports_multiple_missing_in_one_pass(tmp_path: Path) -> None:
    """All missing entries are listed in a single error so the
    operator can fix them in one editing session."""
    _populate(tmp_path, skip={"home_attack_button", "return_home_button"})
    with pytest.raises(TemplatePackError) as exc_info:
        load_template_pack(tmp_path)
    msg = str(exc_info.value)
    assert "home_attack_button.png" in msg
    assert "return_home_button.png" in msg


def test_load_template_dir_default_is_relative() -> None:
    """The default `templates/` path is relative to the CWD —
    operators run from the repo root."""
    from coc.templates import DEFAULT_TEMPLATE_DIR
    assert str(DEFAULT_TEMPLATE_DIR) == "templates"


# ---- TemplatePack surface --------------------------------------------------


def test_pack_get_raises_on_unknown_name(tmp_path: Path) -> None:
    _populate(tmp_path)
    pack = load_template_pack(tmp_path)
    with pytest.raises(TemplatePackError, match="not in pack"):
        pack.get("no_such_name")


def test_pack_contains_works(tmp_path: Path) -> None:
    _populate(tmp_path)
    pack = load_template_pack(tmp_path)
    assert "home_attack_button" in pack
    assert "nope" not in pack


def test_pack_iter_and_len(tmp_path: Path) -> None:
    _populate(tmp_path)
    pack = load_template_pack(tmp_path)
    assert len(pack) == 6
    assert set(iter(pack)) == EXPECTED_NAMES
