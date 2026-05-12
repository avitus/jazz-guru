from __future__ import annotations

from typing import Any

import pytest

from jazz_guru.actions.context import ToolContext, reset_tool_context, set_tool_context
from jazz_guru.actions.registry import register_all, registry
from jazz_guru.actions.tools import delegate as delegate_mod


@pytest.fixture
def stub_runner(monkeypatch: pytest.MonkeyPatch):
    """Replace the real subagent runner with a recording stub."""
    register_all()
    calls: list[dict[str, Any]] = []

    async def _fake_runner(task, *, goal_profile, extra_instructions):
        calls.append(
            {
                "task": task,
                "goal_profile": goal_profile,
                "extra_instructions": extra_instructions,
            }
        )
        return {
            "subsession_id": "sub-abc",
            "text": f"subagent says: {task}",
            "tool_calls": 3,
            "rounds": 2,
            "usage": {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.001},
            "errors": [],
        }

    monkeypatch.setattr(delegate_mod, "_runner", _fake_runner)
    tok = set_tool_context(ToolContext(session_id="parent", turn_idx=0))
    yield calls
    reset_tool_context(tok)


async def test_delegate_task_returns_subagent_summary(stub_runner) -> None:
    out = await registry.invoke(
        "delegate_task",
        {"task": "render and judge 4 voicings, return the best"},
    )
    assert out["ok"] is True
    assert out["parent_session_id"] == "parent"
    assert out["subsession_id"] == "sub-abc"
    assert "render and judge" in out["text"]
    assert out["tool_calls"] == 3
    assert out["usage"]["cost_usd"] == 0.001


async def test_delegate_task_threads_extra_instructions(stub_runner) -> None:
    await registry.invoke(
        "delegate_task",
        {
            "task": "summarize this",
            "extra_instructions": "Context: the user prefers terse answers.",
        },
    )
    assert stub_runner[0]["extra_instructions"] == (
        "Context: the user prefers terse answers."
    )
    assert stub_runner[0]["task"] == "summarize this"


async def test_delegate_task_honors_goal_profile(stub_runner) -> None:
    await registry.invoke(
        "delegate_task",
        {"task": "do the thing", "goal_profile": "research"},
    )
    assert stub_runner[0]["goal_profile"] == "research"


async def test_delegate_task_surfaces_runner_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_all()

    async def _bad_runner(*_a, **_kw):
        raise RuntimeError("subagent crashed")

    monkeypatch.setattr(delegate_mod, "_runner", _bad_runner)
    tok = set_tool_context(ToolContext(session_id="parent", turn_idx=0))
    try:
        out = await registry.invoke("delegate_task", {"task": "x"})
    finally:
        reset_tool_context(tok)
    assert out["ok"] is False
    assert "subagent crashed" in out["error"]


async def test_delegate_task_real_runner_signature() -> None:
    """Smoke check: the real runner has the expected argspec, so the registry
    can call it without TypeError when ``_runner`` isn't stubbed.

    We don't actually run a subagent here (would require a live DB + LLM key);
    we just inspect the function.
    """
    import inspect

    sig = inspect.signature(delegate_mod._run_subagent_default)
    params = set(sig.parameters)
    assert {"task", "goal_profile", "extra_instructions"} <= params
