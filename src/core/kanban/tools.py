"""7개 ``kanban_*`` tool functions (Phase 2-A, Nous Hermes Agent 정렬).

Worker-side tools (gated by `$HERMES_KANBAN_TASK`):
  - kanban_show, kanban_complete, kanban_block, kanban_heartbeat

Orchestrator-side tools (also available to any worker, by Nous convention):
  - kanban_comment, kanban_create, kanban_link

Each function takes an explicit ``KanbanDB`` so tests can inject a fresh
store. The master CLI subprocess will wrap these as Claude tool schema
entries when the worker mode is detected.
"""
from __future__ import annotations

import os

from src.core.kanban.db import KanbanDB
from src.core.kanban.models import KanbanTask


class KanbanToolError(Exception):
    pass


def _resolve_task_id(explicit: str | None) -> str:
    tid = explicit or os.environ.get("HERMES_KANBAN_TASK")
    if not tid:
        raise KanbanToolError(
            "task_id required (or set HERMES_KANBAN_TASK in env)"
        )
    return tid


def _build_worker_context(
    task: KanbanTask,
    runs,
    parents,
    comments,
) -> str:
    lines = [f"# Task {task.id} — {task.title}"]
    lines.append(f"status: {task.status}  assignee: {task.assignee or '—'}")
    if task.tenant:
        lines.append(f"tenant: {task.tenant}")
    if task.body:
        lines.append("")
        lines.append("## Body")
        lines.append(task.body)
    if parents:
        lines.append("")
        lines.append("## Parent handoffs")
        for p in parents:
            lines.append(f"- [{p.id}] {p.title} ({p.status})")
    prior = [r for r in runs if r.ended_at]
    if prior:
        lines.append("")
        lines.append("## Prior attempts")
        for r in prior:
            lines.append(f"- run {r.id}: {r.outcome}")
            if r.summary:
                lines.append(f"  summary: {r.summary}")
            if r.error:
                lines.append(f"  error: {r.error}")
    if comments:
        lines.append("")
        lines.append("## Comments")
        for c in comments:
            lines.append(f"- [{c.created_at}] {c.author}: {c.body}")
    return "\n".join(lines)


# ---- worker-side tools --------------------------------------------------


async def kanban_show(db: KanbanDB, *, task_id: str | None = None) -> dict:
    """Read the current task plus prior attempts, parents, and comments."""
    tid = _resolve_task_id(task_id)
    task = await db.get_task(tid)
    if task is None:
        raise KanbanToolError(f"task {tid!r} not found")
    runs = await db.list_runs(tid)
    parents = await db.parents_of(tid)
    comments = await db.list_comments(tid)
    current_run = None
    if task.current_run_id:
        for r in runs:
            if r.id == task.current_run_id:
                current_run = r
                break
    return {
        "task": task.model_dump(mode="json"),
        "prior_runs": [r.model_dump(mode="json") for r in runs if r.ended_at],
        "current_run": current_run.model_dump(mode="json") if current_run else None,
        "parent_handoffs": [
            {"id": p.id, "title": p.title, "status": p.status,
             "assignee": p.assignee}
            for p in parents
        ],
        "comments": [c.model_dump(mode="json") for c in comments],
        "worker_context": _build_worker_context(task, runs, parents, comments),
    }


async def kanban_complete(
    db: KanbanDB,
    *,
    summary: str,
    metadata: dict | None = None,
    created_cards: list[str] | None = None,
    task_id: str | None = None,
) -> dict:
    """End the current run with outcome=completed and mark the task done.

    v0.13 hallucination recovery: if any ``created_cards`` id does not
    exist, log a ``hallucination_rejected`` event (permanent audit trail)
    and raise — never silently complete with phantom ids.
    """
    tid = _resolve_task_id(task_id)
    task = await db.get_task(tid)
    if task is None:
        raise KanbanToolError(f"task {tid!r} not found")
    if task.current_run_id is None:
        raise KanbanToolError(f"task {tid!r} has no active run")
    if created_cards:
        phantoms: list[str] = []
        for cid in created_cards:
            if await db.get_task(cid) is None:
                phantoms.append(cid)
        if phantoms:
            # v0.13 Tenacity: phantom-id rejection is a hallucination event.
            # The event log keeps a permanent record even after the run
            # is later completed by retry — auditors see the false claim.
            await db.record_event(
                tid, "hallucination_rejected",
                {"phantom_ids": phantoms,
                 "claimed_count": len(created_cards),
                 "summary": summary},
                actor="worker",
            )
            raise KanbanToolError(
                f"created_cards rejected — phantom ids: {phantoms!r}. "
                "Only list ids returned from a successful kanban_create."
            )
    merged_metadata: dict = dict(metadata or {})
    if created_cards:
        merged_metadata["created_cards"] = list(created_cards)
    await db.end_run(
        task.current_run_id,
        outcome="completed",
        summary=summary,
        metadata=merged_metadata,
    )
    await db.set_status(tid, "done", actor="worker")
    # v0.13 Tenacity: clean completion clears the retry budget.
    await db.reset_retry_count(tid)
    promoted: list[str] = []
    for child in await db.children_of(tid):
        if child.status != "todo":
            continue
        parents = await db.parents_of(child.id)
        if parents and all(p.status == "done" for p in parents):
            await db.set_status(child.id, "ready", actor="dispatcher")
            promoted.append(child.id)
    return {
        "task_id": tid,
        "status": "done",
        "promoted_children": promoted,
    }


