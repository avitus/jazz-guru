from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jazz_guru.actions.tools.render import (
    PostProcess,
    _build_filter_chain,
    _load_presets,
    _merge_post,
    _resolve_library,
)
from jazz_guru.config import get_settings


def test_build_filter_chain_empty_when_all_off() -> None:
    pp = PostProcess()
    assert _build_filter_chain(pp) == []


def test_build_filter_chain_orders_filters() -> None:
    pp = PostProcess(
        lowpass_hz=4500, lowpass_q=0.7,
        vibrato_hz=4.8, vibrato_depth=0.05,
        gain_db=-1.5, normalize=True,
    )
    chain = _build_filter_chain(pp)
    assert chain[0] == "lowpass=f=4500.0:t=q:w=0.7"
    assert chain[1].startswith("vibrato=f=4.8")
    assert chain[2].startswith("volume=-1.5dB")
    assert chain[3].startswith("loudnorm=")


def test_build_filter_chain_clamps_vibrato_depth() -> None:
    pp = PostProcess(vibrato_hz=5.0, vibrato_depth=2.5)
    chain = _build_filter_chain(pp)
    assert "d=1.0" in chain[0] or "d=1" in chain[0]


def test_merge_post_overrides_only_set_fields() -> None:
    preset = {"lowpass_hz": 4500, "vibrato_hz": 4.8, "vibrato_depth": 0.05}
    override = PostProcess(lowpass_hz=3000)  # only this is touched
    merged = _merge_post(preset, override)
    assert merged.lowpass_hz == 3000
    assert merged.vibrato_hz == 4.8
    assert merged.vibrato_depth == 0.05


def test_merge_post_no_override_returns_preset() -> None:
    preset = {"lowpass_hz": 6000, "gain_db": -2.0}
    merged = _merge_post(preset, None)
    assert merged.lowpass_hz == 6000
    assert merged.gain_db == -2.0
    assert merged.vibrato_hz is None


def test_resolve_library_absolute_path_kept(tmp_path: Path) -> None:
    sf = tmp_path / "x.sfz"
    sf.write_text("// stub\n")
    assert _resolve_library(str(sf)) == sf


def test_resolve_library_relative_resolves_against_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "jg_instruments_root", tmp_path)
    out = _resolve_library("foo/bar.sfz")
    assert out == (tmp_path / "foo/bar.sfz").resolve()


def test_resolve_library_none_returns_none() -> None:
    assert _resolve_library(None) is None
    assert _resolve_library("") is None


def test_load_presets_returns_default_shape_when_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "jg_instruments_file", tmp_path / "nope.yaml")
    p = _load_presets()
    assert p["presets"] == {}


def test_load_presets_reads_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "i.yaml"
    f.write_text(yaml.safe_dump({
        "default": "x",
        "presets": {"x": {"engine": "sfizz", "library": "x.sfz"}},
    }))
    monkeypatch.setattr(get_settings(), "jg_instruments_file", f)
    p = _load_presets()
    assert p["default"] == "x"
    assert p["presets"]["x"]["engine"] == "sfizz"
