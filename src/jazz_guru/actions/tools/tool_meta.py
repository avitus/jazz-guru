"""Meta-tools that let the agent author its own tools at runtime.

* ``tool_create`` — register a new tool for the current session
* ``tool_publish`` — make a session-scoped tool persistent and global
* ``tool_promote_to_source`` — write the tool into ``src/jazz_guru/actions/tools/`` (Tier 3)
* ``tool_remove`` — drop a session-scoped or global tool
* ``tool_list_dynamic`` — introspection
* ``tool_inspect`` — show the source/schema of any dynamic tool
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from jazz_guru.actions import store
from jazz_guru.actions.context import current
from jazz_guru.actions.dynamic import (
    DynamicSpec,
    ToolValidationError,
    hash_source,
    validate_name,
    validate_schema,
    validate_source,
    write_global_tool_file,
    write_session_tool_file,
)
from jazz_guru.actions.registry import registry
from jazz_guru.logging import get_logger

log = get_logger(__name__)

# Optional event bus (set by AgentLoop on each step). Subprocess-friendly: just
# a module-level callable; if unset, events are dropped silently.
_event_sink: Any = None


def set_event_sink(fn: Any) -> None:
    global _event_sink
    _event_sink = fn


def _emit(name: str, payload: dict[str, Any]) -> None:
    if _event_sink is None:
        return
    try:
        _event_sink(name, payload)
    except Exception as e:  # never let event emission break a tool call
        log.warning("tool_meta.emit_failed", err=str(e))


# ---------- tool_create ---------------------------------------------------


class ToolCreateInput(BaseModel):
    name: str = Field(..., description="snake_case name (a-z, digits, underscores)")
    description: str
    input_schema: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON Schema describing the kwargs run() will receive.",
    )
    source: str = Field(
        ...,
        description=(
            "Python source. MUST define `def run(**kwargs)` (or async). "
            "Writes/reads should stay inside the session workspace."
        ),
    )
    execution: str = Field(
        "subprocess",
        description=(
            "'subprocess' (sandboxed, default, recommended) or 'inproc' "
            "(faster, runs in the server process — only for trusted, fast helpers)."
        ),
    )


@registry.register(
    "tool_create",
    description=(
        "Define a new tool for THIS session. The tool body is your Python source; "
        "it must define `run(**kwargs)`. Use this when none of the existing tools "
        "fit the problem. After tool_create returns ok, call the new tool by name. "
        "Dedupe: if a tool by this name exists in this session, it is replaced."
    ),
    input_model=ToolCreateInput,
    tags=("meta",),
)
async def tool_create(
    name: str,
    description: str,
    source: str,
    input_schema: dict[str, Any] | None = None,
    execution: str = "subprocess",
) -> dict[str, Any]:
    try:
        nm = validate_name(name)
        sch = validate_schema(input_schema)
        validate_source(source)
    except ToolValidationError as e:
        return {"ok": False, "error": str(e)}
    if execution not in {"subprocess", "inproc"}:
        return {"ok": False, "error": f"unknown execution mode: {execution}"}

    src = textwrap.dedent(source).strip() + "\n"
    sid = current().session_id
    path = write_session_tool_file(sid, nm, src)

    spec = DynamicSpec(
        name=nm,
        description=description,
        input_schema=sch,
        source=src,
        sha256=hash_source(src),
        execution=execution,
        scope="session",
        owner_session_id=sid,
        source_path=path,
    )
    if registry._dynamic is None:
        return {"ok": False, "error": "no dynamic registry attached for this session"}
    registry._dynamic.add(spec)
    _emit(
        "tool_proposed",
        {
            "name": spec.name,
            "scope": spec.scope,
            "execution": spec.execution,
            "schema": spec.input_schema,
            "sha256": spec.sha256,
            "path": str(spec.source_path) if spec.source_path else None,
        },
    )
    return {
        "ok": True,
        "name": spec.name,
        "scope": spec.scope,
        "execution": spec.execution,
        "sha256": spec.sha256,
        "path": str(path),
        "callable_now": True,
        "tip": (
            "Call the tool with its declared schema. To make it survive across "
            "sessions, follow up with tool_publish."
        ),
    }


# ---------- tool_publish --------------------------------------------------


class ToolPublishInput(BaseModel):
    name: str
    note: str | None = None


@registry.register(
    "tool_publish",
    description=(
        "Persist a session tool to the database so it is available in ALL future "
        "sessions on every server boot. Use only after the tool is verified."
    ),
    input_model=ToolPublishInput,
    tags=("meta",),
)
async def tool_publish(name: str, note: str | None = None) -> dict[str, Any]:
    if registry._dynamic is None:
        return {"ok": False, "error": "no dynamic registry attached"}
    spec = registry._dynamic.get(name)
    if spec is None:
        return {"ok": False, "error": f"no session tool '{name}'"}
    write_global_tool_file(name, spec.source)
    try:
        row_id = await store.upsert(
            name=spec.name,
            description=spec.description,
            input_schema=spec.input_schema,
            source=spec.source,
            scope="global",
            owner_session_id=spec.owner_session_id,
            meta={"execution": spec.execution, "note": note} if note else {"execution": spec.execution},
        )
    except Exception as e:
        return {"ok": False, "error": f"db error: {e}"}
    spec.scope = "global"
    _emit("tool_promoted", {"name": spec.name, "scope": "global", "id": str(row_id)})
    return {"ok": True, "id": str(row_id), "name": spec.name, "scope": "global"}


# ---------- tool_promote_to_source ----------------------------------------


_SRC_TOOLS_DIR = Path(__file__).resolve().parent  # src/jazz_guru/actions/tools/


class ToolPromoteToSourceInput(BaseModel):
    name: str
    description: str | None = None


_SOURCE_TEMPLATE = textwrap.dedent(
    '''\
    """Auto-generated by tool_promote_to_source. Edit only via the agent or with care."""
    from __future__ import annotations

    from typing import Any

    from pydantic import BaseModel

    from jazz_guru.actions.registry import registry


    # ---- begin generated user source --------------------------------
    {body}
    # ---- end generated user source ----------------------------------


    class _Auto_{name}_Input(BaseModel):
        model_config = {{"extra": "allow"}}


    @registry.register(
        "{name}",
        description={description!r},
        input_model=_Auto_{name}_Input,
        tags=("generated",),
    )
    async def _auto_{name}(**kwargs: Any) -> Any:  # type: ignore[no-untyped-def]
        result = run(**kwargs)
        try:
            import inspect
            if inspect.iscoroutine(result):
                result = await result
        except Exception:
            pass
        return result
    '''
)


@registry.register(
    "tool_promote_to_source",
    description=(
        "(Tier 3, gated) Write the tool's source into "
        "src/jazz_guru/actions/tools/<name>.py so it becomes a first-class "
        "package tool after the next server reload. Server reload is NOT "
        "automatic; the user must restart the server. Use sparingly."
    ),
    input_model=ToolPromoteToSourceInput,
    tags=("meta",),
)
async def tool_promote_to_source(name: str, description: str | None = None) -> dict[str, Any]:
    if registry._dynamic is None:
        return {"ok": False, "error": "no dynamic registry attached"}
    spec = registry._dynamic.get(name)
    if spec is None:
        return {"ok": False, "error": f"no session tool '{name}'"}
    desc = description or spec.description
    target = _SRC_TOOLS_DIR / f"{spec.name}.py"
    if target.exists():
        return {"ok": False, "error": f"refusing to overwrite existing source file {target.name}"}
    rendered = _SOURCE_TEMPLATE.format(
        body=textwrap.indent(spec.source, ""),
        name=spec.name,
        description=desc,
    )
    target.write_text(rendered, encoding="utf-8")
    _emit(
        "tool_promoted",
        {"name": spec.name, "scope": "source", "path": str(target)},
    )
    return {
        "ok": True,
        "name": spec.name,
        "scope": "source",
        "path": str(target),
        "next_steps": "restart the server (or run `make server`) so the new tool registers at boot",
    }


# ---------- tool_remove ---------------------------------------------------


class ToolRemoveInput(BaseModel):
    name: str
    also_global: bool = False


@registry.register(
    "tool_remove",
    description=(
        "Remove a dynamic tool. Always removes from this session. "
        "Set also_global=true to also delete from the persistent DB."
    ),
    input_model=ToolRemoveInput,
    tags=("meta",),
)
async def tool_remove(name: str, also_global: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": True, "name": name}
    if registry._dynamic is not None:
        out["session_removed"] = registry._dynamic.remove(name)
    if also_global:
        try:
            out["global_removed"] = await store.remove(name)
        except Exception as e:
            out["global_removed"] = False
            out["global_error"] = str(e)
    return out


# ---------- tool_list_dynamic / tool_inspect -------------------------------


@registry.register(
    "tool_list_dynamic",
    description="List dynamic tools available in this session.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    tags=("meta",),
)
async def tool_list_dynamic() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    if registry._dynamic is not None:
        for s in registry._dynamic.all():
            items.append({
                "name": s.name,
                "scope": s.scope,
                "execution": s.execution,
                "version": s.version,
                "description": s.description,
                "input_schema": s.input_schema,
            })
    return {"items": items}


class ToolInspectInput(BaseModel):
    name: str


@registry.register(
    "tool_inspect",
    description="Return the source + schema of a dynamic tool.",
    input_model=ToolInspectInput,
    tags=("meta",),
)
async def tool_inspect(name: str) -> dict[str, Any]:
    if registry._dynamic is None:
        return {"ok": False, "error": "no dynamic registry attached"}
    spec = registry._dynamic.get(name)
    if spec is None:
        return {"ok": False, "error": f"no dynamic tool '{name}'"}
    return {
        "ok": True,
        "name": spec.name,
        "scope": spec.scope,
        "execution": spec.execution,
        "version": spec.version,
        "description": spec.description,
        "input_schema": spec.input_schema,
        "source": spec.source,
        "sha256": spec.sha256,
    }
