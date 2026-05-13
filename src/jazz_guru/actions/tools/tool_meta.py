"""Meta-tools that let the agent author its own tools at runtime.

* ``tool_create`` — register a new tool for the current session
* ``tool_publish`` — make a session-scoped tool persistent and global
* ``tool_promote_to_source`` — write the tool into ``src/jazz_guru/actions/tools/`` (Tier 3)
* ``tool_remove`` — drop a session-scoped or global tool
* ``tool_list_dynamic`` — introspection
* ``tool_inspect`` — show the source/schema of any dynamic tool
"""
from __future__ import annotations

import asyncio
import json
import textwrap
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

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
from jazz_guru.config import get_settings
from jazz_guru.db import session_scope
from jazz_guru.logging import get_logger
from jazz_guru.state import GeneratedToolTest

log = get_logger(__name__)

# Optional event bus (set by AgentLoop on each step). Per-async-task scope:
# concurrent turns must not see each other's sinks. Unset → events dropped.
_EVENT_SINK: ContextVar[Any] = ContextVar("jg_meta_event_sink", default=None)


def set_event_sink(fn: Any) -> Token[Any]:
    """Bind an event sink for the current async task. Returns a reset Token."""
    return _EVENT_SINK.set(fn)


def reset_event_sink(token: Token[Any] | None) -> None:
    if token is None:
        _EVENT_SINK.set(None)
    else:
        _EVENT_SINK.reset(token)


def _emit(name: str, payload: dict[str, Any]) -> None:
    sink = _EVENT_SINK.get()
    if sink is None:
        return
    try:
        sink(name, payload)
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
            "'subprocess' is the only accepted value. Same-process execution "
            "would run agent-authored code with the server's full interpreter "
            "state and OS permissions, bypassing the per-tool isolation "
            "boundary, so it is no longer permitted from session-authored "
            "tools."
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
    if execution != "subprocess":
        # 'inproc' previously ran agent-authored code inside the server
        # process. That bypasses the subprocess isolation boundary, so we
        # disallow it here. The dynamic-tool runner still recognizes the
        # mode for trusted/internal callers via DynamicSpec.execution, but
        # nothing in the agent-facing surface should be able to set it.
        return {"ok": False, "error": f"unsupported execution mode: {execution!r} (only 'subprocess' is allowed)"}

    src = textwrap.dedent(source).strip() + "\n"
    sid = current().session_id
    # Check for a bound dynamic registry BEFORE touching the filesystem so
    # we don't leave an orphan .py file for a tool that was never
    # registered (e.g. when tool_create is called outside an AgentLoop
    # turn).
    dyn = registry.current_dynamic()
    if dyn is None:
        return {"ok": False, "error": "no dynamic registry attached for this session"}
    path = await asyncio.to_thread(write_session_tool_file, sid, nm, src)

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
    dyn.add(spec)
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


def _find_last_successful_invocation(
    name: str, session_id: str | None
) -> dict[str, Any] | None:
    """Scan the session trace for the most recent successful call to ``name``.

    Returns the input kwargs of the matching call (suitable for replaying
    as a smoke test), or None if no successful call is recorded. A call is
    "successful" iff the paired ``tool_result`` event has neither
    ``ok: False`` nor an ``error`` field — the same shape ``ActionController``
    emits on the happy path.
    """
    if not session_id:
        return None
    trace_dir = Path(get_settings().jg_trace_dir).resolve()
    p = (trace_dir / f"{session_id}.jsonl").resolve()
    try:
        p.relative_to(trace_dir)
    except ValueError:
        return None
    if not p.exists():
        return None
    records: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    # Pair tool_results to tool_uses by id; we want the most recent
    # tool_use for `name` whose paired result indicates success.
    results_by_id: dict[str, dict[str, Any]] = {}
    for rec in records:
        if rec.get("type") != "tool_result":
            continue
        payload = rec.get("payload") or {}
        rid = payload.get("id")
        if isinstance(rid, str):
            results_by_id[rid] = payload
    for rec in reversed(records):
        if rec.get("type") != "tool_use":
            continue
        payload = rec.get("payload") or {}
        if payload.get("name") != name:
            continue
        tool_use_id = payload.get("id")
        if not isinstance(tool_use_id, str):
            continue
        result = results_by_id.get(tool_use_id)
        if result is None:
            continue
        if result.get("ok") is False or "error" in result:
            continue
        input_args = payload.get("input")
        if isinstance(input_args, dict):
            return input_args
        return {}
    return None


