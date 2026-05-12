"""Bridge MCP tools into the static ToolRegistry.

Each MCP server's tools become first-class jazz-guru tools named
``mcp_<server>_<tool>``. The handler proxies to the MCP client. Built-in tool
names always win; an MCP tool whose namespaced name would collide is skipped
with a warning.
"""
from __future__ import annotations

import re
from typing import Any

from jazz_guru.actions.mcp.config import MCPServerSpec
from jazz_guru.actions.registry import ToolSpec, registry
from jazz_guru.logging import get_logger

log = get_logger(__name__)

_NAME_SAN = re.compile(r"[^a-z0-9_]+")


def _namespaced(server: str, tool: str) -> str:
    server_clean = _NAME_SAN.sub("_", server.lower()).strip("_") or "x"
    tool_clean = _NAME_SAN.sub("_", tool.lower()).strip("_") or "x"
    return f"mcp_{server_clean}_{tool_clean}"


def _filtered(spec: MCPServerSpec, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    include = spec.include_tools
    exclude = set(spec.exclude_tools or [])
    out: list[dict[str, Any]] = []
    for t in tools:
        name = t.get("name", "")
        if not name:
            continue
        if include is not None and name not in include:
            continue
        if name in exclude:
            continue
        out.append(t)
    return out


def _make_handler(client: Any, original_name: str):
    async def _handler(**kwargs: Any) -> Any:
        return await client.call_tool(original_name, kwargs)

    return _handler


async def bridge_server_to_registry(
    spec: MCPServerSpec, client: Any
) -> list[str]:
    """Register the server's tools under ``mcp_<server>_<tool>``. Returns the
    list of namespaced names that were actually registered."""
    tools = _filtered(spec, getattr(client, "tools", []))
    registered: list[str] = []
    for t in tools:
        name = t["name"]
        ns_name = _namespaced(spec.name, name)
        if ns_name in registry._tools:  # type: ignore[attr-defined]
            existing = registry._tools[ns_name]  # type: ignore[attr-defined]
            if "mcp" not in existing.tags:
                log.warning(
                    "mcp.bridge.skip_collision",
                    server=spec.name,
                    tool=name,
                    namespaced=ns_name,
                    reason="built-in tool with same name",
                )
                continue
            # Same-named MCP tool re-registration: replace.
        schema = t.get("input_schema") or {"type": "object"}
        description = t.get("description") or f"MCP tool {name!r} from server {spec.name}"
        if not description.endswith("."):
            description = description + "."
        registry._tools[ns_name] = ToolSpec(  # type: ignore[attr-defined]
            name=ns_name,
            description=f"[mcp:{spec.name}] {description}",
            input_schema=schema,
            handler=_make_handler(client, name),
            tags=("mcp", spec.name),
        )
        registered.append(ns_name)
    return registered


async def unbridge_server_from_registry(
    server_name: str, bridged_tools: list[str]
) -> None:
    """Remove the namespaced tools previously bridged for ``server_name``."""
    for ns_name in list(bridged_tools):
        # Only remove if it was indeed an MCP-bridged tool (tag check) so we
        # don't accidentally clobber a built-in renamed later.
        spec = registry._tools.get(ns_name)  # type: ignore[attr-defined]
        if spec is None:
            continue
        if "mcp" in spec.tags and server_name in spec.tags:
            registry._tools.pop(ns_name, None)  # type: ignore[attr-defined]
