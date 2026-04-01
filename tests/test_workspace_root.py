"""Workspace root resolution for logs and layered env (vs site-packages layout)."""

from __future__ import annotations

from pathlib import Path

import pytest

from utils.workspace_root import get_workspace_root


def test_get_workspace_root_when_cwd_is_repo() -> None:
    root = Path(__file__).resolve().parent.parent
    assert (root / "config" / "runtime.env").is_file()
    assert get_workspace_root() == root


def test_get_workspace_root_prefers_cwd_when_it_has_runtime_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "runtime.env").write_text("HFT_FOO=1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert get_workspace_root() == tmp_path.resolve()
