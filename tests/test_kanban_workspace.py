"""Tests for scratch workspace lifecycle (Phase 2-A)."""
from __future__ import annotations

from pathlib import Path

from src.core.kanban.workspace import (
    cleanup_scratch_workspace,
    ensure_scratch_workspace,
)


def test_ensure_creates_dir(tmp_path: Path):
    p = ensure_scratch_workspace(tmp_path / "ws", "t_abc")
    assert p.exists()
    assert p.is_dir()


def test_ensure_idempotent_preserves_files(tmp_path: Path):
    p1 = ensure_scratch_workspace(tmp_path / "ws", "t_abc")
    (p1 / "f.txt").write_text("hi", encoding="utf-8")
    p2 = ensure_scratch_workspace(tmp_path / "ws", "t_abc")
    assert p1 == p2
    assert (p2 / "f.txt").read_text(encoding="utf-8") == "hi"


def test_cleanup_removes_dir(tmp_path: Path):
    p = ensure_scratch_workspace(tmp_path / "ws", "t_abc")
    (p / "f.txt").write_text("data", encoding="utf-8")
    ok = cleanup_scratch_workspace(tmp_path / "ws", "t_abc")
    assert ok is True
    assert not p.exists()


def test_cleanup_missing_returns_false(tmp_path: Path):
    ok = cleanup_scratch_workspace(tmp_path / "ws", "t_does-not-exist")
    assert ok is False


def test_cleanup_idempotent_after_remove(tmp_path: Path):
    ensure_scratch_workspace(tmp_path / "ws", "t_abc")
    assert cleanup_scratch_workspace(tmp_path / "ws", "t_abc") is True
    assert cleanup_scratch_workspace(tmp_path / "ws", "t_abc") is False


# ---- Phase 2-B: workspace=dir + parse_workspace_spec ----

import pytest

from src.core.kanban.workspace import (
    WorkspaceError,
    ensure_dir_workspace,
    materialize_workspace,
    parse_workspace_spec,
)


def test_parse_workspace_spec_scratch():
    assert parse_workspace_spec("scratch") == ("scratch", None)
    assert parse_workspace_spec("") == ("scratch", None)


def test_parse_workspace_spec_dir_absolute(tmp_path: Path):
    abs_path = str(tmp_path / "shared")
    kind, path = parse_workspace_spec(f"dir:{abs_path}")
    assert kind == "dir"
    assert path == abs_path


def test_parse_workspace_spec_dir_relative_rejected():
    with pytest.raises(WorkspaceError):
        parse_workspace_spec("dir:./relative")


def test_parse_workspace_spec_dir_empty_rejected():
    with pytest.raises(WorkspaceError):
        parse_workspace_spec("dir:")


def test_parse_workspace_spec_worktree_not_yet_supported():
    with pytest.raises(WorkspaceError):
        parse_workspace_spec("worktree")


def test_parse_workspace_spec_unknown_rejected():
    with pytest.raises(WorkspaceError):
        parse_workspace_spec("dropbox:foo")


def test_ensure_dir_workspace_creates(tmp_path: Path):
    target = tmp_path / "video-pipeline"
    out = ensure_dir_workspace(str(target))
    assert out.exists()
    assert out == target.resolve()


def test_ensure_dir_workspace_rejects_relative():
    with pytest.raises(WorkspaceError):
        ensure_dir_workspace("relative/path")


def test_materialize_workspace_scratch(tmp_path: Path):
    out = materialize_workspace(
        kind="scratch", workspace_path=None,
        scratch_root=tmp_path / "ws", task_id="t_abc",
    )
    assert "t_abc" in out
    assert Path(out).exists()


def test_materialize_workspace_dir(tmp_path: Path):
    target = tmp_path / "shared"
    out = materialize_workspace(
        kind="dir", workspace_path=str(target),
        scratch_root=tmp_path / "unused", task_id="t_abc",
    )
    assert Path(out) == target.resolve()
    # task_id is NOT appended for dir kind — shared dir is shared
    assert "t_abc" not in out


def test_materialize_workspace_dir_requires_path(tmp_path: Path):
    with pytest.raises(WorkspaceError):
        materialize_workspace(
            kind="dir", workspace_path=None,
            scratch_root=tmp_path, task_id="t_abc",
        )
