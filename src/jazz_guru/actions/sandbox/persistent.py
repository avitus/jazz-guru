"""Persistent Python REPL sandbox: one long-running subprocess per session.

The point: ``python_exec`` is ephemeral by default, so a script that runs
``import music21`` pays the import cost every call. With ``backend="persistent"``
the call routes to a per-session REPL that keeps cwd, imports, and any module-
or instance-level state alive across calls.

Wire-protocol is line-delimited JSON. Each request:

    {"code": "<source>"}

Each response:

    {"ok": bool, "stdout": "...", "stderr": "...", "error": "...?"}

The REPL prints exactly one newline-terminated JSON object per request, then
flushes. A sentinel exit code is not used; ``stop()`` closes stdin and the
subprocess exits cleanly.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import textwrap
import uuid as uuid_mod
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

from jazz_guru.actions.sandbox._impl import session_workspace
from jazz_guru.logging import get_logger

log = get_logger(__name__)


# REPL server source. Embedded as a string so it can be passed as -c.
_REPL_SOURCE = textwrap.dedent(
    """
    import sys, json, io, traceback, contextlib

    _ns = {"__name__": "__main__", "__builtins__": __builtins__}
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.rstrip("\\n")
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            sys.stdout.write(json.dumps({"ok": False, "error": "bad json: " + str(e), "stdout": "", "stderr": ""}) + "\\n")
            sys.stdout.flush()
            continue
        code = req.get("code", "")
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        ok = True
        err_msg = None
        try:
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                exec(compile(code, "<jg-persistent>", "exec"), _ns)
        except SystemExit:
            err_msg = "SystemExit"
            ok = False
        except BaseException as e:
            ok = False
            err_msg = type(e).__name__ + ": " + str(e)
            err_buf.write(traceback.format_exc())
        try:
            payload = {"ok": ok, "stdout": out_buf.getvalue(), "stderr": err_buf.getvalue()}
            if err_msg is not None:
                payload["error"] = err_msg
            sys.stdout.write(json.dumps(payload) + "\\n")
        except Exception as ee:
            sys.stdout.write(json.dumps({"ok": False, "stdout": "", "stderr": "", "error": "encode failed: " + str(ee)}) + "\\n")
        sys.stdout.flush()
    """
).strip()


class PersistentPythonSession:
    """One long-lived Python REPL subprocess per session."""

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._started = False
        self.id = uuid_mod.uuid4().hex[:8]

    @property
    def started(self) -> bool:
        return self._started and self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        if self.started:
            return
        # Clear any reference to a previous (dead) subprocess so the new one
        # owns the slot cleanly.
        self._proc = None
        cwd = session_workspace(self.session_id)
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",  # unbuffered stdout/stderr
            "-c",
            _REPL_SOURCE,
            cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._started = True

    async def stop(self) -> None:
        if self._proc is None:
            return
        with contextlib.suppress(Exception):
            if self._proc.stdin and not self._proc.stdin.is_closing():
                self._proc.stdin.close()
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=2.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()
            with contextlib.suppress(Exception):
                await self._proc.wait()
        self._proc = None
        self._started = False

    async def execute(self, code: str, timeout_sec: float = 30.0) -> dict[str, Any]:
        """Run ``code`` in the persistent namespace. Returns the parsed JSON response."""
        if not self.started:
            await self.start()
        assert self._proc is not None
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None

        payload = json.dumps({"code": code}).encode("utf-8") + b"\n"
        async with self._lock:  # only one in-flight call per session REPL
            try:
                self._proc.stdin.write(payload)
                await self._proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as e:
                # REPL died; reset state so the next call starts fresh.
                await self.stop()
                return {"ok": False, "error": f"REPL terminated: {e}", "stdout": "", "stderr": ""}
            try:
                line = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=timeout_sec
                )
            except TimeoutError:
                # The REPL is wedged on this call -- restart it so the session
                # is usable again. State is lost, but a hung subprocess is worse.
                await self.stop()
                return {
                    "ok": False,
                    "error": f"timeout after {timeout_sec}s; session was restarted",
                    "stdout": "",
                    "stderr": "",
                }
            if not line:
                # EOF: REPL exited mid-call (segfault, OOM, etc.)
                await self.stop()
                return {
                    "ok": False,
                    "error": "REPL exited unexpectedly",
                    "stdout": "",
                    "stderr": "",
                }
            try:
                return json.loads(line)
            except json.JSONDecodeError as e:
                return {
                    "ok": False,
                    "error": f"bad REPL response: {e}",
                    "stdout": line.decode("utf-8", errors="replace"),
                    "stderr": "",
                }

    async def workspace(self) -> Path:
        return session_workspace(self.session_id)


_PERSISTENT_OVERLAY: ContextVar[PersistentPythonSession | None] = ContextVar(
    "jg_persistent_python", default=None
)


def attach_persistent(s: PersistentPythonSession) -> Token[PersistentPythonSession | None]:
    return _PERSISTENT_OVERLAY.set(s)


def detach_persistent(token: Token[PersistentPythonSession | None] | None = None) -> None:
    if token is None:
        _PERSISTENT_OVERLAY.set(None)
    else:
        _PERSISTENT_OVERLAY.reset(token)


def current_persistent() -> PersistentPythonSession | None:
    return _PERSISTENT_OVERLAY.get()


# Lightweight process-wide cache of REPLs keyed by session_id. Lifetime is
# the process; ``stop_all_persistent`` can be called at shutdown to clean up.
_SESSION_REPLS: dict[str, PersistentPythonSession] = {}


def get_or_create_session_repl(session_id: str | None) -> PersistentPythonSession:
    """Return the cached REPL for this session, lazy-creating one if missing.

    The returned instance manages its own subprocess lifecycle: ``execute``
    will call ``start()`` on first use, and if the subprocess dies the next
    ``execute`` re-spawns. So callers always get the same wrapper object.
    """
    key = session_id or "__scratch__"
    if key not in _SESSION_REPLS:
        _SESSION_REPLS[key] = PersistentPythonSession(session_id)
    return _SESSION_REPLS[key]


async def stop_all_persistent() -> None:
    """Stop every cached REPL. Safe to call multiple times."""
    items = list(_SESSION_REPLS.items())
    _SESSION_REPLS.clear()
    for _key, repl in items:
        with contextlib.suppress(Exception):
            await repl.stop()
