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
    # Poll for an actual mtime change rather than sleeping a fixed interval: HFS+ and
    # some networked filesystems have 1s mtime granularity, so 0.01s isn't enough.
    before_ns = presets_file.stat().st_mtime_ns
    presets_file.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "default": "y",
                "presets": {"y": {"engine": "sfizz", "library": sfz.name}},
            }
        )
    )
    deadline = time.monotonic() + 2.0
    while presets_file.stat().st_mtime_ns == before_ns:
        if time.monotonic() > deadline:
            pytest.fail("presets file mtime did not advance within 2s")
        time.sleep(0.01)
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


def test_validate_preset_checks_library_exists(presets_file: Path, tmp_path: Path) -> None:
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


async def test_preset_upsert_creates_and_sets_default(presets_file: Path, tmp_path: Path) -> None:
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


async def test_preset_upsert_updates_existing(presets_file: Path, tmp_path: Path) -> None:
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
        name="x",
        engine="sfizz",
        library="not-on-disk.sfz",
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


async def test_preset_delete_removes_non_default(presets_file: Path, tmp_path: Path) -> None:
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


async def test_preset_upsert_rejects_library_outside_safe_roots(
    presets_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absolute library paths that escape every safe root must be rejected."""
    # Point the instruments root somewhere DIFFERENT than the SFZ file's parent.
    other_root = tmp_path / "other_root"
    other_root.mkdir()
    monkeypatch.setattr(get_settings(), "jg_instruments_root", other_root)
    # Force resolve_in_safe's roots list to drop tmp_path; safe_roots() includes
    # jg_instruments_root and data_dir but not arbitrary parents of tmp_path.
    monkeypatch.setattr(get_settings(), "jg_data_dir", other_root)
    sfz = _make_sfz(tmp_path, "outside.sfz")
    out = await preset_upsert(
        name="bad",
        engine="sfizz",
        library=str(sfz.resolve()),  # absolute path outside instruments root
        require_library_exists=False,
    )
    assert "error" in out
    assert "rejected" in out["error"] or "safe root" in out["error"]


def test_update_presets_holds_lock_across_load_and_save(
    presets_file: Path, tmp_path: Path
) -> None:
    """update_presets must see disk state as of the moment it grabbed the lock,
    not stale cached state from before."""
    from jazz_guru.presets import update_presets

    sfz = _make_sfz(tmp_path, "x.sfz")
    # Seed via the public surface so the cache reflects what's on disk.
    save_presets(
        load_presets().model_copy(
            update={
                "default": "x",
                "presets": {"x": Preset(engine="sfizz", library=sfz.name)},
            }
        )
    )
    load_presets()  # warm cache

    # Concurrent external write: bypass the lock + cache, simulate another writer.
    presets_file.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "default": "x",
                "presets": {
                    "x": {"engine": "sfizz", "library": sfz.name},
                    "z": {"engine": "sfizz", "library": sfz.name},
                },
            }
        )
    )

    def _mutator(f) -> None:
        # We should see 'z' because update_presets drops the cache before re-reading.
        assert "z" in f.presets, "update_presets read stale cached state"
        f.presets["w"] = Preset(engine="fluidsynth", library=None)

    update_presets(_mutator)
    on_disk = yaml.safe_load(presets_file.read_text())
    assert set(on_disk["presets"].keys()) == {"x", "z", "w"}


def test_save_presets_clears_cache_even_on_failure(
    presets_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed save must invalidate the cache so memory and disk don't diverge."""
    import jazz_guru.presets as presets_mod

    sfz = _make_sfz(tmp_path)
    f = load_presets()
    f.presets["x"] = Preset(engine="sfizz", library=sfz.name)
    f.default = "x"
    save_presets(f)
    load_presets()  # warm cache

    # Force os.replace to blow up so save_presets fails mid-write.
    def _boom(*_a: object, **_k: object) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(presets_mod.os, "replace", _boom)
    with pytest.raises(OSError, match="simulated rename failure"):
        save_presets(f)

    # Cache must be empty; reading falls back to disk.
    assert presets_mod._cache is None
