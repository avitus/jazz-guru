"""Unit tests for blocks/jazz_guru/handler.py — the Blocks Network adapter.

Covers the artifact I/O fix:
  1. JSON-only chat input still returns a JSON envelope (regression).
  2. Inline binary parts (audio_ref / file_ref) land in `in/<name>` and
     the message is augmented with an "Attached files" hint.
  3. Multi-part input (text + WAV) is decoded correctly.
  4. The artifact delta skips pre-existing workspace files and only
     emits files written or modified during this turn.
  5. `render_midi` with a lone MIDI attachment auto-wires `midi_path`.

The dispatchers are stubbed via `_DISPATCH` monkeypatching so we never
hit Anthropic — but the DB is real (per CLAUDE.md, integration tests
hit a real database).
"""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from blocks_network import StartTaskMessage
from blocks_network.types import ArtifactRef, RequestPart

from jazz_guru.config import get_settings

# --------------------------------------------------------------------------
# Module loading — the handler lives outside the `jazz_guru` package, so we
# load it by path the first time and cache the module on sys.modules.
# --------------------------------------------------------------------------

_HANDLER_PATH = Path(__file__).resolve().parents[2] / "blocks" / "jazz_guru" / "handler.py"


def _load_handler() -> Any:
    if "blocks_jazz_guru_handler" in sys.modules:
        return sys.modules["blocks_jazz_guru_handler"]
    spec = importlib.util.spec_from_file_location(
        "blocks_jazz_guru_handler", _HANDLER_PATH
    )
    assert spec and spec.loader, f"could not load spec for {_HANDLER_PATH}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["blocks_jazz_guru_handler"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def handler_mod() -> Any:
    return _load_handler()


@pytest.fixture
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(get_settings(), "jg_workspace_dir", tmp_path)
    return tmp_path


# --------------------------------------------------------------------------
# Part builders
# --------------------------------------------------------------------------


def _text_part(payload: dict | str) -> RequestPart:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return RequestPart(part_id="request", text=raw)


def _inline_artifact_part(
    data: bytes, *, file_name: str, mime_type: str, part_id: str = "audio"
) -> RequestPart:
    ref = ArtifactRef(
        kind="inline",
        mime_type=mime_type,
        size=len(data),
        data=base64.b64encode(data).decode("ascii"),
        file_name=file_name,
    )
    return RequestPart(part_id=part_id, content_type=mime_type, artifact_ref=ref)


def _start_task(*parts: RequestPart) -> StartTaskMessage:
    return StartTaskMessage(
        type="StartTask",
        task_id=str(uuid.uuid4()),
        agent_name="jazz_guru",
        owner_id="test",
        request_parts=list(parts),
    )


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def test_safe_upload_name_strips_path(handler_mod: Any) -> None:
    assert handler_mod._safe_upload_name("../../etc/passwd") == "passwd"
    assert handler_mod._safe_upload_name(".hidden.wav") == "hidden.wav"
    assert handler_mod._safe_upload_name(None) == "upload.bin"
    assert handler_mod._safe_upload_name("") == "upload.bin"
    assert handler_mod._safe_upload_name("solo.mid") == "solo.mid"


def test_parse_text_parts_picks_skill_envelope(handler_mod: Any) -> None:
    task = _start_task(_text_part({"skill": "chat", "message": "hi"}))
    payload, fragments = handler_mod._parse_text_parts(task)
    assert payload == {"skill": "chat", "message": "hi"}
    assert fragments == []


def test_parse_text_parts_loose_text_becomes_fragments(handler_mod: Any) -> None:
    task = _start_task(_text_part("just a prose message"))
    payload, fragments = handler_mod._parse_text_parts(task)
    assert payload is None
    assert fragments == ["just a prose message"]


def test_parse_text_parts_invalid_json_falls_back_to_text(handler_mod: Any) -> None:
    task = _start_task(_text_part("{not actually json"))
    payload, fragments = handler_mod._parse_text_parts(task)
    assert payload is None
    assert fragments == ["{not actually json"]


def test_augment_payload_chat_appends_hint(handler_mod: Any) -> None:
    payload = {"skill": "chat", "message": "transcribe this"}
    attachments = [
        {"path": "in/clip.wav", "name": "clip.wav", "mime": "audio/wav", "size": 4096},
    ]
    handler_mod._augment_payload_with_attachments(payload, attachments)
    assert "in/clip.wav" in payload["message"]
    assert "audio/wav" in payload["message"]
    assert "transcribe this" in payload["message"]


def test_augment_payload_render_midi_auto_wires_single_midi(handler_mod: Any) -> None:
    payload = {"skill": "render_midi", "instrument": "salamander-piano"}
    attachments = [
        {"path": "in/solo.mid", "name": "solo.mid", "mime": "audio/midi", "size": 800},
    ]
    handler_mod._augment_payload_with_attachments(payload, attachments)
    assert payload["midi_path"] == "in/solo.mid"


