"""Thin wrapper over the ``mcp`` Python SDK for a single configured server.

The SDK is an optional dependency. ``MCPClient`` imports it lazily so a
jazz-guru install without ``mcp`` extra still works -- attempts to actually
start a client just raise a friendly error.
"""
from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any

from jazz_guru.actions.mcp.config import MCPError, MCPServerSpec
from jazz_guru.logging import get_logger

log = get_logger(__name__)


def _require_mcp() -> Any:
    try:
        import mcp  # noqa: F401
    except ImportError as e:
        raise MCPError(
            "mcp Python SDK is not installed. Install with `pip install 'jazz-guru[mcp]'`"
            " (or `pip install mcp` directly)."
        ) from e
    return None


def _flatten_result(result: Any) -> Any:
    """Convert an MCP CallToolResult into something JSON-serializable."""
    # mcp.types.CallToolResult has `content: list[Content]` and `isError: bool`.
    content_blocks = getattr(result, "content", None) or []
    parts: list[str] = []
    for block in content_blocks:
        # Each block is a TextContent / ImageContent / etc. We collapse to str.
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
            continue
        data = getattr(block, "data", None)
        if data is not None:
            parts.append(f"<{type(block).__name__}: {len(data)} bytes>")
            continue
        # Best-effort fallback: just JSON-dump the object's dict if possible.
        try:
            parts.append(json.dumps(getattr(block, "model_dump", lambda: {})()))
        except Exception:
            parts.append(repr(block))
    joined = "\n".join(parts) if parts else ""
    is_error = bool(getattr(result, "isError", False))
    return {"text": joined, "is_error": is_error}


class MCPClient:
    """One long-lived MCP session per configured server."""

    def __init__(self, spec: MCPServerSpec) -> None:
        self.spec = spec
        self._stack: AsyncExitStack | None = None
        self._session: Any = None  # mcp.client.session.ClientSession
        self._tools: list[dict[str, Any]] = []
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def tools(self) -> list[dict[str, Any]]:
        return list(self._tools)

    async def start(self) -> None:
        if self._started:
            return
        _require_mcp()
        # Imports are deferred so module load doesn't fail without the SDK.
        from mcp import ClientSession  # type: ignore[import-not-found]

        self._stack = AsyncExitStack()
        try:
            if self.spec.is_stdio():
                from mcp import StdioServerParameters  # type: ignore[import-not-found]
                from mcp.client.stdio import stdio_client  # type: ignore[import-not-found]

                if not self.spec.command:
                    raise MCPError(f"{self.spec.name}: stdio requires 'command'")
                params = StdioServerParameters(
                    command=self.spec.command,
                    args=list(self.spec.args),
                    env=dict(self.spec.env) or None,
                    cwd=self.spec.cwd,
                )
                transport = await self._stack.enter_async_context(stdio_client(params))
                read, write = transport[0], transport[1]
            else:
                from mcp.client.streamable_http import (  # type: ignore[import-not-found]
                    streamablehttp_client,
                )

                if not self.spec.url:
                    raise MCPError(f"{self.spec.name}: http requires 'url'")
                transport = await self._stack.enter_async_context(
                    streamablehttp_client(self.spec.url, headers=dict(self.spec.headers))
                )
                read, write = transport[0], transport[1]
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
            await self._refresh_tools()
            self._started = True
        except Exception:
            # Tear down the partial stack so we don't leak the subprocess/socket
            # on a failed start.
            if self._stack is not None:
                try:
                    await self._stack.aclose()
                except Exception as e:
                    log.warning("mcp.start_cleanup_failed", server=self.spec.name, err=str(e))
                self._stack = None
                self._session = None
            raise

    async def _refresh_tools(self) -> None:
        if self._session is None:
            return
        result = await self._session.list_tools()
        tools: list[dict[str, Any]] = []
        for t in getattr(result, "tools", []):
            tools.append({
                "name": getattr(t, "name", ""),
                "description": getattr(t, "description", "") or "",
                "input_schema": getattr(t, "inputSchema", None) or {"type": "object"},
            })
        self._tools = tools

    async def refresh_tools(self) -> list[dict[str, Any]]:
        await self._refresh_tools()
        return list(self._tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if not self._started or self._session is None:
            raise MCPError(f"{self.spec.name}: client not started")
        result = await self._session.call_tool(name, arguments or {})
        return _flatten_result(result)

    async def stop(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception as e:
                log.warning("mcp.stop_failed", server=self.spec.name, err=str(e))
        self._stack = None
        self._session = None
        self._started = False
        self._tools = []
