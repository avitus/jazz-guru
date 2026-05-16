"""Procedural memory: SKILL.md documents with YAML frontmatter.

Hermes-style three-level progressive disclosure: the agent gets a compact
metadata list in every system prompt and expands skills it cares about with
``skill_view``. Unlike the playbook (one-line transferable lessons), a skill
is a structured document with optional reference files, templates, scripts,
and assets.

Storage lives under ``workspace/skills/<category>/<name>/SKILL.md``. The
storage module enforces a workspace-rooted sandbox; nothing outside
``workspace/skills/`` can be read or written.
"""
from __future__ import annotations

from jazz_guru.skills.conditional import is_skill_active
from jazz_guru.skills.storage import (
    Skill,
    SkillFrontmatter,
    SkillsError,
    delete_skill,
    list_skills_metadata,
    load_all_skills,
    load_skill,
    parse_skill_md,
    render_skill_md,
    skills_metadata_block,
    skills_root,
    write_skill,
    write_skill_file,
)

__all__ = [
    "Skill",
    "SkillFrontmatter",
    "SkillsError",
    "delete_skill",
    "is_skill_active",
    "list_skills_metadata",
    "load_all_skills",
    "load_skill",
    "parse_skill_md",
    "render_skill_md",
    "skills_metadata_block",
    "skills_root",
    "write_skill",
    "write_skill_file",
]