def test_augment_payload_render_midi_rejects_ambiguous(handler_mod: Any) -> None:
    payload = {"skill": "render_midi"}
    attachments = [
        {"path": "in/a.mid", "name": "a.mid", "mime": "audio/midi", "size": 100},
        {"path": "in/b.mid", "name": "b.mid", "mime": "audio/midi", "size": 100},
    ]
    with pytest.raises(ValueError, match="multiple MIDI"):
        handler_mod._augment_payload_with_attachments(payload, attachments)


# --------------------------------------------------------------------------
# Attachment persistence
# --------------------------------------------------------------------------


def test_persist_attachments_writes_inline_bytes(
    handler_mod: Any, tmp_path: Path
) -> None:
    payload_bytes = b"RIFF\x00\x00\x00\x00WAVEfmt "  # tiny WAV header
    part = _inline_artifact_part(payload_bytes, file_name="clip.wav", mime_type="audio/wav")
    task = _start_task(part)
    in_dir = tmp_path / "in"
    attachments = handler_mod._persist_attachments(task, ctx=None, in_dir=in_dir)
    assert len(attachments) == 1
    a = attachments[0]
    assert a["path"] == "in/clip.wav"
    assert a["mime"] == "audio/wav"
    assert a["size"] == len(payload_bytes)
    written = (in_dir / "clip.wav").read_bytes()
    assert written == payload_bytes


def test_persist_attachments_sanitizes_traversal(
    handler_mod: Any, tmp_path: Path
) -> None:
    # An attacker passes "../escape.wav" — sanitizer strips to "escape.wav".
    part = _inline_artifact_part(b"abc", file_name="../escape.wav", mime_type="audio/wav")
    task = _start_task(part)
    in_dir = tmp_path / "in"
    attachments = handler_mod._persist_attachments(task, ctx=None, in_dir=in_dir)
    assert attachments[0]["path"] == "in/escape.wav"
    # And no file was created above the in/ dir:
    assert not (tmp_path / "escape.wav").exists()


# --------------------------------------------------------------------------
# Artifact delta
# --------------------------------------------------------------------------


def test_collect_new_artifacts_returns_only_delta(
    handler_mod: Any, isolated_workspace: Path
) -> None:
    sid = uuid.uuid4()
    sess_dir = isolated_workspace / "sessions" / str(sid)
    sess_dir.mkdir(parents=True)

    # Pre-existing file: must NOT come back.
    (sess_dir / "old.mid").write_bytes(b"old midi")
    before = handler_mod._snapshot_artifacts(sid)

    # Two new files this turn:
    (sess_dir / "solo.mid").write_bytes(b"MThd\x00\x00\x00\x06new midi")
    (sess_dir / "solo.wav").write_bytes(b"RIFFWAVE")

    new = handler_mod._collect_new_artifacts(sid, before)
    paths = sorted(e["_path"] for e in new)
    assert paths == ["solo.mid", "solo.wav"]
    midi_entry = next(e for e in new if e["_path"] == "solo.mid")
    assert midi_entry["mimeType"] == "audio/midi"
    assert midi_entry["fileName"] == "solo.mid"
    assert isinstance(midi_entry["data"], bytes)


def test_collect_new_artifacts_skips_in_dir(
    handler_mod: Any, isolated_workspace: Path
) -> None:
    """Incoming attachments (in/foo.wav) must not be re-emitted as outputs."""
    sid = uuid.uuid4()
    sess_dir = isolated_workspace / "sessions" / str(sid)
    (sess_dir / "in").mkdir(parents=True)
    before = handler_mod._snapshot_artifacts(sid)

    (sess_dir / "in" / "input.wav").write_bytes(b"caller bytes")
    (sess_dir / "output.mid").write_bytes(b"agent bytes")

    new = handler_mod._collect_new_artifacts(sid, before)
    paths = [e["_path"] for e in new]
    assert paths == ["output.mid"]


# --------------------------------------------------------------------------
# Full-stack handler with stubbed dispatcher
# --------------------------------------------------------------------------


