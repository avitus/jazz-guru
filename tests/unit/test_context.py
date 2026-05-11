from __future__ import annotations

from jazz_guru.config import GoalConfig, Objective
from jazz_guru.context import BuildInputs, ContextBuilder


def test_build_includes_all_sections() -> None:
    g = GoalConfig(
        prose="north star",
        objectives=[Objective(id="o1", text="ship art", weight=1.0)],
        constraints=["c1"],
        success_criteria=["s1"],
    )
    cb = ContextBuilder(goal=g)
    p = cb.build(
        BuildInputs(
            user_message="play me a blues",
            history=[{"role": "user", "content": "earlier"}],
            state_doc="state body",
            retrieved_memory=["mem1", "mem2"],
            playbook_excerpts=["lesson 1"],
        )
    )
    assert "north star" in p.system
    assert "Objectives" in p.system
    assert "Externalized state" in p.system
    assert "Playbook" in p.system
    assert "Retrieved memory" in p.system
    assert p.messages[-1] == {"role": "user", "content": "play me a blues"}
    assert p.metadata["history_len"] == 1
    assert p.metadata["memory_items"] == 2