async def _record_smoke_case(tool_id: Any, input_args: dict[str, Any]) -> bool:
    """Idempotently attach a smoke_recorded case to ``tool_id``.

    Replaces any existing smoke_recorded case so re-publishing after a
    fix updates the smoke baseline rather than letting it drift. Returns
    True iff a row was upserted.
    """
    spec_payload = {
        "case": {
            "input": input_args,
            "predicate": {
                "result": {"type": "object"},
                "result.__error__": {"absent": True},
            },
        }
    }
    case_name = "smoke_recorded"
    async with session_scope() as s:
        existing = (
            await s.execute(
                select(GeneratedToolTest)
                .where(GeneratedToolTest.tool_id == tool_id)
                .where(GeneratedToolTest.name == case_name)
            )
        ).scalar_one_or_none()
        if existing is None:
            s.add(
                GeneratedToolTest(
                    tool_id=tool_id,
                    name=case_name,
                    spec=spec_payload,
                    origin="smoke_recorded",
                    enabled=True,
                )
            )
        else:
            existing.spec = spec_payload
            existing.enabled = True
            existing.origin = "smoke_recorded"
        await s.flush()
    return True


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
    dyn = registry.current_dynamic()
    if dyn is None:
        return {"ok": False, "error": "no dynamic registry attached"}
    spec = dyn.get(name)
    if spec is None:
        return {"ok": False, "error": f"no session tool '{name}'"}
    # DB upsert FIRST: if it fails, we don't want a stale .py file in
    # generated_tools/ pointing at a tool the registry won't know about.
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
    # The DB is the source of truth — once upsert succeeds, the tool is
    # discoverable by every future session. Mirroring to generated_tools/
    # is a convenience for inspection; if it fails we report a warning but
    # don't pretend the publish failed.
    warning: str | None = None
    try:
        await asyncio.to_thread(write_global_tool_file, spec.name, spec.source)
    except OSError as e:
        warning = f"published in DB, but failed to mirror generated_tools/{spec.name}.py: {e}"
        log.warning("tool_publish.file_mirror_failed", name=spec.name, err=str(e))
    spec.scope = "global"

    # Record a smoke test from the most recent successful invocation in
    # this session's trace (plan §A.6). Best-effort: a tool published
    # without any prior call just won't get one, and the improvement loop
    # will skip the tool until tests are added manually.
    smoke_recorded = False
    try:
        sid = current().session_id
        # Trace scanning does blocking file I/O. Offload to a thread so a
        # large session trace doesn't stall the event loop while publish
        # is running.
        last_input = await asyncio.to_thread(
            _find_last_successful_invocation, spec.name, sid
        )
        if last_input is not None:
            smoke_recorded = await _record_smoke_case(row_id, last_input)
    except Exception as e:  # never let smoke-recording break a publish
        log.warning("tool_publish.smoke_recording_failed", name=spec.name, err=str(e))

    _emit("tool_promoted", {"name": spec.name, "scope": "global", "id": str(row_id)})
    out: dict[str, Any] = {
        "ok": True,
        "id": str(row_id),
        "name": spec.name,
        "scope": "global",
        "smoke_recorded": smoke_recorded,
    }
    if warning:
        out["warning"] = warning
    if not smoke_recorded:
        out["tip"] = (
            "No smoke case was recorded (no successful prior invocation found in "
            "this session's trace). Authoring at least one tool_test_add case is "
            "required for the improvement loop to evaluate this tool."
        )
    return out


# ---------- tool_promote_to_source ----------------------------------------


_SRC_TOOLS_DIR = Path(__file__).resolve().parent  # src/jazz_guru/actions/tools/


class ToolPromoteToSourceInput(BaseModel):
    name: str
    description: str | None = None


_BODY_MARKER = "__JG_USER_BODY__"

_SOURCE_TEMPLATE = textwrap.dedent(
    '''\
    """Auto-generated by tool_promote_to_source. Edit only via the agent or with care."""
    from __future__ import annotations

    from typing import Any

    from pydantic import BaseModel

    from jazz_guru.actions.registry import registry


    # ---- begin generated user source --------------------------------
    __JG_USER_BODY__
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
        import inspect
        result = run(**kwargs)
        if inspect.iscoroutine(result):
            # Don't swallow exceptions raised by the user's run() — let them
            # propagate so the controller can surface them as tool errors.
            result = await result
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
    dyn = registry.current_dynamic()
    if dyn is None:
        return {"ok": False, "error": "no dynamic registry attached"}
    spec = dyn.get(name)
    if spec is None:
        return {"ok": False, "error": f"no session tool '{name}'"}
    desc = description or spec.description
    target = _SRC_TOOLS_DIR / f"{spec.name}.py"
    if target.exists():
        return {"ok": False, "error": f"refusing to overwrite existing source file {target.name}"}
    # Two-step substitution. Format FIRST (resolves {name} / {description}
    # placeholders and the escaped `{{...}}` literals; the marker is not
    # a format field, so it passes through). THEN replace the marker with
    # the user source. Reversing the order would re-expose any `{...}` in
    # spec.source to str.format and crash on dict literals / f-strings.
    formatted = _SOURCE_TEMPLATE.format(name=spec.name, description=desc)
    rendered = formatted.replace(_BODY_MARKER, textwrap.indent(spec.source, ""))
    await asyncio.to_thread(target.write_text, rendered, encoding="utf-8")
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
    dyn = registry.current_dynamic()
    if dyn is not None:
        out["session_removed"] = dyn.remove(name)
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
    dyn = registry.current_dynamic()
    if dyn is not None:
        for s in dyn.all():
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
    dyn = registry.current_dynamic()
    if dyn is None:
        return {"ok": False, "error": "no dynamic registry attached"}
    spec = dyn.get(name)
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
