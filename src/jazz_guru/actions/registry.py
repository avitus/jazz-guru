from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

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
            schema: dict[str, Any]
            if input_model is not None:
                schema = input_model.model_json_schema()
                schema.pop("title", None)
            elif input_schema is not None:
                schema = input_schema
            else:
                schema = {"type": "object", "properties": {}, "additionalProperties": False}
            self._tools[name] = ToolSpec(
                name=name,
                description=description,
                input_schema=schema,
                handler=fn,
                tags=tags,
            )
            return fn

        return deco

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> ToolSpec:
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def all_specs(self) -> list[ToolSpec]:
        return [self._tools[n] for n in self.names()]

    def to_anthropic(self, allowed: set[str] | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for name in self.names():
            if allowed is not None and name not in allowed:
                continue
            out.append(self._tools[name].to_anthropic())
        return out

    async def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        spec = self._tools[name]
        result = spec.handler(**arguments)
        if inspect.isawaitable(result):
            result = await result
        return result


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
        python_exec,
        render,
        shell,
        tts,
        vision,
        web_search,
    )

    return registry