@pytest.fixture
def stub_chat_dispatcher(handler_mod: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace _DISPATCH['chat'] with a fake that writes a known file
    into the session workspace and records the payload it saw.
    """
    captured: dict[str, Any] = {}

    async def fake_chat(payload: dict[str, Any]) -> dict[str, Any]:
        captured["payload"] = dict(payload)
        sid = payload["session_id"]
        sess_dir = (
            get_settings().jg_workspace_dir / "sessions" / sid
        ).resolve()
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "solo.mid").write_bytes(b"MThd\x00\x00\x00\x06stub midi")
        (sess_dir / "solo.wav").write_bytes(b"RIFFstub wav bytes")
        return {
            "skill": "chat",
            "session_id": sid,
            "text": "stubbed response",
            "tool_calls": 0,
            "usage": {"input": 0, "output": 0, "usd": 0.0},
        }

    monkeypatch.setitem(handler_mod._DISPATCH, "chat", fake_chat)
    return captured


def test_handler_chat_returns_file_artifacts(
    handler_mod: Any,
    isolated_workspace: Path,
    stub_chat_dispatcher: dict[str, Any],
) -> None:
    """Regression: the JSON envelope is artifact[0]; agent-produced files
    follow as separate Blocks artifacts with bytes + MIME + fileName.
    """
    sid = str(uuid.uuid4())
    task = _start_task(_text_part({"skill": "chat", "session_id": sid, "message": "go"}))
    result = handler_mod.handler(task, ctx=None)

    artifacts = result["artifacts"]
    # JSON envelope first, then the two files we wrote.
    assert artifacts[0]["mimeType"] == "application/json"
    envelope = json.loads(artifacts[0]["data"])
    assert envelope["session_id"] == sid
    assert envelope["text"] == "stubbed response"
    assert sorted(envelope["artifacts"]) == ["solo.mid", "solo.wav"]

    by_name = {a.get("fileName"): a for a in artifacts[1:]}
    assert "solo.mid" in by_name and "solo.wav" in by_name
    assert by_name["solo.mid"]["mimeType"] == "audio/midi"
    assert by_name["solo.mid"]["data"] == b"MThd\x00\x00\x00\x06stub midi"
    assert by_name["solo.wav"]["mimeType"] == "audio/wav"
    # Critical: bytes, not str — the SDK base64-encodes inline content
    # for us; passing a str would UTF-8-encode it instead.
    assert isinstance(by_name["solo.mid"]["data"], bytes)


def test_handler_decodes_inline_binary_input(
    handler_mod: Any,
    isolated_workspace: Path,
    stub_chat_dispatcher: dict[str, Any],
) -> None:
    """Inline audio artifact is decoded to in/<name>, the chat message is
    augmented with the attachment hint, and the JSON envelope reports
    `attachments`.
    """
    sid = str(uuid.uuid4())
    audio_bytes = b"RIFF" + b"\x00" * 60 + b"WAVE_payload_xyz"
    task = _start_task(
        _text_part({"skill": "chat", "session_id": sid, "message": "transcribe please"}),
        _inline_artifact_part(audio_bytes, file_name="clip.wav", mime_type="audio/wav"),
    )
    result = handler_mod.handler(task, ctx=None)

    sess_dir = isolated_workspace / "sessions" / sid
    assert (sess_dir / "in" / "clip.wav").read_bytes() == audio_bytes

    payload = stub_chat_dispatcher["payload"]
    assert "in/clip.wav" in payload["message"]
    assert "transcribe please" in payload["message"]

    envelope = json.loads(result["artifacts"][0]["data"])
    assert envelope["attachments"] == ["in/clip.wav"]


def test_handler_render_midi_auto_wires_attachment(
    handler_mod: Any,
    isolated_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A MIDI attachment with no explicit midi_path auto-wires to
    `in/<name>` and is passed to the render_midi dispatcher.
    """
    captured: dict[str, Any] = {}

    async def fake_render(payload: dict[str, Any]) -> dict[str, Any]:
        captured["payload"] = dict(payload)
        sid = payload["session_id"]
        sess_dir = (get_settings().jg_workspace_dir / "sessions" / sid).resolve()
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "out.wav").write_bytes(b"rendered_audio")
        return {"skill": "render_midi", "session_id": sid, "result": {"path": "out.wav"}}

    monkeypatch.setitem(handler_mod._DISPATCH, "render_midi", fake_render)

    sid = str(uuid.uuid4())
    midi_bytes = b"MThd\x00\x00\x00\x06\x00\x01\x00\x01\x01\xe0MTrk"
    task = _start_task(
        _text_part({"skill": "render_midi", "session_id": sid, "instrument": "salamander-piano"}),
        _inline_artifact_part(midi_bytes, file_name="solo.mid", mime_type="audio/midi"),
    )
    result = handler_mod.handler(task, ctx=None)

    payload = captured["payload"]
    assert payload["midi_path"] == "in/solo.mid"
    assert payload["instrument"] == "salamander-piano"

    by_name = {a.get("fileName"): a for a in result["artifacts"][1:]}
    assert by_name["out.wav"]["data"] == b"rendered_audio"
    assert by_name["out.wav"]["mimeType"] == "audio/wav"
