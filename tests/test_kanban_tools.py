"""Tests for the 7 ``kanban_*`` tool functions (Phase 2-A)."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core.kanban import KanbanDB
from src.core.kanban.tools import (
    KanbanToolError,
    kanban_block,
    kanban_comment,
    kanban_complete,
    kanban_create,
    kanban_heartbeat,
    kanban_link,
    kanban_show,
)


async def _make_db(tmp_path: Path) -> KanbanDB:
    db = KanbanDB(tmp_path / "k.db", workspaces_root=tmp_path / "ws")
    await db.migrate()
    return db


# ---- kanban_show -------------------------------------------------------


@pytest.mark.asyncio
async def test_show_with_explicit_task_id(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="hello", assignee="x", status="ready")
    out = await kanban_show(db, task_id=t.id)
    assert out["task"]["id"] == t.id
    assert out["task"]["title"] == "hello"
    assert "worker_context" in out
    assert "hello" in out["worker_context"]


@pytest.mark.asyncio
async def test_show_uses_env_var(tmp_path: Path, monkeypatch):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    monkeypatch.setenv("HERMES_KANBAN_TASK", t.id)
    out = await kanban_show(db)
    assert out["task"]["id"] == t.id


@pytest.mark.asyncio
async def test_show_without_task_id_raises(tmp_path: Path, monkeypatch):
    db = await _make_db(tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with pytest.raises(KanbanToolError):
        await kanban_show(db)


@pytest.mark.asyncio
async def test_show_unknown_task_raises(tmp_path: Path):
    db = await _make_db(tmp_path)
    with pytest.raises(KanbanToolError):
        await kanban_show(db, task_id="t_phantom")


@pytest.mark.asyncio
async def test_show_includes_parent_handoffs(tmp_path: Path):
    db = await _make_db(tmp_path)
    p = await db.create_task(title="parent", assignee="x", status="done")
    c = await db.create_task(
        title="child", assignee="y", status="ready", parents=[p.id]
    )
    out = await kanban_show(db, task_id=c.id)
    handoffs = out["parent_handoffs"]
    assert len(handoffs) == 1
    assert handoffs[0]["id"] == p.id
    assert handoffs[0]["status"] == "done"


# ---- kanban_complete ---------------------------------------------------


@pytest.mark.asyncio
async def test_complete_marks_done(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    await db.atomic_claim_one_ready(claim_ttl_seconds=60)
    out = await kanban_complete(
        db, task_id=t.id, summary="done", metadata={"k": 1}
    )
    assert out["status"] == "done"
    fetched = await db.get_task(t.id)
    assert fetched.status == "done"
    runs = await db.list_runs(t.id)
    assert runs[0].outcome == "completed"
    assert runs[0].summary == "done"
    assert runs[0].metadata == {"k": 1}


@pytest.mark.asyncio
async def test_complete_promotes_eligible_children(tmp_path: Path):
    db = await _make_db(tmp_path)
    p = await db.create_task(title="p", assignee="x", status="ready")
    c = await db.create_task(
        title="c", assignee="x", status="todo", parents=[p.id]
    )
    await db.atomic_claim_one_ready(claim_ttl_seconds=60)  # claim p
    out = await kanban_complete(db, task_id=p.id, summary="parent done")
    assert c.id in out["promoted_children"]
    fetched = await db.get_task(c.id)
    assert fetched.status == "ready"


@pytest.mark.asyncio
async def test_complete_phantom_card_id_rejected(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    await db.atomic_claim_one_ready(claim_ttl_seconds=60)
    with pytest.raises(KanbanToolError):
        await kanban_complete(
            db, task_id=t.id, summary="done",
            created_cards=["t_doesnotexist"],
        )


@pytest.mark.asyncio
async def test_complete_records_created_cards_in_metadata(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    sub = await db.create_task(title="sub", assignee="y", status="ready")
    await db.atomic_claim_one_ready(claim_ttl_seconds=60)
    await kanban_complete(
        db, task_id=t.id, summary="done", created_cards=[sub.id]
    )
    runs = await db.list_runs(t.id)
    assert sub.id in runs[0].metadata.get("created_cards", [])


@pytest.mark.asyncio
async def test_complete_without_active_run_raises(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    with pytest.raises(KanbanToolError):
        await kanban_complete(db, task_id=t.id, summary="done")


# ---- kanban_block ------------------------------------------------------


@pytest.mark.asyncio
async def test_block_marks_blocked_and_records_reason(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    await db.atomic_claim_one_ready(claim_ttl_seconds=60)
    out = await kanban_block(db, task_id=t.id, reason="need decision")
    assert out["status"] == "blocked"
    fetched = await db.get_task(t.id)
    assert fetched.status == "blocked"
    runs = await db.list_runs(t.id)
    assert runs[0].outcome == "blocked"
    assert runs[0].error == "need decision"


@pytest.mark.asyncio
async def test_block_without_reason_raises(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    await db.atomic_claim_one_ready(claim_ttl_seconds=60)
    with pytest.raises(KanbanToolError):
        await kanban_block(db, task_id=t.id, reason="")


# ---- kanban_heartbeat --------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_extends_run_ttl(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    claimed = await db.atomic_claim_one_ready(claim_ttl_seconds=60)
    out = await kanban_heartbeat(
        db, task_id=t.id, ttl_seconds=120, note="halfway"
    )
    assert out["extended"] is True
    run = await db.get_run(claimed.current_run_id)
    assert run.last_heartbeat_at is not None


# ---- kanban_comment ----------------------------------------------------


@pytest.mark.asyncio
async def test_comment_appends(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    out = await kanban_comment(db, task_id=t.id, body="hi", author="orch")
    assert out["task_id"] == t.id
    comments = await db.list_comments(t.id)
    assert len(comments) == 1
    assert comments[0].body == "hi"
    assert comments[0].author == "orch"


@pytest.mark.asyncio
async def test_comment_unknown_task_raises(tmp_path: Path):
    db = await _make_db(tmp_path)
    with pytest.raises(KanbanToolError):
        await kanban_comment(db, task_id="t_nope", body="hi")


# ---- kanban_create -----------------------------------------------------


@pytest.mark.asyncio
async def test_create_basic_lands_ready(tmp_path: Path):
    db = await _make_db(tmp_path)
    out = await kanban_create(db, title="new task", assignee="researcher")
    assert out["task_id"].startswith("t_")
    assert out["status"] == "ready"  # no parents → ready


@pytest.mark.asyncio
async def test_create_with_parents_lands_todo(tmp_path: Path):
    db = await _make_db(tmp_path)
    p = await db.create_task(title="p", assignee="x", status="ready")
    out = await kanban_create(db, title="c", assignee="y", parents=[p.id])
    assert out["status"] == "todo"


@pytest.mark.asyncio
async def test_create_phantom_parent_raises(tmp_path: Path):
    db = await _make_db(tmp_path)
    with pytest.raises(KanbanToolError):
        await kanban_create(
            db, title="x", assignee="y", parents=["t_phantom"]
        )


@pytest.mark.asyncio
async def test_create_with_scheduled_at_lands_triage(tmp_path: Path):
    db = await _make_db(tmp_path)
    out = await kanban_create(
        db, title="delayed", assignee="x",
        scheduled_at="2099-01-01T00:00:00+00:00",
    )
    assert out["status"] == "triage"


@pytest.mark.asyncio
async def test_create_idempotent(tmp_path: Path):
    db = await _make_db(tmp_path)
    a = await kanban_create(
        db, title="x", assignee="y", idempotency_key="job-1"
    )
    b = await kanban_create(
        db, title="z", assignee="w", idempotency_key="job-1"
    )
    assert a["task_id"] == b["task_id"]


@pytest.mark.asyncio
async def test_create_resolves_tenant_from_env(tmp_path: Path, monkeypatch):
    db = await _make_db(tmp_path)
    monkeypatch.setenv("HERMES_TENANT", "biz-a")
    out = await kanban_create(db, title="x", assignee="y")
    fetched = await db.get_task(out["task_id"])
    assert fetched.tenant == "biz-a"


# ---- kanban_link -------------------------------------------------------


@pytest.mark.asyncio
async def test_link_adds_dependency(tmp_path: Path):
    db = await _make_db(tmp_path)
    a = await db.create_task(title="a", assignee="x", status="ready")
    b = await db.create_task(title="b", assignee="x", status="ready")
    out = await kanban_link(db, parent_id=a.id, child_id=b.id)
    assert out["parent_id"] == a.id
    parents = await db.parents_of(b.id)
    assert [t.id for t in parents] == [a.id]


@pytest.mark.asyncio
async def test_link_rejects_cycle(tmp_path: Path):
    db = await _make_db(tmp_path)
    a = await db.create_task(title="a", assignee="x", status="ready")
    b = await db.create_task(
        title="b", assignee="x", status="todo", parents=[a.id]
    )
    with pytest.raises(KanbanToolError):
        await kanban_link(db, parent_id=b.id, child_id=a.id)


@pytest.mark.asyncio
async def test_link_rejects_phantom(tmp_path: Path):
    db = await _make_db(tmp_path)
    a = await db.create_task(title="a", assignee="x", status="ready")
    with pytest.raises(KanbanToolError):
        await kanban_link(db, parent_id=a.id, child_id="t_phantom")


# ---- v0.13 Tenacity: hallucination event + retry reset ----


@pytest.mark.asyncio
async def test_complete_phantom_logs_hallucination_event(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    await db.atomic_claim_one_ready(claim_ttl_seconds=60)
    with pytest.raises(KanbanToolError):
        await kanban_complete(
            db, task_id=t.id, summary="lying",
            created_cards=["t_doesnotexist"],
        )
    events = await db.list_events(t.id)
    kinds = [e.kind for e in events]
    assert "hallucination_rejected" in kinds


@pytest.mark.asyncio
async def test_complete_clears_retry_count(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(
        title="x", assignee="y", status="ready", max_retries=5,
    )
    await db.bump_retry_count(t.id)
    await db.bump_retry_count(t.id)  # retry_count = 2
    await db.atomic_claim_one_ready(claim_ttl_seconds=60)
    await kanban_complete(db, task_id=t.id, summary="done")
    fetched = await db.get_task(t.id)
    assert fetched.status == "done"
    assert fetched.retry_count == 0


@pytest.mark.asyncio
async def test_create_with_workspace_dir_persists(tmp_path: Path):
    db = await _make_db(tmp_path)
    target = tmp_path / "shared"
    out = await kanban_create(
        db, title="render", assignee="renderer",
        workspace_kind="dir", workspace_path=str(target),
    )
    assert out["workspace_kind"] == "dir"
    assert out["workspace_path"] == str(target)


@pytest.mark.asyncio
async def test_create_with_max_retries_persists(tmp_path: Path):
    db = await _make_db(tmp_path)
    out = await kanban_create(
        db, title="x", assignee="y", max_retries=10,
    )
    assert out["max_retries"] == 10
