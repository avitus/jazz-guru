"""Model Context Protocol (MCP) integration.

Lets the agent talk to any MCP server (stdio or HTTP) and have its tools
appear in the registry as ``mcp_<server>_<tool>``. The official ``mcp``
Python SDK is an optional dependency (install via ``pip install jazz-guru[mcp]``).
"""
from __future__ import annotations

from jazz_guru.actions.mcp.config import (
    MCPConfig,
    MCPError,
    MCPServerSpec,
    load_mcp_config,
)
from jazz_guru.actions.mcp.manager import MCPManager, MCPServerState
from jazz_guru.actions.mcp.registry_bridge import (
    bridge_server_to_registry,
    unbridge_server_from_registry,
)

__all__ = [
    "MCPConfig",
    "MCPError",
    "MCPManager",
    "MCPServerSpec",
    "MCPServerState",
    "bridge_server_to_registry",
    "load_mcp_config",
    "unbridge_server_from_registry",
]
