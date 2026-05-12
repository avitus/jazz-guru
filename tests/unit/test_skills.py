from __future__ import annotations

from pathlib import Path

import pytest

from jazz_guru.actions.registry import register_all, registry
from jazz_guru.config import GoalConfig, get_settings
from jazz_guru.context import BuildInputs, ContextBuilder
from jazz_guru.skills import (
    Skill,
    SkillsError,
    delete_skill,
    is_skill_active,
    list_skills_metadata,
    load_all_skills,
    load_skill,
    parse_skill_md,
    render_skill_md,
    skills_metadata_block,
    skills_root,
    write_skill,
)
from jazz_guru.skills.storage import (
    SkillFrontmatter,
    patch_skill_body,
    read_skill_file,
    remove_skill_file,
    write_skill_file,
)


@pytest.fixture
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(get_settings(), "jg_workspace_dir", tmp_path)
    register_all()
    return tmp_path


# ---------- parse / render -------------------------------------------------


def test_parse_skill_md_minimal() -> None:
    md = """---
name: four_way_close
description: Sax voicing in 4-part close
category: voicing
---
# four-way-close

Body content here.
"""
    fm, body = parse_skill_md(md)
    assert fm.name == "four_way_close"
    assert fm.category == "voicing"
    assert "Body content" in body


def test_parse_skill_md_rejects_missing_frontmatter() -> None:
    with pytest.raises(SkillsError):
        parse_skill_md("just a body, no frontmatter")


def test_parse_skill_md_rejects_missing_name() -> None:
    with pytest.raises(SkillsError):
        parse_skill_md("---\ncategory: x\n---\nbody")


def test_parse_skill_md_rejects_invalid_name() -> None:
    with pytest.raises(SkillsError):
        parse_skill_md("---\nname: WithCaps\ncategory: x\n---\nbody")


def test_parse_skill_md_rejects_invalid_category() -> None:
    with pytest.raises(SkillsError):
        parse_skill_md("---\nname: ok\ncategory: With-Dashes\n---\nbody")


def test_render_round_trip() -> None:
    fm = SkillFrontmatter(
        name="alpha",
        description="d",
        category="voicing",
        tags=["v", "harmony"],
        requires_tools=["render_midi"],
    )
    md = render_skill_md(fm, "# Title\nBody\n")
    fm2, body = parse_skill_md(md)
    assert fm2.name == fm.name
    assert fm2.tags == fm.tags
    assert fm2.requires_tools == fm.requires_tools
    assert body.strip() == "# Title\nBody"


# ---------- write / load --------------------------------------------------


def test_write_and_load_skill(isolated_workspace: Path) -> None:
    skill = write_skill(
        "voicing",
        "four_way_close",
        description="Sax 4-part close voicing",
        body="Step 1: ...\nStep 2: ...",
        tags=["sax", "voicing"],
    )
    assert isinstance(skill, Skill)
    assert skill.path.exists()
    loaded = load_skill(skill.path)
    assert loaded.name == "four_way_close"
    assert loaded.frontmatter.tags == ["sax", "voicing"]


def test_write_rejects_invalid_name(isolated_workspace: Path) -> None:
    with pytest.raises(SkillsError):
        write_skill("voicing", "Bad-Name", description="x", body="y")


def test_write_rejects_invalid_category(isolated_workspace: Path) -> None:
    with pytest.raises(SkillsError):
        write_skill("With-Dashes", "ok", description="x", body="y")


def test_write_refuses_overwrite_when_disabled(isolated_workspace: Path) -> None:
    write_skill("voicing", "alpha_one", description="d", body="b")
    with pytest.raises(SkillsError):
        write_skill("voicing", "alpha_one", description="d2", body="b2", overwrite=False)


def test_load_all_skills_walks_tree(isolated_workspace: Path) -> None:
    write_skill("voicing", "alpha", description="da", body="ba")
    write_skill("rhythm", "beta", description="db", body="bb")
    write_skill("voicing", "gamma", description="dc", body="bc")
    skills = load_all_skills()
    names = sorted(s.name for s in skills)
    assert names == ["alpha", "beta", "gamma"]


# ---------- metadata + activation -----------------------------------------


def test_list_skills_metadata_filters_by_allowed_tools(isolated_workspace: Path) -> None:
    write_skill(
        "voicing",
        "needs_render",
        description="d",
        body="b",
        requires_tools=["render_midi"],
    )
    write_skill(
        "voicing",
        "fallback_no_web",
        description="d",
        body="b",
        fallback_when_tools=["web_search"],
    )
    write_skill("voicing", "always_on", description="d", body="b")

    # No render_midi, no web_search: requires_tools blocks needs_render,
    # fallback_when_tools allows fallback_no_web, always_on always shows.
    meta = list_skills_metadata(allowed_tools=set())
    names = {m["name"] for m in meta}
    assert names == {"fallback_no_web", "always_on"}

    # With render_midi: needs_render allowed, fallback_no_web still allowed.
    meta2 = list_skills_metadata(allowed_tools={"render_midi"})
    assert {m["name"] for m in meta2} == {"needs_render", "fallback_no_web", "always_on"}

    # With web_search: fallback_no_web hidden.
    meta3 = list_skills_metadata(allowed_tools={"web_search"})
    assert {m["name"] for m in meta3} == {"always_on"}


