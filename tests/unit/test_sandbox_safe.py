from __future__ import annotations

from pathlib import Path

import pytest

from jazz_guru.actions.sandbox import (
    resolve_in_safe,
    resolve_in_workspace,
    safe_roots,
)
from jazz_guru.config import get_settings


@pytest.fixture
def sandboxed(tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    workspace = Path(tmp_path) / "workspace"  # type: ignore[arg-type]
    data = Path(tmp_path) / "data"  # type: ignore[arg-type]
    extra = Path(tmp_path) / "extra"  # type: ignore[arg-type]
    workspace.mkdir()
    data.mkdir()
    extra.mkdir()
    monkeypatch.setattr(get_settings(), "jg_workspace_dir", workspace)
    monkeypatch.setattr(get_settings(), "jg_data_dir", data)
    monkeypatch.setattr(get_settings(), "jg_safe_extra_paths", [extra])
    return {"workspace": workspace, "data": data, "extra": extra, "root": Path(tmp_path)}  # type: ignore[arg-type]


def test_safe_roots_includes_workspace_data_extra(sandboxed: dict[str, Path]) -> None:
    sid = "abc"
    roots = safe_roots(sid)
    # session workspace
    assert any("sessions/abc" in str(r) for r in roots)
    # data dir
    assert any(str(sandboxed["data"]) in str(r) for r in roots)
    # extra
    assert any(str(sandboxed["extra"]) in str(r) for r in roots)


def test_resolve_in_safe_accepts_data(sandboxed: dict[str, Path]) -> None:
    f = sandboxed["data"] / "instruments.yaml"
    f.write_text("x")
    out = resolve_in_safe(str(f), "sid")
    assert out == f.resolve()


def test_resolve_in_safe_rejects_sibling_path(sandboxed: dict[str, Path]) -> None:
    outside = sandboxed["root"] / "other.txt"
    outside.write_text("x")
    with pytest.raises(PermissionError) as exc:
        resolve_in_safe(str(outside), "sid")
    assert "not under any safe root" in str(exc.value)


def test_resolve_in_safe_rejects_traversal(sandboxed: dict[str, Path]) -> None:
    # A relative .. that walks out of the workspace.
    with pytest.raises(PermissionError):
        resolve_in_safe("../../etc/passwd", "sid")


def test_resolve_in_workspace_still_session_only(sandboxed: dict[str, Path]) -> None:
    f = sandboxed["data"] / "instruments.yaml"
    f.write_text("x")
    # Reading data/ via the strict resolver is still denied.
    with pytest.raises(PermissionError):
        resolve_in_workspace(str(f), "sid")
