from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jazz_guru.config import GoalConfig, get_goal


@dataclass
class Prompt:
    system: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BuildInputs:
    user_message: dict[str, Any] | str
    history: list[dict[str, Any]] = field(default_factory=list)  # [{role,content}]
    state_doc: str | None = None
    retrieved_memory: list[str] = field(default_factory=list)
    playbook_excerpts: list[str] = field(default_factory=list)
    extra_system: str | None = None


class ContextBuilder:
    """Assemble the system prompt and message list for the LLM."""

    def __init__(self, goal: GoalConfig | None = None) -> None:
        self.goal = goal or get_goal()

    def build(self, inputs: BuildInputs) -> Prompt:
        sys_parts: list[str] = []
        sys_parts.append(self.goal.render_system_block())
        if inputs.state_doc:
            sys_parts.append("\n---\n## Externalized state (self-model)\n" + inputs.state_doc.strip())
        if inputs.playbook_excerpts:
            sys_parts.append("\n---\n## Playbook (durable lessons)\n" + "\n".join(f"- {p}" for p in inputs.playbook_excerpts))
        if inputs.retrieved_memory:
            sys_parts.append("\n---\n## Retrieved memory\n" + "\n".join(f"- {m}" for m in inputs.retrieved_memory))
        if inputs.extra_system:
            sys_parts.append("\n---\n" + inputs.extra_system.strip())

        messages: list[dict[str, Any]] = []
        messages.extend(inputs.history)
        if isinstance(inputs.user_message, str):
            messages.append({"role": "user", "content": inputs.user_message})
        else:
            messages.append(dict(inputs.user_message))

        return Prompt(
            system="\n".join(sys_parts).strip(),
            messages=messages,
            metadata={
                "goal_profile": self.goal.profile,
                "history_len": len(inputs.history),
                "memory_items": len(inputs.retrieved_memory),
                "playbook_items": len(inputs.playbook_excerpts),
            },
        )
