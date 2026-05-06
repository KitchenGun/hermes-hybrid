"""Pydantic models for the Kanban store (Phase 2-A, Nous Hermes Agent 정렬)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


KanbanStatus = Literal[
    "triage",     # parking column for rough ideas — dispatcher ignores
    "todo",       # specced but parents not all done
    "ready",      # all parents done; dispatcher can claim
    "running",    # worker holds the claim
    "blocked",    # human input required
    "done",       # finished
    "archived",   # soft delete (hidden)
]

RunOutcome = Literal[
    "completed",
    "blocked",
    "crashed",
    "timed_out",
    "spawn_failed",
    "reclaimed",
]

WorkspaceKind = Literal["scratch", "worktree", "dir"]  # Phase 2-A: scratch only


class KanbanTask(BaseModel):
    id: str
    board_id: str = "default"
    title: str
    body: str = ""
    status: KanbanStatus
    assignee: str | None = None
    tenant: str | None = None
    priority: int = 0
    workspace_kind: WorkspaceKind = "scratch"
    workspace_path: str | None = None
    idempotency_key: str | None = None
    scheduled_at: str | None = None
    max_runtime_seconds: int | None = None
    created_at: str
    updated_at: str
    created_by: str = ""
    current_run_id: str | None = None
    spawn_failure_count: int = 0
    skills: list[str] = Field(default_factory=list)


class KanbanRun(BaseModel):
    id: str
    task_id: str
    started_at: str
    ended_at: str | None = None
    outcome: RunOutcome | None = None
    summary: str | None = None
    metadata: dict = Field(default_factory=dict)
    pid: int | None = None
    workspace_path: str | None = None
    claim_expires_at: str
    last_heartbeat_at: str | None = None
    error: str | None = None


class KanbanComment(BaseModel):
    id: int
    task_id: str
    author: str
    body: str
    created_at: str


class KanbanLink(BaseModel):
    parent_id: str
    child_id: str
    created_at: str


class KanbanEvent(BaseModel):
    id: int
    task_id: str
    kind: str
    payload: dict = Field(default_factory=dict)
    actor: str | None = None
    created_at: str


class KanbanBoard(BaseModel):
    id: str
    name: str
    icon: str | None = None
    description: str | None = None
    created_at: str
    archived_at: str | None = None


__all__ = [
    "KanbanBoard",
    "KanbanComment",
    "KanbanEvent",
    "KanbanLink",
    "KanbanRun",
    "KanbanStatus",
    "KanbanTask",
    "RunOutcome",
    "WorkspaceKind",
]
