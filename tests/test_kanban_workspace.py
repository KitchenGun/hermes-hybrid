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
