from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jazz_guru.actions.mcp import config as mcp_config
from jazz_guru.actions.mcp import manager as mcp_manager
from jazz_guru.actions.mcp import registry_bridge as rb
from jazz_guru.actions.mcp.config import MCPError, load_mcp_config_from_path
from jazz_guru.actions.mcp.manager import MCPManager, MCPServerSpec
from jazz_guru.actions.registry import register_all, registry


def _write_yaml(tmp: Path, body: str) -> Path:
    p = tmp / "mcp.yaml"
    p.write_text(body, encoding="utf-8")
    return p


# ---------- config parsing ---------------------------------------------------


def test_parse_empty_file_yields_empty_config(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path, "mcp_servers: {}\n")
    cfg = load_mcp_config_from_path(p)
    assert cfg.servers == []


def test_parse_missing_file_yields_empty_config(tmp_path: Path) -> None:
    cfg = load_mcp_config_from_path(tmp_path / "nonexistent.yaml")
    assert cfg.servers == []


def test_parse_stdio_server(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
mcp_servers:
  filesystem:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/scope"]
    env:
      FS_ROOT: "/scope"
    cwd: "/scope"
""",
    )
    cfg = load_mcp_config_from_path(p)
    assert len(cfg.servers) == 1
    s = cfg.servers[0]
    assert s.name == "filesystem"
    assert s.transport == "stdio"
    assert s.command == "npx"
    assert s.args == ["-y", "@modelcontextprotocol/server-filesystem", "/scope"]
    assert s.env == {"FS_ROOT": "/scope"}
    assert s.cwd == "/scope"


def test_parse_http_server(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
mcp_servers:
  remote:
    url: "https://example.com/mcp"
    headers:
      Authorization: "Bearer xxx"
""",
    )
    s = load_mcp_config_from_path(p).servers[0]
    assert s.transport == "http"
    assert s.url == "https://example.com/mcp"
    assert s.headers == {"Authorization": "Bearer xxx"}


def test_parse_tools_filters(tmp_path: Path) -> None:
    p = _write_yaml(
        tmp_path,
        """
mcp_servers:
  gh:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-github"]
    tools:
      include: [create_issue, list_issues]
      exclude: [delete_repo]
""",
    )
    s = load_mcp_config_from_path(p).servers[0]
    assert s.include_tools == ["create_issue", "list_issues"]
    assert s.exclude_tools == ["delete_repo"]


def test_parse_rejects_missing_transport(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path, "mcp_servers:\n  bad:\n    foo: bar\n")
    with pytest.raises(MCPError):
        load_mcp_config_from_path(p)


def test_parse_rejects_non_mapping_top_level(tmp_path: Path) -> None:
    p = _write_yaml(tmp_path, "- not a mapping\n")
    with pytest.raises(MCPError):
        load_mcp_config_from_path(p)


# ---------- bridge: stub client ---------------------------------------------


class _StubClient:
    """Mimics MCPClient just enough for the bridge + manager tests."""

    def __init__(self, spec: MCPServerSpec, tools: list[dict[str, Any]] | None = None) -> None:
        self.spec = spec
        self.tools = list(tools or [])
        self.started = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.start_attempts = 0

    async def start(self) -> None:
        self.start_attempts += 1
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        return {"text": f"{self.spec.name}.{name}({args})", "is_error": False}

    async def refresh_tools(self) -> list[dict[str, Any]]:
        return list(self.tools)


@pytest.fixture
def clean_registry():
    """Restore the global registry's _tools dict to its prior state."""
    register_all()
    before = dict(registry._tools)  # type: ignore[attr-defined]
    yield
    registry._tools = before  # type: ignore[attr-defined]


async def test_bridge_registers_namespaced_tools(clean_registry) -> None:
    spec = MCPServerSpec(name="fs", transport="stdio", command="x")
    client = _StubClient(
        spec,
        tools=[
            {"name": "read_file", "description": "read", "input_schema": {"type": "object"}},
            {"name": "list_dir", "description": "list", "input_schema": {"type": "object"}},
        ],
    )
    registered = await rb.bridge_server_to_registry(spec, client)
    assert sorted(registered) == ["mcp_fs_list_dir", "mcp_fs_read_file"]
    assert "mcp_fs_read_file" in registry._tools  # type: ignore[attr-defined]


async def test_bridge_invocation_proxies_to_client(clean_registry) -> None:
    spec = MCPServerSpec(name="fs", transport="stdio", command="x")
    client = _StubClient(
        spec,
        tools=[{"name": "read_file", "description": "read", "input_schema": {}}],
    )
    await rb.bridge_server_to_registry(spec, client)
    out = await registry.invoke("mcp_fs_read_file", {"path": "x.txt"})
    assert out["text"].startswith("fs.read_file")
    assert client.calls == [("read_file", {"path": "x.txt"})]


