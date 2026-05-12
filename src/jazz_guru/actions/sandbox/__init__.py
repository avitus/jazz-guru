"""Sandbox primitives.

This package re-exports the workspace sandbox helpers from the original
``jazz_guru.actions.sandbox`` module and adds the optional persistent
execution backend used by ``python_exec`` with ``backend="persistent"``.
"""
from __future__ import annotations

from jazz_guru.actions.sandbox._impl import (
    data_dir,
    resolve_in_safe,
    resolve_in_workspace,
    safe_roots,
    session_workspace,
    workspace_root,
)
from jazz_guru.actions.sandbox.persistent import (
    PersistentPythonSession,
    attach_persistent,
    current_persistent,
    detach_persistent,
    get_or_create_session_repl,
    stop_all_persistent,
)

__all__ = [
    "PersistentPythonSession",
    "attach_persistent",
    "current_persistent",
    "data_dir",
    "detach_persistent",
    "get_or_create_session_repl",
    "resolve_in_safe",
    "resolve_in_workspace",
    "safe_roots",
    "session_workspace",
    "stop_all_persistent",
    "workspace_root",
]
