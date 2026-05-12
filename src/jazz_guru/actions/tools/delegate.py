"""``delegate_task`` — spawn a child AgentLoop for an isolated sub-task.

The child runs in a fresh session with its own ``DynamicRegistry``,
``BackgroundJobRegistry``, and trace. Only the final ``text`` (and usage
stats) comes back to the parent, so the parent's context budget is
preserved -- exactly Hermes' ``delegate_task`` shape.

Single-turn only for now: the parent supplies a complete task description
and the subagent has up to ``policy.budgets.per_turn.tool_calls`` tool
calls in one step. Multi-turn subsessions are a future extension.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.logging import get_logger

log = get_logger(__name__)


class DelegateTaskInput(BaseModel):
    task: str = Field(
        ...,
        description=(
            "The complete task description handed to the subagent. Be self-"
            "contained: the subagent does not see your conversation history."
        ),
    )
    goal_profile: str = Field(
        default="default",
        description="Goal profile to use for the subagent (defaults to 'default').",
    )
    extra_instructions: str | None = Field(
        default=None,
        description=(
            "Optional preamble inserted as user-message context above the task. "
            "Use this to give the subagent the slice of context it needs."
        ),
    )
    max_turns: int = Field(
        default=1,
        ge=1,
        le=4,
        description="Currently only 1 is supported; reserved for future multi-turn.",
    )


async def _run_subagent_default(
    task: str,
    *,
    goal_profile: str,
    extra_instructions: str | None,
) -> dict[str, Any]:
    """Real subagent runner. Creates a fresh session + AgentLoop and runs one step.

    The session is persisted to the DB so the subagent's tool_use chain has a
    valid foreign key for turns/events. Subsessions inherit the global skills/
    notes/playbook (those are workspace-wide) but get isolated dynamic-tool
    state and trace.
    """
    from jazz_guru.harness import AgentLoop, SessionManager

    sm = SessionManager()
    title_hint = task.strip().splitlines()[0][:80] if task.strip() else "subagent"
    handle = await sm.create(goal_profile=goal_profile, title=f"sub: {title_hint}")
    sub = AgentLoop(handle)
    composed = (
        task
        if not extra_instructions
        else f"{extra_instructions.strip()}\n\n---\n\n{task.strip()}"
    )
    res = await sub.step(composed)
    return {
        "subsession_id": str(handle.id),
        "text": res.text,
        "tool_calls": res.tool_calls,
        "rounds": res.rounds,
        "usage": {
            "input_tokens": res.usage.input_tokens,
            "output_tokens": res.usage.output_tokens,
            "cost_usd": res.usage.cost_usd,
        },
        "errors": list(res.errors),
    }


# Module-level so tests can monkeypatch it without touching the registered
# tool body. Production code always calls through the registry, so this
# indirection costs nothing at runtime.
_runner = _run_subagent_default


@registry.register(
    "delegate_task",
    description=(
        "Spawn a child agent to run an isolated sub-task. The child has its own "
        "conversation context (you save tokens), its own dynamic tools, and its "
        "own trace. Only the child's final summary text -- not its intermediate "
        "tool calls or model output -- is returned to you. Best for: trying "
        "multiple variations in parallel, evaluating long-running operations, "
        "and any sub-task whose intermediate steps you don't need to see."
    ),
    input_model=DelegateTaskInput,
    tags=("control",),
)
async def delegate_task(
    task: str,
    goal_profile: str = "default",
    extra_instructions: str | None = None,
    max_turns: int = 1,
) -> dict[str, Any]:
    parent_ctx = current()
    if max_turns != 1:
        log.info("delegate.multi_turn_not_supported_yet", requested=max_turns)
    try:
        out = await _runner(
            task,
            goal_profile=goal_profile,
            extra_instructions=extra_instructions,
        )
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {
        "ok": True,
        "parent_session_id": parent_ctx.session_id,
        **out,
    }
