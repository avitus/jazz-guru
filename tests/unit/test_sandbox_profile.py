from __future__ import annotations

from pathlib import Path

import pytest

from jazz_guru.actions import sandbox_profile
from jazz_guru.config import get_settings


@pytest.fixture
def temp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point JG_DATA_DIR at a tmp dir and stash a stub profile in it."""
    d = tmp_path / "data"
    d.mkdir()
    (d / "sandbox").mkdir()
    (d / "sandbox" / "jg.sb").write_text("(version 1)\n(allow default)\n")
    monkeypatch.setattr(get_settings(), "jg_data_dir", d)
    return d


def test_passthrough_when_flag_off(temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "jg_os_sandbox", 0)
    argv = ["python", "-c", "print(1)"]
    assert sandbox_profile.wrap_subprocess(argv, temp_data_dir) == argv


def test_passthrough_when_not_darwin(
    temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "jg_os_sandbox", 1)
    monkeypatch.setattr(sandbox_profile.sys, "platform", "linux")
    argv = ["python", "-c", "print(1)"]
    assert sandbox_profile.wrap_subprocess(argv, temp_data_dir) == argv


def test_passthrough_when_profile_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # data dir exists but no jg.sb inside.
    d = tmp_path / "data_empty"
    d.mkdir()
    monkeypatch.setattr(get_settings(), "jg_data_dir", d)
    monkeypatch.setattr(get_settings(), "jg_os_sandbox", 1)
    monkeypatch.setattr(sandbox_profile.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox_profile.shutil, "which", lambda _: "/usr/bin/sandbox-exec")
    argv = ["python", "-c", "print(1)"]
    assert sandbox_profile.wrap_subprocess(argv, d) == argv


def test_prefixes_when_enabled_on_darwin(
    temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "jg_os_sandbox", 1)
    monkeypatch.setattr(sandbox_profile.sys, "platform", "darwin")
    # Pretend sandbox-exec is installed.
    monkeypatch.setattr(sandbox_profile.shutil, "which", lambda _: "/usr/bin/sandbox-exec")
    argv = ["python", "-c", "print(1)"]
    out = sandbox_profile.wrap_subprocess(argv, temp_data_dir)
    assert out[0] == "sandbox-exec"
    assert out[1] == "-f"
    assert out[2].endswith("jg.sb")
    # WORKSPACE / DATA params present
    assert any(s.startswith("WORKSPACE=") for s in out)
    assert any(s.startswith("DATA=") for s in out)
    # Original argv preserved at the tail.
    assert out[-3:] == argv


def test_passthrough_when_sandbox_exec_missing(
    temp_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "jg_os_sandbox", 1)
    monkeypatch.setattr(sandbox_profile.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox_profile.shutil, "which", lambda _: None)
    argv = ["python", "-c", "print(1)"]
    assert sandbox_profile.wrap_subprocess(argv, temp_data_dir) == argv
