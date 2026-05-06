"""Kanban store — Phase 6 stub (memory/project_mode_system_deprecation.md
Phase 1 산출물).

Cross-profile hand-off channel. The motivating flow:

  advisor_ops weekly scan
    → identifies a tooling gap
    → kanban_create task (status=todo, tenant=advisor_ops)

  human review (Discord)
    → optional kanban_comment ("yes/no/details")

  installer_ops on-demand
    → kanban_list pending tasks
    → generates install plan
    → kanban_comment with the plan
    → kanban_complete when done

Storage: single JSON file at ``settings.kanban_store_path``. Atomic
write via tmp + replace. The file is tiny (one task ≈ 1 KB) and the
expected volume is < 100 tasks/year, so SQLite would be overkill. If
volume grows, swap the storage layer behind ``KanbanStore`` without
changing callers.

Why no schedule_at / due_at fields yet:
  Phase 1 is "review-and-then-act" — humans pace the work. Time fields
  arrive when we wire watchers to auto-promote ``review`` → ``in_progress``.

Why a tenant field instead of profile_id:
  The same Kanban store could host non-profile tasks later (system
  health, weekly checklist, etc.). ``tenant`` keeps the door open
  without growing a richer scope hierarchy.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


KanbanStatus = Literal[
    "triage",       # advisor_ops 가 갓 발행한 task — 사용자 검토 대기
    "todo",         # 사용자가 promote — 작업 큐
    "in_progress",  # 처리 중
    "review",       # installer 가 plan 첨부 — 사용자 승인 대기
    "done",
    "cancelled",
]


class KanbanComment(BaseModel):
    at: str
    author: str
    text: str


class KanbanTask(BaseModel):
    id: str
    tenant: str               # advisor_ops / installer_ops / system / ...
    title: str
    body: str = ""
    status: KanbanStatus = "todo"
    created_at: str
    updated_at: str
    created_by: str = ""      # profile_id or user_id
    assigned_to: str | None = None
    tags: list[str] = Field(default_factory=list)
    comments: list[KanbanComment] = Field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class KanbanStore:
    """File-backed Kanban CRUD. Thread-unsafe by design — Phase 1 is
    single-process. Concurrent writers are deferred to the SQLite
    backend swap if the volume ever justifies it."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---- low-level read/write -----------------------------------------

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "tasks": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            # Corrupt or unreadable: start fresh rather than blow up.
            # Caller can rotate the bad file out-of-band if recovery
            # matters; the JSON file is purely operational state.
            return {"version": 1, "tasks": []}
        if not isinstance(data, dict):
            return {"version": 1, "tasks": []}
        data.setdefault("version", 1)
        data.setdefault("tasks", [])
        return data

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    # ---- public CRUD --------------------------------------------------

    def create(
        self,
        *,
        tenant: str,
        title: str,
        body: str = "",
        created_by: str = "",
        tags: list[str] | None = None,
        assigned_to: str | None = None,
        status: KanbanStatus = "todo",
    ) -> KanbanTask:
        data = self._read()
        now = _now_iso()
        task = KanbanTask(
            id=str(uuid.uuid4()),
            tenant=tenant,
            title=title,
            body=body,
            status=status,
            created_at=now,
            updated_at=now,
            created_by=created_by,
            assigned_to=assigned_to,
            tags=list(tags or []),
        )
        data["tasks"].append(task.model_dump(mode="json"))
        self._write(data)
        return task

    def list(
        self,
        *,
        tenant: str | None = None,
        status: KanbanStatus | None = None,
        assigned_to: str | None = None,
    ) -> list[KanbanTask]:
        data = self._read()
        out: list[KanbanTask] = []
        for raw in data["tasks"]:
            try:
                task = KanbanTask(**raw)
            except (ValueError, TypeError):
                continue
            if tenant is not None and task.tenant != tenant:
                continue
            if status is not None and task.status != status:
                continue
            if assigned_to is not None and task.assigned_to != assigned_to:
                continue
            out.append(task)
        out.sort(key=lambda t: t.created_at)
        return out

    def get(self, task_id: str) -> KanbanTask | None:
        for task in self.list():
            if task.id == task_id:
                return task
        return None

    def comment(
        self, task_id: str, *, author: str, text: str
    ) -> KanbanTask | None:
        data = self._read()
        for raw in data["tasks"]:
            if raw.get("id") != task_id:
                continue
            comments = raw.setdefault("comments", [])
            comments.append(
                KanbanComment(
                    at=_now_iso(), author=author, text=text
                ).model_dump(mode="json")
            )
            raw["updated_at"] = _now_iso()
            self._write(data)
            return KanbanTask(**raw)
        return None

    def set_status(
        self, task_id: str, *, status: KanbanStatus
    ) -> KanbanTask | None:
        data = self._read()
        for raw in data["tasks"]:
            if raw.get("id") != task_id:
                continue
            raw["status"] = status
            raw["updated_at"] = _now_iso()
            self._write(data)
            return KanbanTask(**raw)
        return None

    def complete(self, task_id: str) -> KanbanTask | None:
        return self.set_status(task_id, status="done")

    def cancel(self, task_id: str) -> KanbanTask | None:
        return self.set_status(task_id, status="cancelled")


__all__ = [
    "KanbanComment",
    "KanbanStatus",
    "KanbanStore",
    "KanbanTask",
]
