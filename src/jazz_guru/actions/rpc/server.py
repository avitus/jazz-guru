"""Per-python_exec RPC server.

Lifecycle:
* ``start()`` binds a fresh Unix domain socket under ``$TMPDIR`` and starts
  an asyncio server.
* The subprocess launched by ``python_exec`` reads ``JG_RPC_SOCK`` and
  ``JG_RPC_TOKEN`` from its env and uses :data:`RPC_PRELUDE_TEMPLATE` to
  obtain a ``tools`` proxy.
* Every RPC ``call`` request is dispatched through ``registry.invoke`` so
  policy / events / DynamicRegistry overlays apply uniformly.
* ``stop()`` closes the server and removes the socket. The token is unique
  per call so even a delayed stray connect can't replay against a later
  server.

The per-call cap (``DEFAULT_RPC_CALL_CAP``) protects against runaway loops.
The cap is a flat limit on RPC calls inside a single python_exec, *not* the
session-level tool budget; the LLM-driven tool_use that invoked python_exec
still counts as one against the per-turn budget.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import tempfile
from pathlib import Path
from typing import Any

from jazz_guru.logging import get_logger

log = get_logger(__name__)

DEFAULT_RPC_CALL_CAP = 256

# The subprocess prelude. ``__JG_SOCK__`` and ``__JG_TOKEN__`` are templated in
# at start() time. The proxy is plain stdlib so it works under ``python -I``
# (which strips PYTHONPATH and user site-packages).
RPC_PRELUDE_TEMPLATE = r'''
import json as _jg_json
import os as _jg_os
import socket as _jg_socket
import threading as _jg_threading
import sys as _jg_sys

class _JGToolError(RuntimeError):
    pass

class _JGProxy:
    """Synchronous client for the host tool registry, over a Unix socket.

    Each call is a fresh connect/send/recv cycle so the proxy is safe to call
    from multiple threads concurrently; the socket itself is single-shot.
    """
    def __init__(self, sock_path, token):
        self._sock_path = sock_path
        self._token = token
        self._id_lock = _jg_threading.Lock()
        self._next_id = 0

    def _next(self):
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def _call(self, method, params):
        rid = self._next()
        req = _jg_json.dumps({
            "id": rid, "token": self._token, "method": method, "params": params,
        }).encode("utf-8") + b"\n"
        s = _jg_socket.socket(_jg_socket.AF_UNIX, _jg_socket.SOCK_STREAM)
        try:
            s.connect(self._sock_path)
            s.sendall(req)
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if b"\n" in buf:
                    break
        finally:
            try:
                s.close()
            except Exception:
                pass
        if not buf:
            raise _JGToolError("no response from host RPC server")
        line, _, _ = buf.partition(b"\n")
        resp = _jg_json.loads(line.decode("utf-8"))
        if resp.get("error") is not None:
            raise _JGToolError(str(resp["error"]))
        return resp.get("result")

    def list_tools(self):
        return self._call("list_tools", {})

    def call(self, name, **kwargs):
        return self._call("call", {"name": name, "args": kwargs})

    def __getattr__(self, name):
        # Allow ``tools.render_midi(...)``-style ergonomic access.
        if name.startswith("_"):
            raise AttributeError(name)
        proxy = self
        def _invoke(**kwargs):
            return proxy._call("call", {"name": name, "args": kwargs})
        _invoke.__name__ = name
        return _invoke


_jg_sock = _jg_os.environ.get("JG_RPC_SOCK")
_jg_token = _jg_os.environ.get("JG_RPC_TOKEN")
if _jg_sock and _jg_token:
    tools = _JGProxy(_jg_sock, _jg_token)
else:  # pragma: no cover - back-compat for ephemeral callers without RPC
    class _Disabled:
        def __getattr__(self, name):
            raise _JGToolError("RPC tools not available (JG_RPC_SOCK not set)")
    tools = _Disabled()
'''


def build_rpc_prelude(sock_path: str, token: str, user_code: str) -> str:
    """Glue: prepend the RPC prelude to ``user_code`` for ``python -I -c``."""
    # The token and socket path are injected via env, not literal templating,
    # so the prelude is a constant string. Use a small bootstrap to assert env
    # vars are present (otherwise tools just raises on use).
    return RPC_PRELUDE_TEMPLATE + "\n# --- begin user source ---\n" + user_code


class ToolRPCServer:
    """Per-call Unix-socket server bridging back to ``registry.invoke``."""

    def __init__(
        self,
        registry: Any,
        allowed: set[str],
        on_event: Any = None,
        call_cap: int = DEFAULT_RPC_CALL_CAP,
    ) -> None:
        self.registry = registry
        self.allowed = set(allowed)
        self.on_event = on_event
        self.call_cap = call_cap
        self.token: str = ""
        self.sock_path: Path | None = None
        self._tmpdir: Path | None = None
        self._server: asyncio.AbstractServer | None = None
        self.call_count = 0
        self.errors: list[str] = []

    async def start(self) -> tuple[str, str]:
        """Bind the socket and return ``(socket_path, token)``."""
        self.token = secrets.token_hex(16)
        self._tmpdir = Path(tempfile.mkdtemp(prefix="jg-rpc-"))
        self.sock_path = self._tmpdir / "rpc.sock"
        # Restrict permissions on the directory; the socket inherits them.
        self._tmpdir.chmod(0o700)
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self.sock_path)
        )
        return str(self.sock_path), self.token

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        if self.sock_path is not None and self.sock_path.exists():
            with contextlib.suppress(OSError):
                self.sock_path.unlink()
        if self._tmpdir is not None and self._tmpdir.exists():
            # rmdir fails if non-empty -- in that case leave it; the OS will
            # GC the tempdir when the workspace is reaped.
            with contextlib.suppress(OSError):
                self._tmpdir.rmdir()

    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(name, payload)
        except Exception as e:
            log.warning("rpc.emit_failed", err=str(e))

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                try:
                    req = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    writer.write(json.dumps({"id": None, "error": "bad json"}).encode() + b"\n")
                    await writer.drain()
                    continue
                resp = await self._dispatch(req)
                writer.write(json.dumps(resp, default=str).encode("utf-8") + b"\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            return
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(self, req: dict[str, Any]) -> dict[str, Any]:
        rid = req.get("id")
        if not isinstance(req, dict):
            return {"id": rid, "error": "invalid request"}
        if req.get("token") != self.token:
            return {"id": rid, "error": "auth: bad token"}
        method = req.get("method")
        params = req.get("params") or {}

        if method == "list_tools":
            return {"id": rid, "result": sorted(self.allowed)}

        if method == "call":
            name = params.get("name")
            args = params.get("args") or {}
            if not isinstance(name, str):
                return {"id": rid, "error": "call: 'name' is required"}
            if name not in self.allowed:
                self.errors.append(f"tool {name!r} not allowed by policy")
                return {"id": rid, "error": f"tool {name!r} not allowed by policy"}
            if self.call_count >= self.call_cap:
                self.errors.append(f"rpc call cap {self.call_cap} exceeded")
                return {"id": rid, "error": f"rpc call cap {self.call_cap} exceeded"}
            self.call_count += 1
            self._emit("rpc_call", {"name": name, "args": args, "n": self.call_count})
            try:
                result = await self.registry.invoke(name, args if isinstance(args, dict) else {})
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                self.errors.append(err)
                self._emit(
                    "rpc_result", {"name": name, "ok": False, "error": err, "n": self.call_count}
                )
                return {"id": rid, "error": err}
            self._emit("rpc_result", {"name": name, "ok": True, "n": self.call_count})
            return {"id": rid, "result": result}

        return {"id": rid, "error": f"unknown method: {method!r}"}