async def test_bridge_skips_collision_with_builtin(
    clean_registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = MCPServerSpec(name="builtin", transport="stdio", command="x")
    # Pre-register a name that the bridge would try to register too. We
    # masquerade as a non-MCP built-in by using empty tags (no "mcp").
    from jazz_guru.actions.registry import ToolSpec

    fake_name = "mcp_builtin_read_file"
    registry._tools[fake_name] = ToolSpec(  # type: ignore[attr-defined]
        name=fake_name,
        description="pre-existing",
        input_schema={"type": "object"},
        handler=lambda **kw: {"ok": True},
        tags=(),  # no "mcp" tag → looks like a built-in to the bridge
    )
    client = _StubClient(
        spec, tools=[{"name": "read_file", "description": "x", "input_schema": {}}]
    )
    registered = await rb.bridge_server_to_registry(spec, client)
    assert registered == []  # the collision was skipped


async def test_bridge_respects_include_filter(clean_registry) -> None:
    spec = MCPServerSpec(
        name="fs",
        transport="stdio",
        command="x",
        include_tools=["read_file"],
    )
    client = _StubClient(
        spec,
        tools=[
            {"name": "read_file", "description": "x", "input_schema": {}},
            {"name": "write_file", "description": "x", "input_schema": {}},
        ],
    )
    registered = await rb.bridge_server_to_registry(spec, client)
    assert registered == ["mcp_fs_read_file"]


async def test_bridge_respects_exclude_filter(clean_registry) -> None:
    spec = MCPServerSpec(
        name="fs",
        transport="stdio",
        command="x",
        exclude_tools=["delete_file"],
    )
    client = _StubClient(
        spec,
        tools=[
            {"name": "read_file", "description": "x", "input_schema": {}},
            {"name": "delete_file", "description": "x", "input_schema": {}},
        ],
    )
    registered = await rb.bridge_server_to_registry(spec, client)
    assert registered == ["mcp_fs_read_file"]


async def test_unbridge_removes_only_mcp_tools(clean_registry) -> None:
    spec = MCPServerSpec(name="fs", transport="stdio", command="x")
    client = _StubClient(
        spec, tools=[{"name": "read_file", "description": "x", "input_schema": {}}]
    )
    registered = await rb.bridge_server_to_registry(spec, client)
    assert "mcp_fs_read_file" in registry._tools  # type: ignore[attr-defined]
    await rb.unbridge_server_from_registry(spec.name, registered)
    assert "mcp_fs_read_file" not in registry._tools  # type: ignore[attr-defined]


# ---------- manager lifecycle (with stubbed client) -------------------------


async def test_manager_starts_servers_and_bridges(
    clean_registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec_a = MCPServerSpec(name="alpha", transport="stdio", command="x")
    spec_b = MCPServerSpec(name="beta", transport="stdio", command="y", enabled=False)

    created: dict[str, _StubClient] = {}

    def _factory(spec: MCPServerSpec) -> _StubClient:
        client = _StubClient(
            spec,
            tools=[{"name": "ping", "description": "ping", "input_schema": {}}],
        )
        created[spec.name] = client
        return client

    monkeypatch.setattr(mcp_manager, "MCPClient", _factory)

    cfg = mcp_config.MCPConfig(servers=[spec_a, spec_b])
    mgr = MCPManager(cfg)
    await mgr.start_all()

    assert mgr.states["alpha"].status == "running"
    # disabled server gets a distinct state, not "running"
    assert mgr.states["beta"].status == "disabled"
    assert "mcp_alpha_ping" in registry._tools  # type: ignore[attr-defined]
    await mgr.stop_all()
    assert "mcp_alpha_ping" not in registry._tools  # type: ignore[attr-defined]


async def test_manager_retries_on_failure(
    clean_registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three consecutive start failures end in a 'failed' state."""
    spec = MCPServerSpec(name="flaky", transport="stdio", command="x")

    class _Bad(_StubClient):
        async def start(self) -> None:
            self.start_attempts += 1
            raise RuntimeError("nope")

    monkeypatch.setattr(mcp_manager, "MCPClient", lambda s: _Bad(s))
    # No sleeps in the test path.
    sleeps: list[float] = []

    async def _no_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(mcp_manager.asyncio, "sleep", _no_sleep)

    mgr = MCPManager(mcp_config.MCPConfig(servers=[spec]), max_start_retries=3)
    await mgr.start_all()
    assert mgr.states["flaky"].status == "failed"
    assert "nope" in (mgr.states["flaky"].error or "")
    # 3 retries, 3 backoff sleeps
    assert len(sleeps) == 3


def test_manager_status_includes_all_servers(clean_registry) -> None:
    cfg = mcp_config.MCPConfig(
        servers=[
            MCPServerSpec(name="alpha", transport="stdio", command="x"),
            MCPServerSpec(name="beta", transport="http", url="https://x"),
        ]
    )
    mgr = MCPManager(cfg)
    status = mgr.status()
    names = sorted(s["name"] for s in status["servers"])
    assert names == ["alpha", "beta"]
    # Initial state is "stopped"
    assert all(s["status"] == "stopped" for s in status["servers"])
