"""Dynamic tool loader.

Loads agent-authored Python source as a callable tool. Two execution strategies:

* **subprocess**: same sandbox model as ``python_exec`` — the user code is
  executed in a fresh ``python -I`` subprocess that pickles the tool input on
  stdin and prints the result on stdout. This is the default and safest mode.
* **inproc**:    the user code is loaded with ``importlib.util`` inside the
  server process. Faster, but no isolation; only used when explicitly chosen
  by the agent (e.g. for trivially fast helpers).

Each generated tool exposes a ``run(**kwargs) -> dict | str`` (or async).
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
import re
import sys
import textwrap
import uuid as uuid_mod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jazz_guru.actions.context import current
from jazz_guru.actions.sandbox import session_workspace, workspace_root
from jazz_guru.config import get_policy

VALID_NAME = re.compile(r"^[a-z][a-z0-9_]{1,62}$")
RESERVED = {
    "fs_read",
    "fs_write",
    "fs_list",
    "shell",
    "http_get",
    "http_post",
    "python_exec",
    "code_gen",
    "code_edit",
    "web_search",
    "vision",
    "audio_analyze",
    "music_xml_to_midi",
    "midi_to_music_xml",
    "render_midi",
    "tts",
    "tool_create",
    "tool_remove",
    "tool_list_dynamic",
    "tool_publish",
    "tool_promote_to_source",
    "tool_inspect",
}


class ToolValidationError(ValueError):
    """Raised when a dynamic tool definition is rejected (name/schema/source)."""


@dataclass
class DynamicSpec:
    """In-memory representation of a runnable dynamic tool."""

    name: str
    description: str
    input_schema: dict[str, Any]
    source: str
    sha256: str
    execution: str = "subprocess"
    scope: str = "session"  # session | global | source
    owner_session_id: str | None = None
    source_path: Path | None = None
    version: int = 1
    meta: dict[str, Any] | None = None


# ---------- validation -----------------------------------------------------


def validate_name(name: str) -> str:
    if not VALID_NAME.match(name):
        raise ToolValidationError(
            f"invalid tool name '{name}': must match {VALID_NAME.pattern}"
        )
    if name in RESERVED:
        raise ToolValidationError(f"name '{name}' is reserved")
    return name


def validate_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    if schema is None:
        return {"type": "object", "properties": {}, "additionalProperties": False}
    if not isinstance(schema, dict):
        raise ToolValidationError("input_schema must be an object")
    if schema.get("type", "object") != "object":
        raise ToolValidationError("top-level input_schema.type must be 'object'")
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    schema.setdefault("additionalProperties", False)
    return schema


def validate_source(source: str) -> None:
    """Cheap static checks. The real isolation is the subprocess sandbox."""
    if not source.strip():
        raise ToolValidationError("source is empty")
    try:
        compile(source, "<dynamic-tool>", "exec")
    except SyntaxError as e:
        raise ToolValidationError(f"syntax error: {e}") from e
    if "def run" not in source:
        raise ToolValidationError("source must define a top-level `run` function")


def hash_source(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


# ---------- in-memory registry --------------------------------------------


class DynamicRegistry:
    """Per-session overlay on top of the static :class:`ToolRegistry`."""

    def __init__(self) -> None:
        self._specs: dict[str, DynamicSpec] = {}

    def add(self, spec: DynamicSpec) -> None:
        self._specs[spec.name] = spec

    def remove(self, name: str) -> bool:
        return self._specs.pop(name, None) is not None

    def get(self, name: str) -> DynamicSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs.keys())

    def all(self) -> list[DynamicSpec]:
        return [self._specs[n] for n in self.names()]

    def __contains__(self, name: str) -> bool:
        return name in self._specs


# ---------- runners --------------------------------------------------------


def _runner_template() -> str:
    # Extracted to a function for easier testing/inspection.
    return textwrap.dedent(
        '''
        import json, sys, traceback, asyncio, inspect

        # ---- begin user source -------------------------------------------
        {user_source}
        # ---- end user source ---------------------------------------------

        def _main():
            try:
                payload = json.loads(sys.stdin.read() or "{{}}")
            except Exception as e:
                print(json.dumps({{"__error__": f"bad input: {{e}}"}}), flush=True)
                sys.exit(2)
            try:
                result = run(**payload)
                if inspect.iscoroutine(result):
                    result = asyncio.get_event_loop().run_until_complete(result)
            except Exception as e:
                print(json.dumps({{
                    "__error__": f"{{type(e).__name__}}: {{e}}",
                    "traceback": traceback.format_exc(),
                }}), flush=True)
                sys.exit(1)
            try:
                print(json.dumps(result, default=str), flush=True)
            except Exception:
                print(json.dumps({{"value": str(result)}}), flush=True)

        _main()
        '''
    ).strip()


async def _run_subprocess(spec: DynamicSpec, kwargs: dict[str, Any]) -> Any:
    policy = get_policy().for_tool("python_exec")
    timeout = policy.timeout_sec or 30
    src = _runner_template().format(user_source=spec.source)
    cwd = session_workspace(current().session_id)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-c",
        src,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=json.dumps(kwargs).encode("utf-8")),
            timeout=timeout,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"__error__": f"timeout after {timeout}s"}
    out_s = out.decode("utf-8", errors="replace").strip()
    err_s = err.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        try:
            return json.loads(out_s.splitlines()[-1]) if out_s else {"__error__": err_s or "non-zero exit"}
        except Exception:
            return {"__error__": err_s or out_s}
    if not out_s:
        return {"__error__": "no output"}
    try:
        return json.loads(out_s.splitlines()[-1])
    except Exception:
        return {"value": out_s, "stderr": err_s}


async def _run_inproc(spec: DynamicSpec, kwargs: dict[str, Any]) -> Any:
    mod_name = f"jazz_guru._dyn.{spec.name}_{uuid_mod.uuid4().hex[:8]}"
    mod = sys.modules.get(mod_name) or _load_module(mod_name, spec.source)
    fn = getattr(mod, "run", None)
    if fn is None:
        return {"__error__": "no run() in source"}
    res = fn(**kwargs)
    if inspect.iscoroutine(res):
        res = await res
    return res


def _load_module(mod_name: str, source: str) -> Any:
    spec = importlib.util.spec_from_loader(mod_name, loader=None)
    if spec is None:
        raise RuntimeError("could not build module spec")
    mod = importlib.util.module_from_spec(spec)
    exec(source, mod.__dict__)
    sys.modules[mod_name] = mod
    return mod


async def invoke(spec: DynamicSpec, kwargs: dict[str, Any]) -> Any:
    if spec.execution == "inproc":
        return await _run_inproc(spec, kwargs)
    return await _run_subprocess(spec, kwargs)


# ---------- session-tool storage on disk ----------------------------------


def session_tools_dir(session_id: str | None) -> Path:
    base = session_workspace(session_id) / "tools"
    base.mkdir(parents=True, exist_ok=True)
    return base


def global_tools_dir() -> Path:
    base = workspace_root() / "generated_tools"
    base.mkdir(parents=True, exist_ok=True)
    return base


def write_session_tool_file(session_id: str | None, name: str, source: str) -> Path:
    p = session_tools_dir(session_id) / f"{name}.py"
    p.write_text(source, encoding="utf-8")
    return p


def write_global_tool_file(name: str, source: str) -> Path:
    p = global_tools_dir() / f"{name}.py"
    p.write_text(source, encoding="utf-8")
    return p
