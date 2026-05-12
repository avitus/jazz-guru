"""MCP server config: parse ``config/mcp.yaml`` into typed dataclasses."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jazz_guru.config import get_settings


class MCPError(ValueError):
    """Raised on MCP config / lifecycle errors."""


@dataclass
class MCPServerSpec:
    name: str
    transport: str  # "stdio" | "http"
    # stdio fields
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    # http fields
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # filtering
    enabled: bool = True
    include_tools: list[str] | None = None  # whitelist; None = all
    exclude_tools: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def is_stdio(self) -> bool:
        return self.transport == "stdio"

    def is_http(self) -> bool:
        return self.transport == "http"


@dataclass
class MCPConfig:
    servers: list[MCPServerSpec] = field(default_factory=list)

    def get(self, name: str) -> MCPServerSpec | None:
        for s in self.servers:
            if s.name == name:
                return s
        return None


def _parse_server(name: str, data: dict[str, Any]) -> MCPServerSpec:
    if not isinstance(data, dict):
        raise MCPError(f"server {name!r}: config must be a mapping")
    has_command = "command" in data
    has_url = "url" in data
    if has_command and has_url:
        raise MCPError(
            f"server {name!r}: specify only one of 'command' (stdio) or 'url' (http), not both"
        )
    if has_command:
        transport = "stdio"
    elif has_url:
        transport = "http"
    else:
        raise MCPError(
            f"server {name!r}: must specify either 'command' (stdio) or 'url' (http)"
        )

    tools_block = data.get("tools") or {}
    include: list[str] | None
    if isinstance(tools_block, dict) and "include" in tools_block:
        inc = tools_block.get("include") or []
        include = [str(x) for x in inc] if inc else []
    else:
        include = None
    exclude = [str(x) for x in (tools_block.get("exclude") or [])] if isinstance(tools_block, dict) else []

    # Validate types so bad YAML surfaces a clear MCPError instead of a
    # late TypeError / AttributeError during the coercion.
    raw_args = data.get("args") or []
    if not isinstance(raw_args, list):
        raise MCPError(f"server {name!r}: 'args' must be a list")
    raw_env = data.get("env") or {}
    if not isinstance(raw_env, dict):
        raise MCPError(f"server {name!r}: 'env' must be a mapping")
    raw_headers = data.get("headers") or {}
    if not isinstance(raw_headers, dict):
        raise MCPError(f"server {name!r}: 'headers' must be a mapping")
    raw_enabled = data.get("enabled", True)
    if not isinstance(raw_enabled, bool):
        raise MCPError(f"server {name!r}: 'enabled' must be a boolean")

    return MCPServerSpec(
        name=name,
        transport=transport,
        command=data.get("command"),
        args=[str(a) for a in raw_args],
        env={str(k): str(v) for k, v in raw_env.items()},
        cwd=data.get("cwd"),
        url=data.get("url"),
        headers={str(k): str(v) for k, v in raw_headers.items()},
        enabled=raw_enabled,
        include_tools=include,
        exclude_tools=exclude,
        meta={k: v for k, v in data.items() if k not in {
            "command", "args", "env", "cwd", "url", "headers",
            "enabled", "tools",
        }},
    )


def load_mcp_config_from_path(path: Path) -> MCPConfig:
    if not path.exists():
        return MCPConfig(servers=[])
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise MCPError(f"{path}: top-level must be a mapping")
    servers_block = data.get("mcp_servers") or {}
    if not isinstance(servers_block, dict):
        raise MCPError(f"{path}: 'mcp_servers' must be a mapping of name -> spec")
    servers: list[MCPServerSpec] = []
    for name, spec_data in servers_block.items():
        servers.append(_parse_server(str(name), spec_data))
    return MCPConfig(servers=servers)


def mcp_config_path() -> Path:
    settings = get_settings()
    # Reuse the policy file's parent (typically ./config/) so all YAML configs
    # live in the same place.
    return settings.jg_policy_file.parent / "mcp.yaml"


def load_mcp_config() -> MCPConfig:
    return load_mcp_config_from_path(mcp_config_path())
