from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from jazz_guru.actions.schema import normalize_input_schema

if TYPE_CHECKING:
    from jazz_guru.actions.dynamic import DynamicRegistry, DynamicSpec


# Per-async-task overlay of dynamic tools on top of the static registry.
# Concurrent turns (e.g. multiple WS sessions in the same process) each
# get their own view; mutating a module global here would race.
_DYNAMIC_OVERLAY: ContextVar[DynamicRegistry | None] = ContextVar(
    "jg_dynamic_overlay", default=None
)

ToolHandler = Callable[..., Awaitable[Any]]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    tags: tuple[str, ...] = field(default_factory=tuple)

    def to_anthropic(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def attach_dynamic(self, dyn: DynamicRegistry) -> Token[DynamicRegistry | None]:
        """Bind ``dyn`` as the dynamic overlay for the current async task.

        Returns a Token to pass back to :meth:`detach_dynamic`. ContextVar
        scoping means concurrent tasks each get their own overlay.
        """
        return _DYNAMIC_OVERLAY.set(dyn)

    def detach_dynamic(self, token: Token[DynamicRegistry | None] | None = None) -> None:
        if token is None:
            _DYNAMIC_OVERLAY.set(None)
        else:
            _DYNAMIC_OVERLAY.reset(token)

    def current_dynamic(self) -> DynamicRegistry | None:
        """Return the dynamic overlay bound for this async task, or None."""
        return _DYNAMIC_OVERLAY.get()

    def _dyn_get(self, name: str) -> DynamicSpec | None:
        d = _DYNAMIC_OVERLAY.get()
        return d.get(name) if d else None

    def _dyn_names(self) -> list[str]:
        d = _DYNAMIC_OVERLAY.get()
        return d.names() if d else []

    def register(
        self,
        name: str,
        *,
        description: str,
        input_model: type[BaseModel] | None = None,
        input_schema: dict[str, Any] | None = None,
        tags: tuple[str, ...] = (),
    ) -> Callable[[ToolHandler], ToolHandler]:
        def deco(fn: ToolHandler) -> ToolHandler:
            raw: dict[str, Any] | None
            if input_model is not None:
                raw = input_model.model_json_schema()
            elif input_schema is not None:
                raw = input_schema
            else:
                raw = None
            self._tools[name] = ToolSpec(
                name=name,
                description=description,
                input_schema=normalize_input_schema(raw),
                handler=fn,
                tags=tags,
            )
            return fn

        return deco

    def __contains__(self, name: str) -> bool:
        return name in self._tools or name in self._dyn_names()

    def get(self, name: str) -> ToolSpec:
        if name in self._tools:
            return self._tools[name]
        d = self._dyn_get(name)
        if d is not None:
            return ToolSpec(
                name=d.name,
                description=d.description,
                input_schema=d.input_schema,
                handler=_dyn_handler(d),
                tags=("dynamic",),
            )
        raise KeyError(name)

    def names(self) -> list[str]:
        return sorted(set(self._tools) | set(self._dyn_names()))

    def all_specs(self) -> list[ToolSpec]:
        return [self.get(n) for n in self.names()]

    def to_anthropic(self, allowed: set[str] | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name in self.names():
            if allowed is not None and name not in allowed:
                continue
            out.append(self.get(name).to_anthropic())
        return out

    async def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        if name in self._tools:
            spec = self._tools[name]
            result = spec.handler(**arguments)
            if inspect.isawaitable(result):
                result = await result
            return result
        d = self._dyn_get(name)
        if d is not None:
            from jazz_guru.actions.dynamic import invoke as dyn_invoke

            return await dyn_invoke(d, arguments or {})
        raise KeyError(f"unknown tool: {name}")


def _dyn_handler(spec: DynamicSpec) -> ToolHandler:
    async def _h(**kwargs: Any) -> Any:
        from jazz_guru.actions.dynamic import invoke as dyn_invoke

        return await dyn_invoke(spec, kwargs)

    return _h


registry = ToolRegistry()


def register_all() -> ToolRegistry:
    """Import all tool modules so their @registry.register decorators run."""
    from jazz_guru.actions.tools import (  # noqa: F401
        audio_analyze,
        code_gen,
        fs,
        http,
        midi,
        music_xml,
        presets,
        python_exec,
        render,
        shell,
        tool_meta,
        tool_test_meta,
        tts,
        vision,
        web_search,
    )

    return registry
