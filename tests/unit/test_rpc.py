"""Tests for the tool-RPC pipeline (python_exec subprocess <-> host registry)."""
from __future__ import annotations

import asyncio
import json
import textwrap
from pathlib import Path

import pytest

from jazz_guru.actions.context import ToolContext, reset_tool_context, set_tool_context
from jazz_guru.actions.registry import register_all, registry
from jazz_guru.actions.rpc import ToolRPCServer, build_rpc_prelude
from jazz_guru.config import get_settings

# ---------- prelude / build helpers -----------------------------------------


def test_build_rpc_prelude_includes_proxy_and_user_code(tmp_path: Path) -> None:
    prelude = build_rpc_prelude(str(tmp_path / "sock"), "token", "x = 1\n")
    assert "_JGProxy" in prelude
    assert "begin user source" in prelude
    assert prelude.endswith("x = 1\n")


# ---------- server: in-process dispatch -------------------------------------


def _fake_registry(calls: list[tuple[str, dict]]):
    """Build a tiny registry-like object with predetermined return values."""
    class _R:
        async def invoke(self, name, args):
            calls.append((name, dict(args)))
            return {"echo": name, "args": args}

        def names(self):
            return ["echo_tool"]

    return _R()


async def test_server_lifecycle_creates_and_removes_socket(tmp_path: Path) -> None:
    s = ToolRPCServer(_fake_registry([]), allowed={"echo_tool"})
    sock, token = await s.start()
    assert Path(sock).exists()
    assert len(token) == 32  # hex-encoded 16 bytes
    await s.stop()
    assert not Path(sock).exists()


async def test_server_dispatch_call(tmp_path: Path) -> None:
    calls: list[tuple[str, dict]] = []
    s = ToolRPCServer(_fake_registry(calls), allowed={"echo_tool"})
    sock, token = await s.start()
    try:

        async def _client(method: str, params: dict) -> dict:
            reader, writer = await asyncio.open_unix_connection(path=sock)
            try:
                req = {"id": 1, "token": token, "method": method, "params": params}
                writer.write((json.dumps(req) + "\n").encode())
                await writer.drain()
                line = await reader.readline()
                return json.loads(line)
            finally:
                writer.close()
                await writer.wait_closed()

        # list_tools
        listing = await _client("list_tools", {})
        assert listing["result"] == ["echo_tool"]
        # call
        resp = await _client("call", {"name": "echo_tool", "args": {"x": 1}})
        assert resp["result"]["echo"] == "echo_tool"
        assert resp["result"]["args"] == {"x": 1}
        assert calls == [("echo_tool", {"x": 1})]
    finally:
        await s.stop()


