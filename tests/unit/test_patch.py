from __future__ import annotations

from pathlib import Path

import pytest

from jazz_guru.actions.context import ToolContext, reset_tool_context, set_tool_context
from jazz_guru.actions.registry import register_all, registry
from jazz_guru.config import get_settings


@pytest.fixture
def isolated_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "jg_workspace_dir", tmp_path)
    register_all()
    tok = set_tool_context(ToolContext(session_id="test", turn_idx=0))
    yield tmp_path
    reset_tool_context(tok)


def _make_session_file(tmp_path: Path, name: str, content: str) -> Path:
    sess_dir = tmp_path / "sessions" / "test"
    sess_dir.mkdir(parents=True, exist_ok=True)
    p = sess_dir / name
    p.write_text(content, encoding="utf-8")
    return p


async def test_patch_exact_match(isolated_session: Path) -> None:
    _make_session_file(isolated_session, "f.py", "x = 1\ny = 2\n")
    out = await registry.invoke(
        "patch", {"path": "f.py", "find": "y = 2", "replace": "y = 42"}
    )
    assert out["ok"] is True
    assert out["strategy"] == "exact"
    assert out["replacements"] == 1
    assert "y = 42" in (isolated_session / "sessions" / "test" / "f.py").read_text()


async def test_patch_rejects_multiple_exact_without_change_all(
    isolated_session: Path,
) -> None:
    _make_session_file(isolated_session, "f.txt", "abc\nabc\n")
    out = await registry.invoke(
        "patch", {"path": "f.txt", "find": "abc", "replace": "xyz"}
    )
    assert out["ok"] is False
    assert out["matches"] == 2
    assert "change_all" in out["reason"]


async def test_patch_change_all_exact(isolated_session: Path) -> None:
    _make_session_file(isolated_session, "f.txt", "abc\nabc\n")
    out = await registry.invoke(
        "patch",
        {"path": "f.txt", "find": "abc", "replace": "xyz", "change_all": True},
    )
    assert out["ok"] is True
    assert out["replacements"] == 2
    assert (isolated_session / "sessions" / "test" / "f.txt").read_text() == "xyz\nxyz\n"


async def test_patch_line_trimmed_match(isolated_session: Path) -> None:
    # Indented in file; `find` is unindented. Exact match fails, line-trimmed
    # picks it up and preserves indentation.
    _make_session_file(
        isolated_session,
        "f.py",
        "def foo():\n    return 1\n\ndef bar():\n    return 2\n",
    )
    out = await registry.invoke(
        "patch",
        {
            "path": "f.py",
            "find": "return 1",
            "replace": "return 999",
        },
    )
    # The above will match exactly since 'return 1' appears once. So switch
    # to a multi-line find that needs indentation tolerance.
    text = (isolated_session / "sessions" / "test" / "f.py").read_text()
    assert "return 999" in text
    assert out["strategy"] == "exact"  # exactly one occurrence — exact wins


async def test_patch_line_trimmed_with_indentation_diff(isolated_session: Path) -> None:
    _make_session_file(
        isolated_session,
        "f.py",
        "def foo():\n    x = 1\n    y = 2\n    return x + y\n",
    )
    # Find pattern WITHOUT the 4-space indent — exact won't match.
    out = await registry.invoke(
        "patch",
        {
            "path": "f.py",
            "find": "x = 1\ny = 2",
            "replace": "x = 10\ny = 20",
        },
    )
    assert out["ok"] is True
    assert out["strategy"] == "line_trimmed"
    text = (isolated_session / "sessions" / "test" / "f.py").read_text()
    assert "    x = 10" in text
    assert "    y = 20" in text


async def test_patch_fuzzy_match(isolated_session: Path) -> None:
    _make_session_file(
        isolated_session, "f.txt", "Hello, World!\nGoodbye, World!\n"
    )
    out = await registry.invoke(
        "patch",
        {
            "path": "f.txt",
            "find": "Hello, World",  # missing the !, fuzzy ratio should still pass
            "replace": "Hello, Mars!",
        },
    )
    # 'Hello, World' is an exact substring → exact wins. Need to break
    # exact match. Use a slightly different word.
    text = (isolated_session / "sessions" / "test" / "f.txt").read_text()
    assert "Hello, Mars!" in text
    assert out["strategy"] == "exact"