def test_is_skill_active_helpers(isolated_workspace: Path) -> None:
    skill = write_skill(
        "voicing",
        "alpha",
        description="d",
        body="b",
        requires_tools=["xx"],
        fallback_when_tools=["yy"],
    )
    assert is_skill_active(skill, {"xx"}) is True
    assert is_skill_active(skill, set()) is False  # missing required xx
    assert is_skill_active(skill, {"xx", "yy"}) is False  # forbidden yy present


def test_skills_metadata_block_renders_compactly(isolated_workspace: Path) -> None:
    write_skill("voicing", "alpha", description="a-desc", body="x", tags=["v"])
    block = skills_metadata_block()
    assert "Skills available" in block
    assert "voicing/alpha" in block
    assert "a-desc" in block


def test_skills_metadata_block_empty_when_no_skills(isolated_workspace: Path) -> None:
    assert skills_metadata_block() == ""


# ---------- patch / delete / files -----------------------------------------


def test_patch_skill_body(isolated_workspace: Path) -> None:
    write_skill("voicing", "alpha", description="d", body="hello world")
    patched = patch_skill_body("voicing", "alpha", "world", "everyone")
    assert "hello everyone" in patched.body


def test_patch_skill_rejects_missing_find(isolated_workspace: Path) -> None:
    write_skill("voicing", "alpha", description="d", body="hello world")
    with pytest.raises(SkillsError):
        patch_skill_body("voicing", "alpha", "absent string", "x")


def test_patch_skill_rejects_ambiguous(isolated_workspace: Path) -> None:
    write_skill("voicing", "alpha", description="d", body="a a a")
    with pytest.raises(SkillsError):
        patch_skill_body("voicing", "alpha", "a", "b")


def test_delete_skill(isolated_workspace: Path) -> None:
    write_skill("voicing", "alpha", description="d", body="b")
    assert delete_skill("voicing", "alpha") is True
    assert delete_skill("voicing", "alpha") is False  # already gone
    assert load_all_skills() == []


def test_write_and_read_adjunct_file(isolated_workspace: Path) -> None:
    write_skill("voicing", "alpha", description="d", body="b")
    info = write_skill_file("voicing", "alpha", "references/example.txt", "hello")
    assert Path(info["path"]).exists()
    content = read_skill_file("voicing", "alpha", "references/example.txt")
    assert content == "hello"


def test_adjunct_file_rejects_unsupported_dir(isolated_workspace: Path) -> None:
    write_skill("voicing", "alpha", description="d", body="b")
    with pytest.raises(SkillsError):
        write_skill_file("voicing", "alpha", "secrets/x.txt", "y")


def test_adjunct_file_rejects_path_escape(isolated_workspace: Path) -> None:
    write_skill("voicing", "alpha", description="d", body="b")
    with pytest.raises(SkillsError):
        write_skill_file(
            "voicing", "alpha", "references/../../escape.txt", "y"
        )


def test_remove_adjunct_file(isolated_workspace: Path) -> None:
    write_skill("voicing", "alpha", description="d", body="b")
    write_skill_file("voicing", "alpha", "references/x.txt", "x")
    assert remove_skill_file("voicing", "alpha", "references/x.txt") is True
    assert remove_skill_file("voicing", "alpha", "references/x.txt") is False


# ---------- tools (skills_list / skill_view / skill_manage) ---------------


async def test_skills_list_returns_metadata(isolated_workspace: Path) -> None:
    write_skill("voicing", "alpha", description="d-a", body="b")
    write_skill("rhythm", "beta", description="d-b", body="b")
    out = await registry.invoke("skills_list", {})
    assert out["ok"] is True
    assert out["count"] == 2
    names = {s["name"] for s in out["skills"]}
    assert names == {"alpha", "beta"}


async def test_skills_list_filter_by_category(isolated_workspace: Path) -> None:
    write_skill("voicing", "alpha", description="d", body="b")
    write_skill("rhythm", "beta", description="d", body="b")
    out = await registry.invoke("skills_list", {"category": "voicing"})
    assert out["count"] == 1
    assert out["skills"][0]["name"] == "alpha"


async def test_skill_view_returns_body(isolated_workspace: Path) -> None:
    write_skill("voicing", "alpha", description="d", body="step 1\nstep 2")
    out = await registry.invoke("skill_view", {"name": "alpha"})
    assert out["ok"] is True
    assert "step 1" in out["body"]
    assert out["metadata"]["name"] == "alpha"


