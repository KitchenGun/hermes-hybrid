"""Tests for src/config.py — Settings field invariants.

Phase 16 (2026-05-07): added ``project_root`` so the Claude CLI subprocess
gets a stable cwd → ``.claude/settings.json`` allow patterns apply.
"""
from __future__ import annotations

from pathlib import Path

from src.config import Settings


def test_project_root_resolves_to_repo_root():
    """project_root must point at the repo root (where .claude/ lives)."""
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert isinstance(s.project_root, Path)
    # .claude/settings.json should exist under project_root — the whole
    # point of the field is that Claude CLI subprocesses use this as cwd
    # so they discover the per-repo settings file.
    settings_file = s.project_root / ".claude" / "settings.json"
    assert settings_file.exists(), (
        f"expected {settings_file} to exist — project_root resolution "
        "broken or settings.json missing"
    )


def test_project_root_overridable_via_env(monkeypatch, tmp_path):
    """PROJECT_ROOT env var overrides — needed for worktree-specific cwd."""
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.project_root == tmp_path