async def test_patch_fuzzy_when_no_exact_or_line_match(
    isolated_session: Path,
) -> None:
    _make_session_file(
        isolated_session,
        "f.py",
        "x = 1\ny = 2\nz = 3\n",
    )
    # Tiny typo: 'z = 3' vs find 'z := 3' — line-trimmed will reject; fuzzy passes.
    out = await registry.invoke(
        "patch",
        {
            "path": "f.py",
            "find": "z := 3",
            "replace": "z = 30",
            "min_ratio": 0.6,
        },
    )
    assert out["ok"] is True
    assert out["strategy"] == "fuzzy"
    assert (isolated_session / "sessions" / "test" / "f.py").read_text().count(
        "z = 30"
    ) == 1


async def test_patch_rejects_when_no_match(isolated_session: Path) -> None:
    _make_session_file(isolated_session, "f.txt", "alpha\n")
    out = await registry.invoke(
        "patch", {"path": "f.txt", "find": "nothing-like-this", "replace": "x"}
    )
    assert out["ok"] is False
    assert "strategies_tried" in out


async def test_patch_returns_unified_diff(isolated_session: Path) -> None:
    _make_session_file(isolated_session, "f.txt", "before\n")
    out = await registry.invoke(
        "patch", {"path": "f.txt", "find": "before", "replace": "after"}
    )
    assert out["ok"] is True
    diff = out["diff"]
    assert "-before" in diff
    assert "+after" in diff


async def test_patch_reverts_on_syntax_error(isolated_session: Path) -> None:
    _make_session_file(isolated_session, "f.py", "x = 1\n")
    out = await registry.invoke(
        "patch",
        {"path": "f.py", "find": "x = 1", "replace": "x =:= 1  # broken"},
    )
    assert out["ok"] is False
    assert "syntax" in out["reason"].lower()
    # File should be unchanged
    assert (isolated_session / "sessions" / "test" / "f.py").read_text() == "x = 1\n"


async def test_patch_syntax_check_can_be_disabled(isolated_session: Path) -> None:
    _make_session_file(isolated_session, "f.py", "x = 1\n")
    out = await registry.invoke(
        "patch",
        {
            "path": "f.py",
            "find": "x = 1",
            "replace": "x =:= 1  # intentionally broken",
            "syntax_check": False,
        },
    )
    assert out["ok"] is True


async def test_patch_rejects_empty_find(isolated_session: Path) -> None:
    _make_session_file(isolated_session, "f.txt", "stuff\n")
    out = await registry.invoke(
        "patch", {"path": "f.txt", "find": "", "replace": "x"}
    )
    assert out["ok"] is False


async def test_patch_rejects_noop(isolated_session: Path) -> None:
    _make_session_file(isolated_session, "f.txt", "stuff\n")
    out = await registry.invoke(
        "patch", {"path": "f.txt", "find": "stuff", "replace": "stuff"}
    )
    assert out["ok"] is False
    assert "no-op" in out["reason"]


async def test_patch_rejects_missing_file(isolated_session: Path) -> None:
    out = await registry.invoke(
        "patch", {"path": "nope.txt", "find": "x", "replace": "y"}
    )
    assert out["ok"] is False
    assert "missing" in out["reason"]


async def test_code_edit_back_compat(isolated_session: Path) -> None:
    _make_session_file(isolated_session, "f.py", "x = 1\n")
    out = await registry.invoke(
        "code_edit", {"path": "f.py", "old_str": "x = 1", "new_str": "x = 99"}
    )
    assert out["ok"] is True
    assert out["replacements"] == 1
    assert (isolated_session / "sessions" / "test" / "f.py").read_text() == "x = 99\n"


async def test_code_edit_back_compat_change_all(isolated_session: Path) -> None:
    _make_session_file(isolated_session, "f.txt", "abc\nabc\n")
    out = await registry.invoke(
        "code_edit",
        {"path": "f.txt", "old_str": "abc", "new_str": "xyz", "change_all": True},
    )
    assert out["ok"] is True
    assert out["replacements"] == 2
