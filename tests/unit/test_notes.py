from __future__ import annotations

from pathlib import Path

import pytest

from jazz_guru.config import GoalConfig, Objective, get_settings
from jazz_guru.context import BuildInputs, ContextBuilder
from jazz_guru.notes import (
    AGENT_NOTES_CAP,
    USER_NOTES_CAP,
    NotesError,
    normalize_key,
    patch_notes,
    read_notes,
    render_notes_block,
    write_notes,
)


@pytest.fixture
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect workspace to a tmp dir so notes writes don't pollute the repo."""
    monkeypatch.setattr(get_settings(), "jg_workspace_dir", tmp_path)
    return tmp_path


def test_read_empty_when_no_files(isolated_workspace: Path) -> None:
    notes = read_notes()
    assert notes == {"AGENT_NOTES": "", "USER": ""}


def test_write_then_read_round_trip(isolated_workspace: Path) -> None:
    info = write_notes("AGENT_NOTES", "default tempo is 120 BPM")
    assert info["file"] == "AGENT_NOTES"
    notes = read_notes()
    assert "120 BPM" in notes["AGENT_NOTES"]
    assert notes["USER"] == ""


def test_normalize_key_accepts_aliases() -> None:
    assert normalize_key("AGENT_NOTES") == "AGENT_NOTES"
    assert normalize_key("agent_notes") == "AGENT_NOTES"
    assert normalize_key("AGENT_NOTES.md") == "AGENT_NOTES"
    assert normalize_key("USER") == "USER"
    assert normalize_key("user.md") == "USER"


def test_normalize_key_rejects_unknown() -> None:
    with pytest.raises(NotesError):
        normalize_key("PROJECT")


def test_write_rejects_over_cap(isolated_workspace: Path) -> None:
    with pytest.raises(NotesError) as exc:
        write_notes("USER", "x" * (USER_NOTES_CAP + 1))
    assert "exceeds cap" in str(exc.value)


def test_patch_happy_path(isolated_workspace: Path) -> None:
    write_notes("AGENT_NOTES", "tempo 120, swing 0.62, mood: bright")
    info = patch_notes("AGENT_NOTES", "swing 0.62", "swing 0.66")
    assert info["replaced"] == 1
    assert "swing 0.66" in read_notes()["AGENT_NOTES"]


def test_patch_rejects_missing_find(isolated_workspace: Path) -> None:
    write_notes("AGENT_NOTES", "tempo 120")
    with pytest.raises(NotesError) as exc:
        patch_notes("AGENT_NOTES", "swing 0.62", "swing 0.66")
    assert "not present" in str(exc.value)


def test_patch_rejects_ambiguous_find(isolated_workspace: Path) -> None:
    write_notes("AGENT_NOTES", "abc abc")
    with pytest.raises(NotesError) as exc:
        patch_notes("AGENT_NOTES", "abc", "xyz")
    assert "occurs 2 times" in str(exc.value)


def test_patch_rejects_when_growth_exceeds_cap(isolated_workspace: Path) -> None:
    # Fill USER almost to cap, then try to grow it past.
    seed = "abc" + "x" * (USER_NOTES_CAP - 4)
    write_notes("USER", seed)
    with pytest.raises(NotesError) as exc:
        patch_notes("USER", "abc", "abc" + "y" * 100)
    assert "exceeds cap" in str(exc.value)


def test_patch_requires_existing_file(isolated_workspace: Path) -> None:
    with pytest.raises(NotesError) as exc:
        patch_notes("USER", "x", "y")
    assert "does not exist" in str(exc.value)


def test_patch_rejects_empty_find(isolated_workspace: Path) -> None:
    write_notes("USER", "anything")
    with pytest.raises(NotesError):
        patch_notes("USER", "", "y")


def test_render_notes_block_empty_when_both_empty(isolated_workspace: Path) -> None:
    assert render_notes_block() == ""


def test_render_notes_block_includes_filled_files(isolated_workspace: Path) -> None:
    write_notes("AGENT_NOTES", "fact about the band")
    block = render_notes_block()
    assert "Durable notes" in block
    assert "AGENT_NOTES.md" in block
    assert "fact about the band" in block
    # USER section omitted when empty
    assert "USER.md" not in block


def test_context_builder_injects_notes_block(isolated_workspace: Path) -> None:
    write_notes("USER", "operator prefers Real Book changes")
    g = GoalConfig(
        prose="north star",
        objectives=[Objective(id="o1", text="ship art", weight=1.0)],
    )
    p = ContextBuilder(goal=g).build(
        BuildInputs(
            user_message="play me a blues",
            history=[],
            state_doc="state body",
            retrieved_memory=[],
            playbook_excerpts=[],
        )
    )
    assert "Durable notes" in p.system
    assert "Real Book" in p.system
    # Notes must appear BEFORE the externalized state section so they sit in
    # the cache-friendly prefix.
    assert p.system.index("Durable notes") < p.system.index("Externalized state")


def test_context_builder_omits_notes_block_when_empty(isolated_workspace: Path) -> None:
    g = GoalConfig(prose="north star")
    p = ContextBuilder(goal=g).build(BuildInputs(user_message="hi"))
    assert "Durable notes" not in p.system


def test_caps_are_distinct_and_sane() -> None:
    assert AGENT_NOTES_CAP > USER_NOTES_CAP > 0


def test_normalize_key_strips_md_case_insensitively() -> None:
    assert normalize_key("  agent_notes.MD  ") == "AGENT_NOTES"


def test_reflection_result_carries_notes_patches() -> None:
    # Pure-dataclass check — ensures the field exists and defaults to []
    import uuid as _uuid

    from jazz_guru.distillation.reflexion import ReflectionResult

    r = ReflectionResult(
        session_id=_uuid.uuid4(),
        score=0.5,
        critique="ok",
        revised_plan="",
    )
    assert r.notes_patches == []
    assert r.notes_applied == 0
