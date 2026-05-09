"""Tests for KanbanDispatcher (Phase 2-A).

spawn_runner is mocked; ``now``/``pid_alive`` are injected so TTL and
crash recovery are testable without real processes or sleeps.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.kanban import KanbanDB
from src.core.kanban.dispatcher import KanbanDispatcher
from src.core.kanban.tools import kanban_complete


async def _make_db(tmp_path: Path) -> KanbanDB:
    db = KanbanDB(tmp_path / "k.db", workspaces_root=tmp_path / "ws")
    await db.migrate()
    return db


def _spawn_returns(pid_value: int):
    async def _runner(task):
        return pid_value
    return _runner


def _spawn_raises(exc: Exception):
    async def _runner(task):
        raise exc
    return _runner


@pytest.mark.asyncio
async def test_promote_todo_when_parents_all_done(tmp_path: Path):
    db = await _make_db(tmp_path)
    p = await db.create_task(title="p", assignee="x", status="done")
    c = await db.create_task(
        title="c", assignee="y", status="todo", parents=[p.id]
    )
    disp = KanbanDispatcher(
        db, spawn_runner=_spawn_returns(0), max_inflight=0,
    )
    report = await disp.tick()
    assert c.id in report["promoted"]
    fetched = await db.get_task(c.id)
    assert fetched.status == "ready"


@pytest.mark.asyncio
async def test_does_not_promote_if_any_parent_not_done(tmp_path: Path):
    db = await _make_db(tmp_path)
    a = await db.create_task(title="a", assignee="x", status="done")
    b = await db.create_task(title="b", assignee="x", status="ready")
    c = await db.create_task(
        title="c", assignee="y", status="todo", parents=[a.id, b.id]
    )
    disp = KanbanDispatcher(
        db, spawn_runner=_spawn_returns(0), max_inflight=0,
    )
    await disp.tick()
    fetched = await db.get_task(c.id)
    assert fetched.status == "todo"


@pytest.mark.asyncio
async def test_promote_triage_when_scheduled_at_reached(tmp_path: Path):
    db = await _make_db(tmp_path)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(
        timespec="seconds"
    )
    t = await db.create_task(
        title="x", assignee="y", status="triage", scheduled_at=past
    )
    disp = KanbanDispatcher(
        db, spawn_runner=_spawn_returns(0), max_inflight=0,
    )
    report = await disp.tick()
    assert t.id in report["promoted"]
    fetched = await db.get_task(t.id)
    assert fetched.status == "ready"


@pytest.mark.asyncio
async def test_does_not_promote_when_scheduled_at_in_future(tmp_path: Path):
    db = await _make_db(tmp_path)
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(
        timespec="seconds"
    )
    t = await db.create_task(
        title="x", assignee="y", status="todo", scheduled_at=future
    )
    disp = KanbanDispatcher(
        db, spawn_runner=_spawn_returns(0), max_inflight=0,
    )
    report = await disp.tick()
    assert t.id not in report["promoted"]


@pytest.mark.asyncio
async def test_atomic_claim_and_spawn_records_pid(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    spawned: list[str] = []

    async def runner(task):
        spawned.append(task.id)
        return 12345

    disp = KanbanDispatcher(db, spawn_runner=runner, max_inflight=1)
    report = await disp.tick()
    assert t.id in report["claimed"]
    assert spawned == [t.id]
    runs = await db.list_runs(t.id)
    assert runs[0].pid == 12345


@pytest.mark.asyncio
async def test_concurrency_limit_one(tmp_path: Path):
    db = await _make_db(tmp_path)
    a = await db.create_task(
        title="a", assignee="x", status="ready", priority=2
    )
    b = await db.create_task(
        title="b", assignee="x", status="ready", priority=1
    )
    spawned: list[str] = []

    async def runner(task):
        spawned.append(task.id)
        return 1

    disp = KanbanDispatcher(db, spawn_runner=runner, max_inflight=1)
    report = await disp.tick()
    assert spawned == [a.id]  # only highest-priority claimed
    assert b.id not in report["claimed"]
    fetched_b = await db.get_task(b.id)
    assert fetched_b.status == "ready"


@pytest.mark.asyncio
async def test_inflight_recovers_after_complete(tmp_path: Path):
    db = await _make_db(tmp_path)
    a = await db.create_task(
        title="a", assignee="x", status="ready", priority=2
    )
    b = await db.create_task(
        title="b", assignee="x", status="ready", priority=1
    )
    pids = iter([101, 102])

    async def runner(task):
        return next(pids)

    disp = KanbanDispatcher(db, spawn_runner=runner, max_inflight=1)

    r1 = await disp.tick()
    assert a.id in r1["claimed"]

    await kanban_complete(db, task_id=a.id, summary="done")

    r2 = await disp.tick()
    assert b.id in r2["claimed"]


@pytest.mark.asyncio
async def test_spawn_failure_resets_to_ready(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    disp = KanbanDispatcher(
        db, spawn_runner=_spawn_raises(RuntimeError("boom")),
        spawn_failure_limit=5,
    )
    report = await disp.tick()
    assert t.id in report["spawn_failed"]
    fetched = await db.get_task(t.id)
    assert fetched.status == "ready"
    assert fetched.spawn_failure_count == 1


@pytest.mark.asyncio
async def test_circuit_breaker_after_n_failures(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    disp = KanbanDispatcher(
        db, spawn_runner=_spawn_raises(RuntimeError("boom")),
        spawn_failure_limit=3,
    )
    for _ in range(3):
        await disp.tick()
    fetched = await db.get_task(t.id)
    assert fetched.status == "blocked"
    assert fetched.spawn_failure_count == 3


@pytest.mark.asyncio
async def test_reclaim_ttl_expired_run(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    await db.atomic_claim_one_ready(claim_ttl_seconds=1)

    far_future = datetime.now(timezone.utc) + timedelta(hours=10)
    disp = KanbanDispatcher(
        db, spawn_runner=_spawn_returns(0),
        now=lambda: far_future,
    )
    report = await disp.tick()
    assert t.id in report["reclaimed"]
    fetched = await db.get_task(t.id)
    assert fetched.status == "ready"
    runs = await db.list_runs(t.id)
    assert runs[0].outcome == "timed_out"


@pytest.mark.asyncio
async def test_reclaim_dead_pid(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")
    claimed = await db.atomic_claim_one_ready(claim_ttl_seconds=99999)
    await db.attach_pid(claimed.current_run_id, 99999)
    disp = KanbanDispatcher(
        db, spawn_runner=_spawn_returns(0),
        pid_alive=lambda pid: False,
    )
    report = await disp.tick()
    assert t.id in report["reclaimed"]
    runs = await db.list_runs(t.id)
    assert runs[-1].outcome == "crashed"


@pytest.mark.asyncio
async def test_disabled_dispatcher_noop(tmp_path: Path):
    db = await _make_db(tmp_path)
    await db.create_task(title="x", assignee="y", status="ready")
    disp = KanbanDispatcher(
        db, spawn_runner=_spawn_returns(0), enabled=False,
    )
    report = await disp.tick()
    assert report == {}


@pytest.mark.asyncio
async def test_run_loop_starts_and_stops(tmp_path: Path):
    db = await _make_db(tmp_path)
    disp = KanbanDispatcher(
        db, spawn_runner=_spawn_returns(0), poll_seconds=10,
    )
    task = asyncio.create_task(disp.run())
    await asyncio.sleep(0.1)
    await disp.stop()
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_notify_callback_invoked_on_claim(tmp_path: Path):
    db = await _make_db(tmp_path)
    await db.create_task(title="x", assignee="y", status="ready")
    notifications: list[tuple[str, str]] = []

    async def notify(kind, tid):
        notifications.append((kind, tid))

    disp = KanbanDispatcher(
        db, spawn_runner=_spawn_returns(1), notify=notify,
    )
    await disp.tick()
    kinds = {k for k, _ in notifications}
    assert "claimed" in kinds


@pytest.mark.asyncio
async def test_notify_failure_does_not_break_tick(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(title="x", assignee="y", status="ready")

    async def bad_notify(kind, tid):
        raise RuntimeError("notify boom")

    disp = KanbanDispatcher(
        db, spawn_runner=_spawn_returns(1), notify=bad_notify,
    )
    report = await disp.tick()
    assert t.id in report["claimed"]


@pytest.mark.asyncio
async def test_dry_run_reports_promote_without_writing(tmp_path: Path):
    db = await _make_db(tmp_path)
    p = await db.create_task(title="p", assignee="x", status="done")
    c = await db.create_task(
        title="c", assignee="y", status="todo", parents=[p.id]
    )
    spawned = []

    async def runner(task):
        spawned.append(task.id)
        return 1

    disp = KanbanDispatcher(db, spawn_runner=runner)
    report = await disp.tick(dry_run=True)
    assert c.id in report["promoted"]
    # No mutation: child still todo
    fetched = await db.get_task(c.id)
    assert fetched.status == "todo"
    assert spawned == []  # spawn_runner never called


@pytest.mark.asyncio
async def test_dry_run_reports_what_would_claim(tmp_path: Path):
    db = await _make_db(tmp_path)
    a = await db.create_task(
        title="a", assignee="x", status="ready", priority=2
    )
    b = await db.create_task(
        title="b", assignee="x", status="ready", priority=1
    )

    async def runner(task):
        raise RuntimeError("should not be called")

    disp = KanbanDispatcher(db, spawn_runner=runner, max_inflight=999)
    report = await disp.tick(dry_run=True)
    assert a.id in report["claimed"]
    assert b.id in report["claimed"]
    # No actual claim: still ready
    fetched_a = await db.get_task(a.id)
    fetched_b = await db.get_task(b.id)
    assert fetched_a.status == "ready"
    assert fetched_b.status == "ready"


@pytest.mark.asyncio
async def test_max_claims_caps_step4(tmp_path: Path):
    db = await _make_db(tmp_path)
    for i in range(5):
        await db.create_task(
            title=f"t{i}", assignee="x", status="ready", priority=i
        )
    spawned = []

    async def runner(task):
        spawned.append(task.id)
        return 1

    disp = KanbanDispatcher(db, spawn_runner=runner, max_inflight=10)
    report = await disp.tick(max_claims=2)
    assert len(report["claimed"]) == 2
    assert len(spawned) == 2


# ---- v0.13 Tenacity: per-task retry budget + auto-block ----


@pytest.mark.asyncio
async def test_reclaim_within_budget_returns_to_ready(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(
        title="x", assignee="y", status="ready", max_retries=3,
    )
    await db.atomic_claim_one_ready(claim_ttl_seconds=1)
    far_future = datetime.now(timezone.utc) + timedelta(hours=10)
    disp = KanbanDispatcher(
        db, spawn_runner=_spawn_returns(0),
        now=lambda: far_future,
    )
    report = await disp.tick()
    assert t.id in report["reclaimed"]
    assert t.id not in report["blocked"]
    fetched = await db.get_task(t.id)
    assert fetched.status == "ready"
    assert fetched.retry_count == 1


@pytest.mark.asyncio
async def test_reclaim_exhausts_budget_auto_blocks(tmp_path: Path):
    db = await _make_db(tmp_path)
    t = await db.create_task(
        title="x", assignee="y", status="ready", max_retries=2,
    )
    far_future = datetime.now(timezone.utc) + timedelta(hours=10)
    disp = KanbanDispatcher(
        db, spawn_runner=_spawn_returns(0),
        now=lambda: far_future,
    )
    # Tick 1: claim → expire → retry_count=1 → ready
    await db.atomic_claim_one_ready(claim_ttl_seconds=1)
    await disp.tick()
    fetched = await db.get_task(t.id)
    assert fetched.status == "ready"
    assert fetched.retry_count == 1
    # Tick 2: claim again → expire → retry_count=2 == max_retries → blocked
    await db.atomic_claim_one_ready(claim_ttl_seconds=1)
    report = await disp.tick()
    assert t.id in report["blocked"]
    fetched = await db.get_task(t.id)
    assert fetched.status == "blocked"
    assert fetched.retry_count == 2