async def kanban_block(
    db: KanbanDB,
    *,
    reason: str,
    task_id: str | None = None,
) -> dict:
    """End the current run with outcome=blocked. Reason becomes the audit note."""
    if not reason:
        raise KanbanToolError("reason required (one sentence)")
    tid = _resolve_task_id(task_id)
    task = await db.get_task(tid)
    if task is None:
        raise KanbanToolError(f"task {tid!r} not found")
    if task.current_run_id is None:
        raise KanbanToolError(f"task {tid!r} has no active run")
    await db.end_run(task.current_run_id, outcome="blocked", error=reason)
    await db.set_status(tid, "blocked", actor="worker", reason=reason)
    return {"task_id": tid, "status": "blocked", "reason": reason}


async def kanban_heartbeat(
    db: KanbanDB,
    *,
    ttl_seconds: int = 300,
    note: str = "",
    task_id: str | None = None,
) -> dict:
    """Refresh the claim TTL. Use for runs > 2 minutes; pure side-effect."""
    tid = _resolve_task_id(task_id)
    task = await db.get_task(tid)
    if task is None or task.current_run_id is None:
        raise KanbanToolError(f"task {tid!r} has no active run")
    ok = await db.heartbeat(
        task.current_run_id, ttl_seconds=ttl_seconds, note=note
    )
    return {
        "task_id": tid,
        "run_id": task.current_run_id,
        "extended": ok,
    }


# ---- orchestrator-side tools -------------------------------------------


async def kanban_comment(
    db: KanbanDB,
    *,
    task_id: str,
    body: str,
    author: str = "worker",
) -> dict:
    """Append a comment to any task — durable note for cross-agent context."""
    if not task_id:
        raise KanbanToolError("task_id required")
    if not body:
        raise KanbanToolError("body required")
    if await db.get_task(task_id) is None:
        raise KanbanToolError(f"task {task_id!r} not found")
    comment = await db.add_comment(task_id, author=author, body=body)
    return {"comment_id": comment.id, "task_id": task_id}


async def kanban_create(
    db: KanbanDB,
    *,
    title: str,
    assignee: str,
    body: str = "",
    parents: list[str] | None = None,
    priority: int = 0,
    tenant: str | None = None,
    idempotency_key: str | None = None,
    scheduled_at: str | None = None,
    max_runtime_seconds: int | None = None,
    created_by: str | None = None,
    skills: list[str] | None = None,
    board_id: str | None = None,
    workspace_kind: str = "scratch",
    workspace_path: str | None = None,
    max_retries: int = 3,
) -> dict:
    """Create a new task. Validates parents exist; auto-resolves tenant + board from env."""
    if not title:
        raise KanbanToolError("title required")
    if not assignee:
        raise KanbanToolError("assignee required")
    for pid in (parents or []):
        if await db.get_task(pid) is None:
            raise KanbanToolError(f"parent {pid!r} does not exist")
    eff_tenant = tenant if tenant is not None else os.environ.get("HERMES_TENANT")
    eff_board = (
        board_id if board_id is not None
        else os.environ.get("HERMES_KANBAN_BOARD") or "default"
    )
    has_parents = bool(parents)
    if scheduled_at:
        initial_status = "todo" if has_parents else "triage"
    else:
        initial_status = "todo" if has_parents else "ready"
    task = await db.create_task(
        title=title,
        assignee=assignee,
        body=body,
        status=initial_status,
        tenant=eff_tenant,
        priority=priority,
        scheduled_at=scheduled_at,
        max_runtime_seconds=max_runtime_seconds,
        created_by=created_by or "worker",
        idempotency_key=idempotency_key,
        parents=parents,
        skills=skills,
        board_id=eff_board,
        workspace_kind=workspace_kind,
        workspace_path=workspace_path,
        max_retries=max_retries,
    )
    return {
        "task_id": task.id,
        "status": task.status,
        "assignee": task.assignee,
        "skills": task.skills,
        "board_id": task.board_id,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "max_retries": task.max_retries,
    }


async def kanban_link(
    db: KanbanDB,
    *,
    parent_id: str,
    child_id: str,
) -> dict:
    """Add a parent→child dependency edge. Cycle attempts are rejected."""
    if not (parent_id and child_id):
        raise KanbanToolError("parent_id and child_id required")
    if await db.get_task(parent_id) is None:
        raise KanbanToolError(f"parent {parent_id!r} does not exist")
    if await db.get_task(child_id) is None:
        raise KanbanToolError(f"child {child_id!r} does not exist")
    ok = await db.add_link(parent_id=parent_id, child_id=child_id)
    if not ok:
        raise KanbanToolError("link rejected (cycle or duplicate)")
    return {"parent_id": parent_id, "child_id": child_id}


__all__ = [
    "KanbanToolError",
    "kanban_block",
    "kanban_comment",
    "kanban_complete",
    "kanban_create",
    "kanban_heartbeat",
    "kanban_link",
    "kanban_show",
]