async def test_skill_view_with_adjunct(isolated_workspace: Path) -> None:
    write_skill("voicing", "alpha", description="d", body="b")
    write_skill_file("voicing", "alpha", "templates/example.md", "T")
    out = await registry.invoke(
        "skill_view", {"name": "alpha", "path": "templates/example.md"}
    )
    assert out["ok"] is True
    assert out["content"] == "T"


async def test_skill_view_unknown_skill(isolated_workspace: Path) -> None:
    out = await registry.invoke("skill_view", {"name": "nope"})
    assert out["ok"] is False
    assert "no skill named" in out["error"]


async def test_skill_manage_create_then_patch(isolated_workspace: Path) -> None:
    out = await registry.invoke(
        "skill_manage",
        {
            "action": "create",
            "name": "alpha",
            "category": "voicing",
            "description": "d",
            "body": "hello world",
        },
    )
    assert out["ok"] is True
    assert out["category"] == "voicing"
    # Patch
    out2 = await registry.invoke(
        "skill_manage",
        {
            "action": "patch",
            "name": "alpha",
            "find": "world",
            "replace": "everyone",
        },
    )
    assert out2["ok"] is True
    # Verify
    view = await registry.invoke("skill_view", {"name": "alpha"})
    assert "hello everyone" in view["body"]


async def test_skill_manage_create_requires_category(isolated_workspace: Path) -> None:
    out = await registry.invoke(
        "skill_manage",
        {
            "action": "create",
            "name": "alpha",
            "description": "d",
            "body": "b",
        },
    )
    assert out["ok"] is False
    assert "category" in out["error"]


async def test_skill_manage_delete(isolated_workspace: Path) -> None:
    await registry.invoke(
        "skill_manage",
        {"action": "create", "name": "alpha", "category": "voicing", "description": "d", "body": "b"},
    )
    out = await registry.invoke("skill_manage", {"action": "delete", "name": "alpha"})
    assert out["ok"] is True
    listed = await registry.invoke("skills_list", {})
    assert listed["count"] == 0


async def test_skill_manage_write_file_then_remove(isolated_workspace: Path) -> None:
    await registry.invoke(
        "skill_manage",
        {"action": "create", "name": "alpha", "category": "voicing", "description": "d", "body": "b"},
    )
    wf = await registry.invoke(
        "skill_manage",
        {
            "action": "write_file",
            "name": "alpha",
            "relpath": "references/x.txt",
            "content": "hi",
        },
    )
    assert wf["ok"] is True
    rm = await registry.invoke(
        "skill_manage",
        {"action": "remove_file", "name": "alpha", "relpath": "references/x.txt"},
    )
    assert rm["ok"] is True


# ---------- ContextBuilder integration ------------------------------------


def test_context_builder_injects_skills_metadata(isolated_workspace: Path) -> None:
    write_skill("voicing", "alpha", description="four-way close", body="b")
    g = GoalConfig(prose="north star")
    p = ContextBuilder(goal=g).build(BuildInputs(user_message="hi"))
    assert "Skills available" in p.system
    assert "voicing/alpha" in p.system


def test_context_builder_filters_skills_by_allowed_tools(
    isolated_workspace: Path,
) -> None:
    write_skill(
        "voicing",
        "needs_render",
        description="d",
        body="b",
        requires_tools=["render_midi"],
    )
    g = GoalConfig(prose="north star")
    # Without render_midi in the allow list, the skill should NOT appear.
    p = ContextBuilder(goal=g).build(
        BuildInputs(user_message="hi", allowed_tools=set())
    )
    assert "needs_render" not in p.system
    # With render_midi, it appears.
    p2 = ContextBuilder(goal=g).build(
        BuildInputs(user_message="hi", allowed_tools={"render_midi"})
    )
    assert "needs_render" in p2.system


# ---------- reflexion contract sanity -------------------------------------


def test_reflection_result_has_skill_fields() -> None:
    import uuid as _uuid

    from jazz_guru.distillation.reflexion import ReflectionResult

    r = ReflectionResult(session_id=_uuid.uuid4(), score=0.0, critique="", revised_plan="")
    assert r.skill_writes == []
    assert r.skills_applied == 0


# ---------- sandbox boundary ----------------------------------------------


def test_skill_dir_cannot_escape_workspace(
    isolated_workspace: Path,
) -> None:
    # Use a path-like name to try to escape; validate_name rejects it.
    with pytest.raises(SkillsError):
        write_skill("voicing", "../escape", description="d", body="b")


def test_skills_root_is_under_workspace(isolated_workspace: Path) -> None:
    root = skills_root()
    assert root.resolve().is_relative_to(isolated_workspace.resolve())
