"""MCPManager: start / stop / reload configured servers, mount their tools."""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from jazz_guru.actions.mcp.client import MCPClient
from jazz_guru.actions.mcp.config import (
    MCPConfig,
    MCPServerSpec,
    load_mcp_config,
)
from jazz_guru.actions.mcp.registry_bridge import (
    bridge_server_to_registry,
    unbridge_server_from_registry,
)
from jazz_guru.logging import get_logger

log = get_logger(__name__)


@dataclass
class MCPServerState:
    spec: MCPServerSpec
    client: MCPClient | None = None
    status: str = "stopped"  # stopped | starting | running | failed
    error: str | None = None
    tool_count: int = 0
    bridged_tools: list[str] = field(default_factory=list)


class MCPManager:
    """Owns the lifecycle for all configured MCP servers in one process."""

    def __init__(
        self,
        config: MCPConfig | None = None,
        *,
        # Bound for stdio crash retries before we give up and mark as failed.
        max_start_retries: int = 3,
    ) -> None:
        self.config = config or load_mcp_config()
        self.max_start_retries = max_start_retries
        self.states: dict[str, MCPServerState] = {
            s.name: MCPServerState(spec=s) for s in self.config.servers
        }

    # ---------- discovery / startup --------------------------------------

    async def start_all(self) -> None:
        if not self.states:
            log.info("mcp.start_all", count=0)
            return
        log.info("mcp.start_all", count=len(self.states))
        # Start servers concurrently — a slow one shouldn't block fast ones.
        await asyncio.gather(*(self._start_one(name) for name in list(self.states)))

    async def _start_one(self, name: str) -> None:
        state = self.states[name]
        if not state.spec.enabled:
            state.status = "disabled"
            return
        state.status = "starting"
        attempt = 0
        delay = 0.5
        last_err: Exception | None = None
        while attempt < self.max_start_retries:
            attempt += 1
            client = MCPClient(state.spec)
            try:
                await client.start()
                state.client = client
                state.tool_count = len(client.tools)
                state.status = "running"
                state.bridged_tools = await bridge_server_to_registry(
                    state.spec, client
                )
                state.error = None
                log.info(
                    "mcp.server.up",
                    name=name,
                    tool_count=state.tool_count,
                    bridged=len(state.bridged_tools),
                )
                return
            except Exception as e:
                last_err = e
                log.warning(
                    "mcp.server.start_failed",
                    name=name,
                    attempt=attempt,
                    err=str(e),
                )
                with contextlib.suppress(Exception):
                    await client.stop()
                # Reset any partial state the try-block may have already
                # written — e.g. client.start() succeeded but
                # bridge_server_to_registry() raised, leaving state.client
                # pointing at a now-stopped client.
                state.client = None
                state.tool_count = 0
                state.bridged_tools = []
                state.status = "starting"
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, 5.0)
        state.status = "failed"
        state.error = str(last_err) if last_err else "unknown error"

    # ---------- runtime ops ---------------------------------------------

    async def stop_all(self) -> None:
        for name in list(self.states):
            await self._stop_one(name)

    async def reload(self) -> None:
        """Re-read mcp.yaml from disk and reconcile (stop removed, start new)."""
        new_cfg = load_mcp_config()
        new_names = {s.name for s in new_cfg.servers}
        # Stop servers no longer in config.
        for name in list(self.states):
            if name not in new_names:
                await self._stop_one(name)
                self.states.pop(name, None)
        # Add new ones; restart changed ones.
        for spec in new_cfg.servers:
            existing = self.states.get(spec.name)
            if existing is None:
                self.states[spec.name] = MCPServerState(spec=spec)
                await self._start_one(spec.name)
            elif existing.spec != spec:
                await self._stop_one(spec.name)
                self.states[spec.name] = MCPServerState(spec=spec)
                await self._start_one(spec.name)
            # else: unchanged, leave running
        self.config = new_cfg

    async def _stop_one(self, name: str) -> None:
        state = self.states.get(name)
        if state is None:
            return
        # Decouple unbridge from client.stop(): if the registry-bridge step
        # fails, we still want to terminate the underlying subprocess /
        # socket so it doesn't leak.
        if state.bridged_tools:
            try:
                await unbridge_server_from_registry(state.spec.name, state.bridged_tools)
            except Exception as e:
                log.warning("mcp.unbridge_failed", name=name, err=str(e))
        if state.client is not None:
            try:
                await state.client.stop()
            except Exception as e:
                log.warning("mcp.client_stop_failed", name=name, err=str(e))
        state.client = None
        state.status = "stopped"
        state.tool_count = 0
        state.error = None
        state.bridged_tools = []

    def status(self) -> dict[str, Any]:
        out = {
            "servers": [
                {
                    "name": s.spec.name,
                    "transport": s.spec.transport,
                    "enabled": s.spec.enabled,
                    "status": s.status,
                    "tool_count": s.tool_count,
                    "bridged_tools": list(s.bridged_tools),
                    "error": s.error,
                }
                for s in self.states.values()
            ]
        }
        return out
