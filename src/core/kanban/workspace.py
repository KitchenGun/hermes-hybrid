"""Scratch workspace lifecycle for Kanban tasks (Phase 2-A).

Each task gets a fresh dir under ``settings.kanban_workspaces_root/<task_id>/``.
Created on first claim, exposed to the worker via ``$HERMES_KANBAN_WORKSPACE``,
and removed when the task reaches ``done`` or ``archived``. ``worktree`` and
``dir:<path>`` kinds arrive in Phase 2-B.
"""
from __future__ import annotations

import shutil
from pathlib import Path


def ensure_scratch_workspace(root: Path, task_id: str) -> Path:
    """Create (or reuse) the scratch dir for a task and return its absolute path."""
    path = (Path(root) / task_id).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_scratch_workspace(root: Path, task_id: str) -> bool:
    """Remove the scratch dir. Returns True if removed, False if absent."""
    path = (Path(root) / task_id).resolve()
    if not path.exists():
        return False
    shutil.rmtree(path, ignore_errors=True)
    return not path.exists()


__all__ = ["cleanup_scratch_workspace", "ensure_scratch_workspace"]
