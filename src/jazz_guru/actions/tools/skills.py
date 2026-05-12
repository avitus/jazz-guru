"""Tools for the procedural-memory ``skills`` system.

Three tools, mirroring the Hermes three-level progressive disclosure:

* ``skills_list`` -- metadata only (cheap; agent uses this to discover).
* ``skill_view``  -- full SKILL.md body, or a specific adjunct file.
* ``skill_manage`` -- create/patch/edit/delete + adjunct write/remove.

The agent gets a one-line summary of every active skill in its system prompt
automatically (see ``ContextBuilder``); these tools are how it expands and
maintains the library.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from jazz_guru.actions.registry import registry
from jazz_guru.skills import SkillsError, delete_skill, list_skills_metadata, load_all_skills
from jazz_guru.skills.storage import (
    patch_skill_body,
    read_skill_file,
    remove_skill_file,
    write_skill,
    write_skill_file,
)

# ---------- skills_list ---------------------------------------------------


class SkillsListInput(BaseModel):
    category: str | None = Field(
        default=None, description="Filter to a single category (e.g. 'voicing')."
    )


@registry.register(
    "skills_list",
    description=(
        "List metadata for skills in the library. Returns name, category, "
        "description, version, tags only -- not the body. Use `skill_view` to "
        "read a skill's full content. Skills with unsatisfied `requires_tools` "
        "or `fallback_when_tools` are already filtered out by the system "
        "prompt; this tool returns the same active set."
    ),
    input_model=SkillsListInput,
    tags=("memory",),
)
async def skills_list(category: str | None = None) -> dict[str, Any]:
    items = list_skills_metadata()
    if category:
        items = [it for it in items if it.get("category") == category]
    return {"ok": True, "count": len(items), "skills": items}


# ---------- skill_view ----------------------------------------------------


class SkillViewInput(BaseModel):
    name: str = Field(..., description="Skill name (snake_case).")
    category: str | None = Field(
        default=None,
        description=(
            "Optional category disambiguator. If omitted and multiple skills "
            "share the name, returns the first match (with all candidates listed)."
        ),
    )
    path: str | None = Field(
        default=None,
        description=(
            "Relative path to an adjunct file (under references/, templates/, "
            "scripts/, or assets/). If omitted, returns the SKILL.md content."
        ),
    )


def _find_skill(name: str, category: str | None):
    candidates = [s for s in load_all_skills() if s.name == name]
    if not candidates:
        return None, []
    if category is not None:
        candidates = [s for s in candidates if s.category == category]
        if not candidates:
            return None, []
    return candidates[0], candidates


@registry.register(
    "skill_view",
    description=(
        "Read a skill's full content. Without `path`, returns the SKILL.md "
        "body (with frontmatter metadata as a separate field). With `path`, "
        "returns the contents of an adjunct file (references/templates/scripts/"
        "assets). Use after `skills_list` shows you a useful entry."
    ),
    input_model=SkillViewInput,
    tags=("memory",),
)
async def skill_view(
    name: str, category: str | None = None, path: str | None = None
) -> dict[str, Any]:
    skill, candidates = _find_skill(name, category)
    if skill is None:
        return {"ok": False, "error": f"no skill named {name!r}"}
    multi = (
        [{"name": c.name, "category": c.category} for c in candidates]
        if len(candidates) > 1
        else None
    )
    if path is None:
        return {
            "ok": True,
            "name": skill.name,
            "category": skill.category,
            "metadata": skill.frontmatter.render(),
            "body": skill.body,
            "path": str(skill.path),
            "sha256": skill.sha256,
            "candidates": multi,
        }
    try:
        content = read_skill_file(skill.category, skill.name, path)
    except SkillsError as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": True,
        "name": skill.name,
        "category": skill.category,
        "file": path,
        "content": content,
        "candidates": multi,
    }


# ---------- skill_manage --------------------------------------------------


class SkillManageInput(BaseModel):
    action: str = Field(
        ...,
        description=(
            "One of: create, patch, edit, delete, write_file, remove_file. "
            "create/edit take a full body; patch is a single find-and-replace; "
            "delete removes the skill directory; write_file/remove_file manage "
            "adjunct files under references/templates/scripts/assets."
        ),
    )
    name: str = Field(..., description="Skill name (snake_case).")
    category: str | None = Field(
        default=None,
        description="Category. Required for 'create'; inferred for others if the skill exists.",
    )
    description: str | None = Field(
        default=None, description="One-line description (for create/edit)."
    )
    body: str | None = Field(default=None, description="Full SKILL.md body (for create/edit).")
    version: str = Field(default="1.0.0", description="Semver-ish version string.")
    tags: list[str] | None = None
    requires_tools: list[str] | None = None
    fallback_when_tools: list[str] | None = None
    find: str | None = Field(default=None, description="For 'patch': substring to replace.")
    replace: str | None = Field(default=None, description="For 'patch': replacement text.")
    relpath: str | None = Field(
        default=None, description="For write_file/remove_file: relative path inside the skill dir."
    )
    content: str | None = Field(default=None, description="For write_file: file contents.")


def _resolve_category(name: str, supplied: str | None) -> str | None:
    if supplied:
        return supplied
    _, candidates = _find_skill(name, None)
    if not candidates:
        return None
    if len(candidates) > 1:
        return None
    return candidates[0].category


@registry.register(
    "skill_manage",
    description=(
        "Create / patch / edit / delete a skill, or add / remove adjunct files. "
        "Use after running a task whose approach should be reusable. Prefer "
        "'patch' (surgical edit) over 'edit' (full rewrite) when possible. "
        "Skills go under workspace/skills/<category>/<name>/SKILL.md."
    ),
    input_model=SkillManageInput,
    tags=("memory", "meta"),
)
async def skill_manage(
    action: str,
    name: str,
    category: str | None = None,
    description: str | None = None,
    body: str | None = None,
    version: str = "1.0.0",
    tags: list[str] | None = None,
    requires_tools: list[str] | None = None,
    fallback_when_tools: list[str] | None = None,
    find: str | None = None,
    replace: str | None = None,
    relpath: str | None = None,
    content: str | None = None,
) -> dict[str, Any]:
    try:
        if action in ("create", "edit"):
            cat = category or _resolve_category(name, None)
            if not cat:
                return {"ok": False, "error": f"{action} requires 'category' for new skills"}
            if not description:
                return {"ok": False, "error": f"{action} requires 'description'"}
            if body is None:
                return {"ok": False, "error": f"{action} requires 'body'"}
            skill = write_skill(
                cat,
                name,
                description=description,
                body=body,
                version=version,
                tags=tags,
                requires_tools=requires_tools,
                fallback_when_tools=fallback_when_tools,
                overwrite=(action == "edit"),
            )
            return {
                "ok": True,
                "action": action,
                "name": skill.name,
                "category": skill.category,
                "path": str(skill.path),
                "sha256": skill.sha256,
            }
        if action == "patch":
            cat = category or _resolve_category(name, None)
            if not cat:
                return {"ok": False, "error": "patch could not infer category; supply one"}
            if find is None or replace is None:
                return {"ok": False, "error": "patch requires both 'find' and 'replace'"}
            skill = patch_skill_body(cat, name, find, replace)
            return {
                "ok": True,
                "action": "patch",
                "name": skill.name,
                "category": skill.category,
                "path": str(skill.path),
                "sha256": skill.sha256,
            }
        if action == "delete":
            cat = category or _resolve_category(name, None)
            if not cat:
                return {"ok": False, "error": "delete could not infer category; supply one"}
            ok = delete_skill(cat, name)
            return {"ok": ok, "action": "delete", "name": name, "category": cat}
        if action == "write_file":
            cat = category or _resolve_category(name, None)
            if not cat:
                return {"ok": False, "error": "write_file could not infer category; supply one"}
            if not relpath or content is None:
                return {"ok": False, "error": "write_file requires 'relpath' and 'content'"}
            info = write_skill_file(cat, name, relpath, content)
            return {"ok": True, "action": "write_file", "name": name, "category": cat, **info}
        if action == "remove_file":
            cat = category or _resolve_category(name, None)
            if not cat:
                return {"ok": False, "error": "remove_file could not infer category; supply one"}
            if not relpath:
                return {"ok": False, "error": "remove_file requires 'relpath'"}
            ok = remove_skill_file(cat, name, relpath)
            return {"ok": ok, "action": "remove_file", "name": name, "category": cat}
    except SkillsError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": False, "error": f"unknown action: {action!r}"}
