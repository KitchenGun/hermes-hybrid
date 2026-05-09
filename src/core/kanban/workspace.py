"""Workspace lifecycle for Kanban tasks (Phase 2-A + 2-B).

Two kinds active in this branch:
  * ``scratch`` — fresh tmp dir under ``settings.kanban_workspaces_root/<task_id>/``,
    created on claim and removed on done/archive. Independent per task.
  * ``dir:<absolute-path>`` — shared persistent directory referenced by the task,
    created if missing, never removed by Kanban. Used for video pipelines,
    obsidian vaults, per-account folders, etc.

The ``worktree`` kind (git worktree per task) arrives in a later phase.
"""
from __future__ import annotations

import shutil
from pathlib import Path


_DIR_PREFIX = "dir:"


class WorkspaceError(ValueError):
    pass


def parse_workspace_spec(spec: str) -> tuple[str, str | None]:
    """Parse a ``--workspace`` CLI value into (kind, path|None).

    Examples:
      "scratch"           → ("scratch", None)
      "dir:/abs/path"     → ("dir", "/abs/path")
      "dir:relative"      → raises (Phase 2-B requires absolute path)
      "worktree"          → raises (not yet supported)
    """
    if not spec or spec == "scratch":
        return "scratch", None
    if spec.startswith(_DIR_PREFIX):
        raw = spec[len(_DIR_PREFIX):].strip()
        if not raw:
            raise WorkspaceError("dir: requires a path")
        path = Path(raw)
        if not path.is_absolute():
            raise WorkspaceError(
                f"dir: requires an ABSOLUTE path, got {raw!r}"
            )
        return "dir", str(path)
    if spec == "worktree":
        raise WorkspaceError("workspace=worktree not yet supported")
    raise WorkspaceError(
        f"unknown workspace spec {spec!r}; use 'scratch' or 'dir:<absolute-path>'"
    )


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


def ensure_dir_workspace(absolute_path: str) -> Path:
    """Materialize a shared persistent dir. Path MUST be absolute.

    Unlike scratch, this is a long-lived directory passed by the user
    (e.g. an Obsidian vault root or a video-pipeline project root). We
    create the directory tree if missing but never delete it.
    """
    path = Path(absolute_path)
    if not path.is_absolute():
        raise WorkspaceError(
            f"dir workspace requires absolute path, got {absolute_path!r}"
        )
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def materialize_workspace(
    *,
    kind: str,
    workspace_path: str | None,
    scratch_root: Path,
    task_id: str,
) -> str:
    """Resolve workspace based on kind. Returns absolute path string.

    For ``scratch``, allocates ``<scratch_root>/<task_id>``.
    For ``dir``, validates and materializes ``workspace_path``.
    """
    if kind == "scratch":
        return str(ensure_scratch_workspace(scratch_root, task_id))
    if kind == "dir":
        if not workspace_path:
            raise WorkspaceError("dir workspace requires workspace_path")
        return str(ensure_dir_workspace(workspace_path))
    raise WorkspaceError(f"unsupported workspace_kind {kind!r}")


__all__ = [
    "WorkspaceError",
    "cleanup_scratch_workspace",
    "ensure_dir_workspace",
    "ensure_scratch_workspace",
    "materialize_workspace",
    "parse_workspace_spec",
]
