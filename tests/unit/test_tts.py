from __future__ import annotations

from pathlib import Path

import pytest

from jazz_guru.actions import ToolContext, register_all, reset_tool_context, set_tool_context
from jazz_guru.config import get_settings


@pytest.mark.asyncio
async def test_tts_stub_writes_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "jg_workspace_dir", tmp_path)
    monkeypatch.setattr(get_settings(), "feature_tts", 0)
    r = register_all()
    tok = set_tool_context(ToolContext(session_id="ttx"))
    try:
        out = await r.invoke("tts", {"text": "hello there", "out_path": "out/speech.wav"})
        p = Path(out["path"])  # stub writes .txt
        assert p.exists()
        assert p.suffix == ".txt"
        assert out["engine"] == "stub"
    finally:
        reset_tool_context(tok)
