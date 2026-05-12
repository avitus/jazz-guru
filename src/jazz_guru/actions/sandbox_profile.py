"""Opt-in OS-level sandbox for shell / python_exec / dynamic-tool subprocesses.

When ``JG_OS_SANDBOX=1`` and the platform is macOS, every subprocess that
the agent can spawn is wrapped in ``sandbox-exec -f data/sandbox/jg.sb``.
The profile (see :func:`profile_path`) restricts filesystem writes to the
session workspace and reads to the workspace + ``data/`` + brew prefixes,
while leaving network access intact so ``pip``/``http``/``voyage`` still
work.

Off by default. Set ``JG_OS_SANDBOX=1`` in ``.env`` (or the environment)
to enable. Linux and other platforms are passthrough today; a
``bwrap``/``firejail`` variant can land later without changing call sites.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from jazz_guru.actions.sandbox import data_dir
from jazz_guru.config import get_settings


def profile_path() -> Path:
    """Return the path to the macOS sandbox profile shipped in repo."""
    return (data_dir() / "sandbox" / "jg.sb").resolve()


def _enabled() -> bool:
    s = get_settings()
    if not s.jg_os_sandbox:
        return False
    if sys.platform != "darwin":
        return False
    if shutil.which("sandbox-exec") is None:
        return False
    return profile_path().exists()


def wrap_subprocess(argv: list[str], cwd: Path) -> list[str]:
    """Prepend ``sandbox-exec`` to ``argv`` if the OS sandbox is enabled.

    ``cwd`` becomes the writable WORKSPACE root inside the profile. The
    DATA directive points at ``data/`` so the profile can grant read-only
    access there.

    Returns ``argv`` unchanged when the sandbox is off or unavailable, so
    callers can wire this through unconditionally.
    """
    if not _enabled():
        return argv
    workspace_root = str(cwd.resolve())
    data_root = str(data_dir())
    return [
        "sandbox-exec",
        "-f",
        str(profile_path()),
        "-D",
        f"WORKSPACE={workspace_root}",
        "-D",
        f"DATA={data_root}",
        *argv,
    ]