async def test_server_rejects_bad_token(tmp_path: Path) -> None:
    s = ToolRPCServer(_fake_registry([]), allowed={"echo_tool"})
    sock, _ = await s.start()
    try:
        reader, writer = await asyncio.open_unix_connection(path=sock)
        req = {"id": 1, "token": "wrong", "method": "list_tools", "params": {}}
        writer.write((json.dumps(req) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
        resp = json.loads(line)
        assert "bad token" in resp["error"]
        writer.close()
        await writer.wait_closed()
    finally:
        await s.stop()


async def test_server_enforces_allowlist(tmp_path: Path) -> None:
    s = ToolRPCServer(_fake_registry([]), allowed={"echo_tool"})
    sock, token = await s.start()
    try:
        reader, writer = await asyncio.open_unix_connection(path=sock)
        req = {
            "id": 1,
            "token": token,
            "method": "call",
            "params": {"name": "not_allowed", "args": {}},
        }
        writer.write((json.dumps(req) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
        resp = json.loads(line)
        assert "not allowed" in resp["error"]
        writer.close()
        await writer.wait_closed()
    finally:
        await s.stop()


async def test_server_enforces_call_cap(tmp_path: Path) -> None:
    s = ToolRPCServer(_fake_registry([]), allowed={"echo_tool"}, call_cap=2)
    sock, token = await s.start()
    try:
        for i in range(3):
            reader, writer = await asyncio.open_unix_connection(path=sock)
            req = {
                "id": i,
                "token": token,
                "method": "call",
                "params": {"name": "echo_tool", "args": {"i": i}},
            }
            writer.write((json.dumps(req) + "\n").encode())
            await writer.drain()
            line = await reader.readline()
            resp = json.loads(line)
            if i < 2:
                assert "result" in resp
            else:
                assert "cap" in (resp.get("error") or "")
            writer.close()
            await writer.wait_closed()
    finally:
        await s.stop()


async def test_server_emits_events(tmp_path: Path) -> None:
    events: list[tuple[str, dict]] = []
    s = ToolRPCServer(
        _fake_registry([]),
        allowed={"echo_tool"},
        on_event=lambda n, p: events.append((n, p)),
    )
    sock, token = await s.start()
    try:
        reader, writer = await asyncio.open_unix_connection(path=sock)
        req = {
            "id": 1,
            "token": token,
            "method": "call",
            "params": {"name": "echo_tool", "args": {}},
        }
        writer.write((json.dumps(req) + "\n").encode())
        await writer.drain()
        await reader.readline()
        writer.close()
        await writer.wait_closed()
    finally:
        await s.stop()
    kinds = [n for n, _ in events]
    assert "rpc_call" in kinds
    assert "rpc_result" in kinds


# ---------- integration: real python subprocess ----------------------------


@pytest.fixture
def isolated_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "jg_workspace_dir", tmp_path)
    register_all()
    tok = set_tool_context(ToolContext(session_id="test", turn_idx=0))
    # Ensure the session workspace dir exists for python_exec's cwd resolution.
    (tmp_path / "sessions" / "test").mkdir(parents=True, exist_ok=True)
    yield tmp_path
    reset_tool_context(tok)


async def test_python_exec_subprocess_calls_registered_tool(
    isolated_session: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a python_exec script invokes ``tools.fs_write`` and the
    file actually shows up in the workspace."""
    code = textwrap.dedent(
        """
        out = tools.fs_write(path="rpc_made_this.txt", content="hello from RPC")
        print("WROTE:", out)
        listed = tools.list_tools()
        print("TOOLS_AVAILABLE_COUNT:", len(listed))
        """
    )
    out = await registry.invoke(
        "python_exec", {"code": code, "rpc_tools": True, "timeout_sec": 20}
    )
    assert out["exit_code"] == 0, out
    assert "WROTE:" in out["stdout"]
    # Tool calls counted: fs_write + list_tools (list_tools counts as 0 because
    # it doesn't go through allowed-set logic in our server) -- but the actual
    # call_count tracks only "call" requests. fs_write is one.
    assert out["rpc_calls"] >= 1
    # The file should now exist in the session workspace.
    assert (isolated_session / "sessions" / "test" / "rpc_made_this.txt").exists()


async def test_python_exec_subprocess_without_rpc(
    isolated_session: Path,
) -> None:
    """rpc_tools=false yields a plain ephemeral subprocess; `tools` raises."""
    out = await registry.invoke(
        "python_exec",
        {
            "code": "print('hi')",
            "rpc_tools": False,
            "timeout_sec": 10,
        },
    )
    assert out["exit_code"] == 0
    assert "hi" in out["stdout"]
    # rpc_calls is absent when rpc_tools=False
    assert "rpc_calls" not in out


async def test_python_exec_subprocess_rejects_disallowed_tool(
    isolated_session: Path,
) -> None:
    """The RPC server enforces the allowlist; a denied tool yields a
    _JGToolError that the script can catch."""
    code = textwrap.dedent(
        """
        try:
            tools.python_exec(code="print('inner')")
        except RuntimeError as e:
            print("DENIED:", str(e))
        """
    )
    out = await registry.invoke(
        "python_exec", {"code": code, "rpc_tools": True, "timeout_sec": 20}
    )
    assert out["exit_code"] == 0, out
    # python_exec is intentionally excluded from the RPC allowed-set to avoid
    # re-entrancy / budget confusion.
    assert "DENIED:" in out["stdout"]


async def test_python_exec_subprocess_emits_rpc_count(
    isolated_session: Path,
) -> None:
    """The python_exec result includes the number of RPC calls made."""
    code = textwrap.dedent(
        """
        for i in range(3):
            tools.fs_write(path=f"out_{i}.txt", content=f"line {i}")
        """
    )
    out = await registry.invoke(
        "python_exec", {"code": code, "rpc_tools": True, "timeout_sec": 20}
    )
    assert out["exit_code"] == 0
    assert out["rpc_calls"] == 3
