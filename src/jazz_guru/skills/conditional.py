"""Conditional skill activation.

A skill can declare ``requires_tools`` and ``fallback_when_tools`` in its
frontmatter to express which toolsets it makes sense alongside. When the
agent's current allowed toolset doesn't satisfy the condition, the skill is
hidden from ``skills_list`` (and therefore not injected into the system
prompt). The agent can still ``skill_view`` it by name if it has a reason to.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jazz_guru.skills.storage import Skill


def is_skill_active(skill: Skill, allowed_tools: set[str]) -> bool:
    """Return True iff this skill is relevant for the current toolset.

    Semantics:
    - ``requires_tools``: every name listed must be present in ``allowed_tools``.
    - ``fallback_when_tools``: every name listed must be **absent** (the skill
      is a fallback for when those tools aren't available).
    - Empty lists are no-ops.
    """
    fm = skill.frontmatter
    if any(tool not in allowed_tools for tool in fm.requires_tools):
        return False
    return all(tool not in allowed_tools for tool in fm.fallback_when_tools)
