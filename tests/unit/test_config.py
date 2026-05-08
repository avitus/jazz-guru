from __future__ import annotations

from jazz_guru.config import (
    GoalConfig,
    Objective,
    Policy,
    ToolPolicy,
    get_settings,
    load_goal,
    load_policy,
)


def test_settings_defaults_load() -> None:
    s = get_settings()
    assert s.anthropic_model
    assert s.embedding_dim > 0


def test_load_goal_renders_system_block() -> None:
    s = get_settings()
    g = load_goal(s)
    block = g.render_system_block()
    assert "Objectives" in block or g.prose  # at least one section present


def test_load_policy_has_tool_entries() -> None:
    s = get_settings()
    p = load_policy(s)
    assert "fs_read" in p.tools
    assert p.for_tool("fs_read").mode == "allow"
    assert p.for_tool("nonexistent").mode == p.default


def test_goal_render_with_objectives() -> None:
    g = GoalConfig(
        prose="hello",
        objectives=[Objective(id="o1", text="do x", weight=1.0)],
        constraints=["c1"],
        success_criteria=["s1"],
        style={"voice": "warm"},
    )
    out = g.render_system_block()
    assert "Objectives" in out
    assert "Constraints" in out
    assert "Success criteria" in out
    assert "Style" in out


def test_policy_for_tool_default() -> None:
    p = Policy(default="confirm", tools={"x": ToolPolicy(mode="allow")})
    assert p.for_tool("x").mode == "allow"
    assert p.for_tool("missing").mode == "confirm"
