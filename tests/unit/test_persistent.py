"""Persistent Python REPL backend tests.

These spawn a real Python subprocess so they're slower than other unit tests
(~1-2 seconds for the whole module). Each test stops its REPL in teardown.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jazz_guru.actions.context import ToolContext, reset_tool_context, set_tool_context
from jazz_guru.actions.registry import register_all, registry
from jazz_guru.actions.sandbox import (
    PersistentPythonSession,
    get_or_create_session_repl,
    stop_all_persistent,
)
from jazz_guru.config import get_settings


@pytest.fixture
def isolated_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(get_settings(), "jg_workspace_dir", tmp_path)
    register_all()
    tok = set_tool_context(ToolContext(session_id="test-persistent", turn_idx=0))
    (tmp_path / "sessions" / "test-persistent").mkdir(parents=True, exist_ok=True)
    yield tmp_path
    reset_tool_context(tok)


@pytest.fixture(autouse=True)
async def _cleanup_persistent_repls():
    yield
    await stop_all_persistent()


# ---------- raw PersistentPythonSession --------------------------------------


async def test_persistent_session_round_trip(tmp_path: Path) -> None:
    s = PersistentPythonSession(session_id=None)
    await s.start()
    try:
        resp = await s.execute("print('hi')")
        assert resp["ok"] is True
        assert "hi" in resp["stdout"]
    finally:
        await s.stop()


async def test_persistent_session_keeps_state_across_calls(tmp_path: Path) -> None:
    s = PersistentPythonSession(session_id=None)
    await s.start()
    try:
        r1 = await s.execute("x = 41\nprint('set')")
        assert r1["ok"] is True
        r2 = await s.execute("print(x + 1)")
        assert r2["ok"] is True
        assert "42" in r2["stdout"]
    finally:
        await s.stop()


async def test_persistent_session_keeps_imports(tmp_path: Path) -> None:
    s = PersistentPythonSession(session_id=None)
    await s.start()
    try:
        await s.execute("import math\nprint('imported')")
        r = await s.execute("print(round(math.pi, 4))")
        assert r["ok"] is True
        assert "3.1416" in r["stdout"]
    finally:
        await s.stop()


async def test_persistent_session_surfaces_exception(tmp_path: Path) -> None:
    s = PersistentPythonSession(session_id=None)
    await s.start()
    try:
        r = await s.execute("raise ValueError('nope')")
        assert r["ok"] is False
        assert "ValueError" in r.get("error", "")
        assert "Traceback" in r["stderr"]
        # After an exception, the namespace should still be alive.
        r2 = await s.execute("print('alive')")
        assert r2["ok"] is True
        assert "alive" in r2["stdout"]
    finally:
        await s.stop()


async def test_persistent_session_timeout_restarts(tmp_path: Path) -> None:
    s = PersistentPythonSession(session_id=None)
    await s.start()
    try:
        r = await s.execute("import time; time.sleep(5)", timeout_sec=0.5)
        assert r["ok"] is False
        assert "timeout" in r["error"]
        # After restart, a fresh execute should work (but state is lost).
        r2 = await s.execute("print('fresh')")
        assert r2["ok"] is True
        assert "fresh" in r2["stdout"]
    finally:
        await s.stop()


# ---------- python_exec backend="persistent" --------------------------------


async def test_python_exec_persistent_preserves_state(isolated_session) -> None:
    out1 = await registry.invoke(
        "python_exec",
        {"code": "y = 100\nprint('set')", "backend": "persistent", "timeout_sec": 10},
    )
    assert out1["exit_code"] == 0
    assert out1["backend"] == "persistent"

    out2 = await registry.invoke(
        "python_exec",
        {"code": "print(y * 2)", "backend": "persistent", "timeout_sec": 10},
    )
    assert out2["exit_code"] == 0
    assert "200" in out2["stdout"]


async def test_python_exec_ephemeral_does_not_preserve_state(
    isolated_session,
) -> None:
    # First call: define a variable in an ephemeral subprocess.
    await registry.invoke(
        "python_exec",
        {"code": "z = 99\nprint('set')", "rpc_tools": False, "timeout_sec": 10},
    )
    # Second call (ephemeral): the variable shouldn't exist.
    out = await registry.invoke(
        "python_exec",
        {
            "code": "try:\n    print(z)\nexcept NameError as e:\n    print('NAMEERROR')",
            "rpc_tools": False,
            "timeout_sec": 10,
        },
    )
    assert "NAMEERROR" in out["stdout"]


async def test_python_exec_rejects_unknown_backend(isolated_session) -> None:
    out = await registry.invoke(
        "python_exec", {"code": "print('x')", "backend": "bogus"}
    )
    assert out["exit_code"] == -1
    assert "unknown backend" in out["stderr"]


async def test_python_exec_persistent_rejects_background(isolated_session) -> None:
    out = await registry.invoke(
        "python_exec",
        {"code": "pass", "backend": "persistent", "background": True},
    )
    assert out["exit_code"] == -1
    assert "not supported" in out["stderr"]


async def test_python_exec_persistent_per_session_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two different session_ids get separate REPLs."""
    monkeypatch.setattr(get_settings(), "jg_workspace_dir", tmp_path)
    register_all()
    (tmp_path / "sessions" / "sess-a").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sessions" / "sess-b").mkdir(parents=True, exist_ok=True)

    tok_a = set_tool_context(ToolContext(session_id="sess-a", turn_idx=0))
    try:
        await registry.invoke(
            "python_exec",
            {"code": "marker = 'A'\nprint('a')", "backend": "persistent"},
        )
    finally:
        reset_tool_context(tok_a)

    tok_b = set_tool_context(ToolContext(session_id="sess-b", turn_idx=0))
    try:
        out_b = await registry.invoke(
            "python_exec",
            {
                "code": (
                    "try:\n"
                    "    print(marker)\n"
                    "except NameError:\n"
                    "    print('SESSION_B_HAS_NO_MARKER')"
                ),
                "backend": "persistent",
            },
        )
    finally:
        reset_tool_context(tok_b)
    assert "SESSION_B_HAS_NO_MARKER" in out_b["stdout"]


# ---------- cache cleanup --------------------------------------------------


async def test_get_or_create_returns_same_instance(isolated_session) -> None:
    s1 = get_or_create_session_repl("test-persistent")
    s2 = get_or_create_session_repl("test-persistent")
    assert s1 is s2
    await stop_all_persistent()


async def test_stop_all_clears_cache(isolated_session) -> None:
    s = get_or_create_session_repl("test-persistent")
    await s.start()
    assert s.started
    await stop_all_persistent()
    # After stop_all, a new lookup yields a fresh instance.
    s2 = get_or_create_session_repl("test-persistent")
    assert s2 is not s
