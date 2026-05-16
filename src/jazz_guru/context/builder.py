from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jazz_guru.config import GoalConfig, get_goal
from jazz_guru.logging import get_logger
from jazz_guru.notes import render_notes_block
from jazz_guru.skills import list_skills_metadata, skills_metadata_block

log = get_logger(__name__)

_TOOL_CREATION_HINT = """
---
## Authoring your own tools
You can create new tools at runtime when none of the existing ones fit:

- `tool_create(name, description, input_schema, source)` — register a new tool
  for THIS session. The source must define `def run(**kwargs)` (sync or async),
  and runs in a sandboxed subprocess (cwd = session workspace) by default.
  After this returns ok, call the new tool by `name`.
- `tool_publish(name)` — persist a session tool to the database so it survives
  restarts and is available to all future sessions. A smoke test is auto-recorded
  from the most recent successful invocation in this session, if any.
- `tool_promote_to_source(name)` — write the tool into the package source tree
  (Tier 3, requires server restart to take effect).
- `tool_remove(name, also_global=False)` — drop a tool.
- `tool_list_dynamic()` / `tool_inspect(name)` — introspection.

Prefer building a small, focused tool over inlining ad hoc python_exec calls
when the same operation will recur. After creating a tool, immediately call
it on a sample input to verify it works; iterate if not.

## Testing published tools
Before `tool_publish`, exercise the tool at least once so a smoke case can be
auto-recorded. For tools that will see meaningful reuse, author explicit cases:

- `tool_test_add(name, case_name, case_spec)` — attach a case. The spec is
  `{case: {input, predicate?}, rubric?}`; predicates use a small DSL over the
  tool's output (paths like `result.x`, ops like `eq`, `len`, `regex`).
- `tool_test_run(name)` — run every enabled case against the published source.
- `tool_test_list(name)` — inspect the suite.

The offline improvement loop only proposes patches for tools that have at
least one test, so adding cases is what makes a published tool repairable.
""".strip()


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
    # When provided, skills are filtered against this set via the conditional
    # activation rules (``requires_tools`` / ``fallback_when_tools``).
    allowed_tools: set[str] | None = None


class ContextBuilder:
    """Assemble the system prompt and message list for the LLM."""

    def __init__(self, goal: GoalConfig | None = None) -> None:
        self.goal = goal or get_goal()

    def build(self, inputs: BuildInputs) -> Prompt:
        sys_parts: list[str] = []
        sys_parts.append(self.goal.render_system_block())
        sys_parts.append(_TOOL_CREATION_HINT)
        # Notes sit between the (rarely changing) goal/tool-hint prefix and the
        # (per-turn) state_doc/memory blocks. They are themselves rarely-changed
        # so this position is friendly to the prompt cache.
        notes_block = render_notes_block()
        if notes_block:
            sys_parts.append(notes_block)
        # Skills metadata: terse list ("- category/name: description"), filtered
        # against the active tool allowlist so the agent only sees relevant
        # skills. Full content is fetched on demand via the `skill_view` tool.
        try:
            skills_meta = list_skills_metadata(allowed_tools=inputs.allowed_tools)
            skills_block = skills_metadata_block(skills_meta)
        except Exception as e:
            log.warning("context.skills_block_failed", err=str(e))
            skills_block = ""
        if skills_block:
            sys_parts.append(skills_block)
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
