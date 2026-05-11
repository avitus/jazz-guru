from __future__ import annotations

from pathlib import Path

import pytest

from jazz_guru.actions import ToolContext, register_all, reset_tool_context, set_tool_context
from jazz_guru.config import get_settings


@pytest.fixture
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(get_settings(), "jg_workspace_dir", tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_midi_from_notes_writes_file(isolated_workspace: Path) -> None:
    r = register_all()
    tok = set_tool_context(ToolContext(session_id="t1"))
    try:
        notes = [
            {"pitch": 60, "start_beat": 0.0, "duration_beat": 0.5, "velocity": 96},
            {"pitch": 64, "start_beat": 0.5, "duration_beat": 0.5, "velocity": 96},
            {"pitch": 67, "start_beat": 1.0, "duration_beat": 0.5, "velocity": 96},
        ]
        out = await r.invoke("midi_from_notes", {"out_path": "out/test.mid", "notes": notes, "bpm": 120})
        p = Path(out["path"])
        assert p.exists() and p.suffix == ".mid"
        assert out["notes"] == 3
        info = await r.invoke("midi_info", {"path": "out/test.mid"})
        assert info["note_on_count"] == 3
    finally:
        reset_tool_context(tok)


@pytest.mark.asyncio
async def test_music_xml_from_tinynotation(isolated_workspace: Path) -> None:
    r = register_all()
    tok = set_tool_context(ToolContext(session_id="t2"))
    try:
        out = await r.invoke(
            "music_xml_from_tinynotation",
            {"out_path": "out/scale.xml", "tinynotation": "4/4 c4 d e f g a b c'1", "title": "scale"},
        )
        p = Path(out["path"])
        assert p.exists()
        info = await r.invoke("music_xml_info", {"path": "out/scale.xml"})
        assert info["measures"] >= 1
    finally:
        reset_tool_context(tok)
