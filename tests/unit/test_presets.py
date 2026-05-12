from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from jazz_guru.actions.tools.presets import (
    preset_delete,
    preset_get,
    preset_list,
    preset_upsert,
)
from jazz_guru.config import get_settings
from jazz_guru.presets import (
    Preset,
    PresetValidationError,
    clear_presets_cache,
    load_presets,
    save_presets,
    validate_preset,
)


@pytest.fixture
def presets_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the presets module at an empty file in tmp_path."""
    f = tmp_path / "instruments.yaml"
    f.write_text(yaml.safe_dump({"version": 1, "default": None, "presets": {}}))
    monkeypatch.setattr(get_settings(), "jg_instruments_file", f)
    monkeypatch.setattr(get_settings(), "jg_instruments_root", tmp_path)
    clear_presets_cache()
    return f


def _make_sfz(tmp_path: Path, name: str = "lib.sfz") -> Path:
    p = tmp_path / name
    p.write_text("// stub\n")
    return p


def test_load_save_round_trip(presets_file: Path, tmp_path: Path) -> None:
    f = load_presets()
    assert f.presets == {}
    sfz = _make_sfz(tmp_path)
    f.presets["x"] = Preset(engine="sfizz", library=sfz.name, description="x")
    f.default = "x"
    save_presets(f)

    # Re-read from disk.
    again = load_presets()
    assert again.default == "x"
    assert again.presets["x"].engine == "sfizz"
    assert again.presets["x"].library == sfz.name


def test_mtime_cache_invalidates_on_write(presets_file: Path, tmp_path: Path) -> None:
    load_presets()  # warm the cache
    sfz = _make_sfz(tmp_path)
    # Bypass save_presets so the cache isn't manually cleared — only mtime change matters.
    time.sleep(0.01)  # ensure mtime tick on coarse-grained filesystems
    presets_file.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "default": "y",
                "presets": {"y": {"engine": "sfizz", "library": sfz.name}},
            }
        )
    )
    f = load_presets()
    assert f.default == "y"
    assert "y" in f.presets


def test_validate_preset_rejects_bad_name(tmp_path: Path) -> None:
    p = Preset(engine="fluidsynth", library=None)
    with pytest.raises(PresetValidationError):
        validate_preset("has spaces", p, require_library=False)
    with pytest.raises(PresetValidationError):
        validate_preset("", p, require_library=False)


def test_validate_preset_requires_library_for_sfizz(tmp_path: Path) -> None:
    p = Preset(engine="sfizz", library=None)
    with pytest.raises(PresetValidationError):
        validate_preset("ok-name", p, require_library=False)


def test_validate_preset_checks_library_exists(
    presets_file: Path, tmp_path: Path
) -> None:
    p = Preset(engine="sfizz", library="missing.sfz")
    with pytest.raises(PresetValidationError):
        validate_preset("ok-name", p, require_library=True)
    # require_library=False skips the existence check.
    validate_preset("ok-name", p, require_library=False)


def test_validate_preset_allows_null_library_for_fluidsynth() -> None:
    p = Preset(engine="fluidsynth", library=None)
    validate_preset("gm", p, require_library=False)


async def test_preset_list_empty(presets_file: Path) -> None:
    out = await preset_list()
    assert out == {"default": None, "presets": []}


async def test_preset_upsert_creates_and_sets_default(
    presets_file: Path, tmp_path: Path
) -> None:
    sfz = _make_sfz(tmp_path, "piano.sfz")
    out = await preset_upsert(
        name="my-piano",
        engine="sfizz",
        library=sfz.name,
        description="test piano",
    )
    assert out["status"] == "created"
    # First preset becomes default automatically.
    assert out["default"] == "my-piano"

    got = await preset_get(name="my-piano")
    assert got["engine"] == "sfizz"
    assert got["library"] == sfz.name


async def test_preset_upsert_updates_existing(
    presets_file: Path, tmp_path: Path
) -> None:
    sfz = _make_sfz(tmp_path, "a.sfz")
    await preset_upsert(name="a", engine="sfizz", library=sfz.name)
    out = await preset_upsert(name="a", engine="sfizz", library=sfz.name, description="updated")
    assert out["status"] == "updated"
    assert (await preset_get(name="a"))["description"] == "updated"


async def test_preset_upsert_rejects_invalid_engine(presets_file: Path) -> None:
    out = await preset_upsert(name="bad", engine="not-a-real-engine")
    assert "error" in out


async def test_preset_upsert_rejects_missing_library_by_default(
    presets_file: Path,
) -> None:
    out = await preset_upsert(name="x", engine="sfizz", library="not-on-disk.sfz")
    assert "error" in out
    assert "not found" in out["error"]


async def test_preset_upsert_can_skip_library_existence_check(
    presets_file: Path,
) -> None:
    out = await preset_upsert(
        name="x", engine="sfizz", library="not-on-disk.sfz",
        require_library_exists=False,
    )
    assert out["status"] == "created"


async def test_preset_delete_refuses_default(presets_file: Path, tmp_path: Path) -> None:
    sfz = _make_sfz(tmp_path)
    await preset_upsert(name="only", engine="sfizz", library=sfz.name)
    # 'only' is the default now.
    out = await preset_delete(name="only")
    assert "error" in out
    assert "default" in out["error"]


async def test_preset_delete_removes_non_default(
    presets_file: Path, tmp_path: Path
) -> None:
    sfz_a = _make_sfz(tmp_path, "a.sfz")
    sfz_b = _make_sfz(tmp_path, "b.sfz")
    await preset_upsert(name="a", engine="sfizz", library=sfz_a.name)
    await preset_upsert(name="b", engine="sfizz", library=sfz_b.name)
    # 'a' is default. Delete 'b'.
    out = await preset_delete(name="b")
    assert out["status"] == "removed"
    listing = await preset_list()
    assert [p["name"] for p in listing["presets"]] == ["a"]


async def test_preset_get_unknown(presets_file: Path) -> None:
    out = await preset_get(name="ghost")
    assert out["error"].startswith("unknown preset")
