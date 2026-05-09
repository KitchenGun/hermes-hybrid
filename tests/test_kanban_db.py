"""Tests for the SQLite-backed Kanban store (Phase 2-A, Nous 정렬).

Locks down:
  * schema migration is idempotent
  * task CRUD round-trip
  * filter combinations (status / assignee / tenant)
  * idempotency key returns existing task
  * parents/children queries + cycle rejection
  * atomic_claim_one_ready picks highest priority + creates run + workspace
  * status changes recorded in task_events
  * heartbeat extends claim TTL
  * scheduled_at filter respects status whitelist (triage/todo only)
  * spawn failure counter increments and resets
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest

from src.core.kanban import KanbanDB


async def _make_db(tmp_path: Path) -> KanbanDB:
    inst = KanbanDB(tmp_path / "k.db", workspaces_root=tmp_path / "ws")
    await inst.migrate()
    return inst


@pytest.mark.asyncio
async def test_migrate_is_idempotent(tmp_path: Path):
    inst = KanbanDB(tmp_path / "k.db", workspaces_root=tmp_path / "ws")
    await inst.migrate()
    await inst.migrate()  # second call shouldn't fail
    async with aiosqlite.connect(inst.db_path) as conn:
        async with conn.execute("SELECT id FROM boards") as cur:
            rows = await cur.fetchall()
    assert ("default",) in [tuple(r) for r in rows]


@pytest.mark.asyncio
async def test_create_task_persists_round_trip(tmp_path: Path):
    db = await _make_db(tmp_path)
    task = await db.create_task(title="t", assignee="devops", status="ready")
    assert task.id.startswith("t_")
    assert task.status == "ready"
    assert task.board_id == "default"
    assert task.spawn_failure_count == 0

    fetched = await db.get_task(task.id)
    assert fetched is not None
    assert fetched.title == "t"
    assert fetched.assignee == "devops"


@pytest.mark.asyncio
async def test_list_filters_compose(tmp_path: Path):
    db = await _make_db(tmp_path)
    await db.create_task(title="a", assignee="dev1", tenant="ten1", status="ready")
    await db.create_task(title="b", assignee="dev2", tenant="ten1", status="todo")
    await db.create_task(title="c", assignee="dev1", tenant="ten2", status="ready")

    by_assignee = await db.list_tasks(assignee="dev1")
    assert {t.title for t in by_assignee} == {"a", "c"}

    by_status = await db.list_tasks(status="ready")
    assert {t.title for t in by_status} == {"a", "c"}

    by_tenant = await db.list_tasks(tenant="ten1")
    assert {t.title for t in by_tenant} == {"a", "b"}

    composed = await db.list_tasks(tenant="ten1", status="ready")
    assert [t.title for t in composed] == ["a"]


@pytest.mark.asyncio
async def test_list_excludes_archived_by_default(tmp_path: Path):
    db = await _make_db(tmp_path)
    a = await db.create_task(title="active", assignee="x", status="ready")
    b = await db.create_task(title="archived", assignee="x", status="ready")
    await db.set_status(b.id, "archived")

    visible = await db.list_tasks()
    titles = {t.title for t in visible}
    assert "active" in titles
    assert "archived" not in titles

    all_tasks = await db.list_tasks(include_archived=True)
    titles = {t.title for t in all_tasks}
    assert "archived" in titles


@pytest.mark.asyncio
async def test_idempotency_returns_existing(tmp_path: Path):
    db = await _make_db(tmp_path)
    a = await db.create_task(
        title="x", assignee="y", idempotency_key="job-1", status="ready"
    )
    b = await db.create_task(
        title="y-different", assignee="z", idempotency_key="job-1", status="ready"
    )
    assert b.id == a.id
    assert b.title == "x"  # not the second call's title


@pytest.mark.asyncio
async def test_idempotency_allows_new_after_done(tmp_path: Path):
    db = await _make_db(tmp_path)
    a = await db.create_task(
        title="x", assignee="y", idempotency_key="job-1", status="ready"
    )
    await db.set_status(a.id, "done")
    b = await db.create_task(
        title="y", assignee="z", idempotency_key="job-1", status="ready"
    )
    assert b.id != a.id  # done task doesn't block re-issue


@pytest.mark.asyncio
async def test_parents_and_children_queries(tmp_path: Path):
    db = await _make_db(tmp_path)
    p = await db.create_task(title="parent", assignee="x", status="ready")
    c = await db.create_task(
        title="child", assignee="x", status="todo", parents=[p.id]
    )

    parents = await db.parents_of(c.id)
    assert [t.id for t in parents] == [p.id]

    children = await db.children_of(p.id)
    assert [t.id for t in children] == [c.id]


@pytest.mark.asyncio
async def test_add_link_rejects_cycle(tmp_path: Path):
    db = await _make_db(tmp_path)
    a = await db.create_task(title="a", assignee="x", status="ready")
    b = await db.create_task(
        title="b", assignee="x", status="todo", parents=[a.id]
    )
    # Adding b → a would close the cycle (a → b already exists).
    ok = await db.add_link(parent_id=b.id, child_id=a.id)
    assert ok is False


@pytest.mark.asyncio
async def test_add_link_rejects_self_loop(tmp_path: Path):
    db = await _make_db(tmp_path)
    a = await db.create_task(title="a", assignee="x", status="ready")
    assert await db.add_link(parent_id=a.id, child_id=a.id) is False


@pytest.mark.asyncio
async def test_add_link_allows_dag_edges(tmp_path: Path):
    db = await _make_db(tmp_path)
    a = await db.create_task(title="a", assignee="x", status="ready")
    b = await db.create_task(title="b", assignee="x", status="ready")
    c = await db.create_task(title="c", assignee="x", status="todo")

    assert await db.add_link(parent_id=a.id, child_id=c.id) is True
    assert await db.add_link(parent_id=b.id, child_id=c.id) is True

    parents = await db.parents_of(c.id)
    assert {t.id for t in parents} == {a.id, b.id}


@pytest.mark.asyncio
async def test_atomic_claim_picks_highest_priority(tmp_path: Path):
    db = await _make_db(tmp_path)
    await db.create_task(title="low", assignee="x", priority=1, status="ready")
    high = await db.create_task(
        title="high", assignee="x", priority=5, status="ready"
    )
    await db.create_task(title="mid", assignee="x", priority=3, status="ready")

    claimed = await db.atomic_claim_one_ready(claim_ttl_seconds=60)
    assert claimed is not None
    assert claimed.id == high.id
    assert claimed.status == "running"
    assert claimed.current_run_id and claimed.current_run_id.startswith("r_")


@pytest.mark.asyncio
async def test_atomic_claim_returns_none_when_no_ready(tmp_path: Path):
    db = await _make_db(tmp_path)
    await db.create_task(title="x", assignee="y", status="todo")
    assert await db.atomic_claim_one_ready(claim_ttl_seconds=60) is None


@pytest.mark.asyncio
async def test_atomic_claim_creates_run_and_workspace(tmp_path: Path):
    db = await _make_db(tmp_path)
    task = await db.create_task(title="x", assignee="y", status="ready")
    claimed = await db.atomic_claim_one_ready(claim_ttl_seconds=60)
    assert claimed is not None

    runs = await db.list_runs(task.id)
    assert len(runs) == 1
    assert runs[0].pid is None  # not yet attached
    ws_path = Path(runs[0].workspace_path)
    assert ws_path.exists()


@pytest.mark.asyncio
async def test_set_status_records_event(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    await db.set_status(t.id, "blocked", actor="human", reason="manual")

    fetched = await db.get_task(t.id)
    assert fetched.status == "blocked"

    events = await db.list_events(t.id)
    kinds = [e.kind for e in events]
    assert "status_changed" in kinds


@pytest.mark.asyncio
async def test_comment_appends_and_lists_in_order(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    await db.add_comment(t.id, author="kang", body="approve")
    await db.add_comment(t.id, author="kang", body="another")

    comments = await db.list_comments(t.id)
    assert [c.body for c in comments] == ["approve", "another"]


@pytest.mark.asyncio
async def test_end_run_records_outcome_and_metadata(tmp_path: Path):
    db = await _make_db(tmp_path)
    await db.create_task(title="x", assignee="y", status="ready")
    claimed = await db.atomic_claim_one_ready(claim_ttl_seconds=60)
    run_id = claimed.current_run_id

    await db.end_run(
        run_id, outcome="completed", summary="done", metadata={"k": 1}
    )
    run = await db.get_run(run_id)
    assert run.outcome == "completed"
    assert run.metadata == {"k": 1}
    assert run.summary == "done"
    assert run.ended_at is not None


@pytest.mark.asyncio
async def test_heartbeat_extends_ttl(tmp_path: Path):
    db = await _make_db(tmp_path)
    await db.create_task(title="x", assignee="y", status="ready")
    claimed = await db.atomic_claim_one_ready(claim_ttl_seconds=60)
    run_before = await db.get_run(claimed.current_run_id)
    ttl_before = run_before.claim_expires_at

    await asyncio.sleep(1.1)  # ensure ISO seconds tick
    ok = await db.heartbeat(
        claimed.current_run_id, ttl_seconds=120, note="halfway"
    )
    assert ok is True

    run_after = await db.get_run(claimed.current_run_id)
    assert run_after.claim_expires_at > ttl_before
    assert run_after.last_heartbeat_at is not None


@pytest.mark.asyncio
async def test_list_due_filters_by_scheduled_at(tmp_path: Path):
    db = await _make_db(tmp_path)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(
        timespec="seconds"
    )
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(
        timespec="seconds"
    )
    await db.create_task(title="past", assignee="x", status="todo", scheduled_at=past)
    await db.create_task(title="future", assignee="x", status="todo", scheduled_at=future)
    await db.create_task(title="now-no-sched", assignee="x", status="todo")

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    due = await db.list_tasks_due(now_iso)
    assert [t.title for t in due] == ["past"]


@pytest.mark.asyncio
async def test_list_due_skips_running_status(tmp_path: Path):
    db = await _make_db(tmp_path)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(
        timespec="seconds"
    )
    await db.create_task(
        title="past-running", assignee="x", status="running", scheduled_at=past
    )
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    assert await db.list_tasks_due(now_iso) == []


@pytest.mark.asyncio
async def test_bump_spawn_failure_increments(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    assert await db.bump_spawn_failure(t.id) == 1
    assert await db.bump_spawn_failure(t.id) == 2

    fetched = await db.get_task(t.id)
    assert fetched.spawn_failure_count == 2

    await db.reset_spawn_failure(t.id)
    fetched = await db.get_task(t.id)
    assert fetched.spawn_failure_count == 0


@pytest.mark.asyncio
async def test_set_assignee_updates_and_records_event(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="dev1", status="ready")
    updated = await db.set_assignee(t.id, "dev2")
    assert updated.assignee == "dev2"
    events = await db.list_events(t.id)
    kinds = [e.kind for e in events]
    assert "assigned" in kinds


@pytest.mark.asyncio
async def test_set_assignee_to_none_unassigns(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="dev1", status="ready")
    updated = await db.set_assignee(t.id, None)
    assert updated.assignee is None


@pytest.mark.asyncio
async def test_remove_link_returns_true_and_records_event(tmp_path: Path):
    db = await _make_db(tmp_path)
    a = await db.create_task(title="a", assignee="x", status="ready")
    b = await db.create_task(title="b", assignee="x", status="ready")
    await db.add_link(parent_id=a.id, child_id=b.id)
    removed = await db.remove_link(parent_id=a.id, child_id=b.id)
    assert removed is True
    parents = await db.parents_of(b.id)
    assert parents == []
    events = await db.list_events(b.id)
    assert "unlinked" in [e.kind for e in events]


@pytest.mark.asyncio
async def test_remove_link_returns_false_when_absent(tmp_path: Path):
    db = await _make_db(tmp_path)
    a = await db.create_task(title="a", assignee="x", status="ready")
    b = await db.create_task(title="b", assignee="x", status="ready")
    removed = await db.remove_link(parent_id=a.id, child_id=b.id)
    assert removed is False


# ---- boards (multi-board, Phase 2-A) ----------------------------------


@pytest.mark.asyncio
async def test_create_board_persists(tmp_path: Path):
    db = await _make_db(tmp_path)
    b = await db.create_board("alpha", name="Alpha", icon="🚀")
    assert b.id == "alpha"
    assert b.name == "Alpha"
    fetched = await db.get_board("alpha")
    assert fetched is not None and fetched.icon == "🚀"


@pytest.mark.asyncio
async def test_create_board_rejects_invalid_slug(tmp_path: Path):
    from src.core.kanban import InvalidSlugError
    db = await _make_db(tmp_path)
    for bad in ["with space", "../escape", "UPPERCASE-OK-via-normalize",
                "with/slash", "."]:
        if bad == "UPPERCASE-OK-via-normalize":
            # normalize_board_slug lowercases; the actual slug value used
            # is the lowercased form. Accepted.
            await db.create_board(bad)
            continue
        with pytest.raises((InvalidSlugError, ValueError)):
            await db.create_board(bad)


@pytest.mark.asyncio
async def test_create_board_rejects_duplicate(tmp_path: Path):
    db = await _make_db(tmp_path)
    await db.create_board("a")
    with pytest.raises(ValueError):
        await db.create_board("a")


@pytest.mark.asyncio
async def test_list_boards_excludes_archived_by_default(tmp_path: Path):
    db = await _make_db(tmp_path)
    await db.create_board("active")
    await db.create_board("dead")
    await db.archive_board("dead")
    visible = await db.list_boards()
    slugs = {b.id for b in visible}
    assert "active" in slugs
    assert "dead" not in slugs
    assert "default" in slugs  # auto-created on migrate
    all_boards = await db.list_boards(include_archived=True)
    assert "dead" in {b.id for b in all_boards}


@pytest.mark.asyncio
async def test_archive_default_board_rejected(tmp_path: Path):
    db = await _make_db(tmp_path)
    assert await db.archive_board("default") is False


@pytest.mark.asyncio
async def test_hard_delete_cascades_tasks(tmp_path: Path):
    db = await _make_db(tmp_path)
    await db.create_board("temp")
    t = await db.create_task(
        title="x", assignee="y", status="ready", board_id="temp"
    )
    assert await db.hard_delete_board("temp") is True
    assert await db.get_task(t.id) is None
    assert await db.get_board("temp") is None


@pytest.mark.asyncio
async def test_current_pointer_round_trip(tmp_path: Path):
    db = await _make_db(tmp_path)
    assert db.get_current_board() == "default"
    await db.create_board("alpha")
    db.set_current_board("alpha")
    assert db.get_current_board() == "alpha"


@pytest.mark.asyncio
async def test_list_tasks_board_filter(tmp_path: Path):
    db = await _make_db(tmp_path)
    await db.create_board("alpha")
    await db.create_task(
        title="t-default", assignee="x", status="ready",
    )
    await db.create_task(
        title="t-alpha", assignee="x", status="ready", board_id="alpha",
    )
    default_only = await db.list_tasks(board_id="default")
    assert {t.title for t in default_only} == {"t-default"}
    alpha_only = await db.list_tasks(board_id="alpha")
    assert {t.title for t in alpha_only} == {"t-alpha"}
    all_boards = await db.list_tasks(board_id=None)
    assert {t.title for t in all_boards} == {"t-default", "t-alpha"}


# ---- skills (per-task) ------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_with_skills_persists(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(
        title="x", assignee="y", status="ready",
        skills=["security", "k8s"],
    )
    fetched = await db.get_task(t.id)
    assert fetched.skills == ["security", "k8s"]


@pytest.mark.asyncio
async def test_create_task_skills_default_empty(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    fetched = await db.get_task(t.id)
    assert fetched.skills == []


# ---- event tail support ----------------------------------------------


@pytest.mark.asyncio
async def test_latest_event_id_returns_zero_on_empty(tmp_path: Path):
    db = await _make_db(tmp_path)
    assert await db.latest_event_id() == 0


@pytest.mark.asyncio
async def test_list_events_since_filters(tmp_path: Path):
    db = await _make_db(tmp_path)
    a = await db.create_task(title="a", assignee="x", status="ready")
    b = await db.create_task(title="b", assignee="x", status="ready")
    snap = await db.latest_event_id()
    await db.set_status(a.id, "blocked", actor="cli", reason="manual")
    await db.set_status(b.id, "blocked", actor="cli", reason="other")
    new_events = await db.list_events_since(snap)
    assert len(new_events) == 2
    only_a = await db.list_events_since(snap, task_id=a.id)
    assert all(e.task_id == a.id for e in only_a)
    assert len(only_a) == 1


# ---- v0.13 Tenacity: retry budget + workspace=dir + record_event ----


@pytest.mark.asyncio
async def test_create_task_with_max_retries(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(
        title="x", assignee="y", status="ready", max_retries=7,
    )
    fetched = await db.get_task(t.id)
    assert fetched.max_retries == 7
    assert fetched.retry_count == 0


@pytest.mark.asyncio
async def test_bump_retry_count(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    assert await db.bump_retry_count(t.id) == 1
    assert await db.bump_retry_count(t.id) == 2
    fetched = await db.get_task(t.id)
    assert fetched.retry_count == 2


@pytest.mark.asyncio
async def test_reset_retry_count(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    await db.bump_retry_count(t.id)
    await db.bump_retry_count(t.id)
    await db.reset_retry_count(t.id)
    fetched = await db.get_task(t.id)
    assert fetched.retry_count == 0


@pytest.mark.asyncio
async def test_create_task_with_dir_workspace_persists(tmp_path: Path):
    db = await _make_db(tmp_path)
    abs_path = str(tmp_path / "shared")
    t = await db.create_task(
        title="x", assignee="y", status="ready",
        workspace_kind="dir", workspace_path=abs_path,
    )
    fetched = await db.get_task(t.id)
    assert fetched.workspace_kind == "dir"
    assert fetched.workspace_path == abs_path


@pytest.mark.asyncio
async def test_atomic_claim_dir_workspace_materializes(tmp_path: Path):
    db = await _make_db(tmp_path)
    target = tmp_path / "video-project"
    await db.create_task(
        title="render", assignee="renderer", status="ready",
        workspace_kind="dir", workspace_path=str(target),
    )
    claimed = await db.atomic_claim_one_ready(claim_ttl_seconds=60)
    assert claimed is not None
    # workspace_path is the resolved (canonicalized) form
    assert Path(claimed.workspace_path) == target.resolve()
    assert target.exists()


@pytest.mark.asyncio
async def test_record_event_public(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    await db.record_event(
        t.id, "hallucination_rejected",
        {"phantom_ids": ["t_phantom"]},
        actor="worker",
    )
    events = await db.list_events(t.id)
    assert any(e.kind == "hallucination_rejected" for e in events)
