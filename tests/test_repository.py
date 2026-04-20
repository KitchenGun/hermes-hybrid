"""Repository tests — SQLite persistence and daily budget ledger (R4)."""
from __future__ import annotations

import pytest

from src.state import Repository, TaskState


@pytest.mark.asyncio
async def test_save_and_get_roundtrip(tmp_path):
    repo = Repository(tmp_path / "r.db")
    await repo.init()
    s = TaskState(session_id="s1", user_id="u1", user_message="hi", current_tier="L2")
    s.status = "succeeded"
    s.final_response = "hello"
    await repo.save_task(s)

    got = await repo.get_task(s.task_id)
    assert got is not None
    assert got.task_id == s.task_id
    assert got.final_response == "hello"
    assert got.status == "succeeded"


@pytest.mark.asyncio
async def test_upsert_updates_existing(tmp_path):
    repo = Repository(tmp_path / "r.db")
    await repo.init()
    s = TaskState(session_id="s1", user_id="u1", user_message="hi")
    await repo.save_task(s)
    s.status = "succeeded"
    s.final_response = "done"
    await repo.save_task(s)

    got = await repo.get_task(s.task_id)
    assert got is not None
    assert got.status == "succeeded"
    assert got.final_response == "done"


@pytest.mark.asyncio
async def test_list_user_tasks_orders_by_created_desc(tmp_path):
    repo = Repository(tmp_path / "r.db")
    await repo.init()
    tasks = []
    for _ in range(3):
        t = TaskState(session_id="s", user_id="u1", user_message="x")
        await repo.save_task(t)
        tasks.append(t)
    rows = await repo.list_user_tasks("u1", limit=10)
    assert len(rows) == 3
    # newest first
    assert rows[0].created_at >= rows[1].created_at >= rows[2].created_at


@pytest.mark.asyncio
async def test_list_user_tasks_isolates_users(tmp_path):
    repo = Repository(tmp_path / "r.db")
    await repo.init()
    await repo.save_task(TaskState(session_id="s", user_id="alice", user_message="x"))
    await repo.save_task(TaskState(session_id="s", user_id="bob", user_message="y"))
    alice = await repo.list_user_tasks("alice")
    bob = await repo.list_user_tasks("bob")
    assert len(alice) == 1 and alice[0].user_id == "alice"
    assert len(bob) == 1 and bob[0].user_id == "bob"


@pytest.mark.asyncio
async def test_daily_budget_ledger_accumulates(tmp_path):
    repo = Repository(tmp_path / "r.db")
    await repo.init()
    assert await repo.used_tokens_today("u1") == 0
    total = await repo.add_tokens("u1", 100)
    assert total == 100
    total = await repo.add_tokens("u1", 250)
    assert total == 350
    # Different user is independent.
    assert await repo.used_tokens_today("u2") == 0


@pytest.mark.asyncio
async def test_add_tokens_ignores_nonpositive(tmp_path):
    repo = Repository(tmp_path / "r.db")
    await repo.init()
    await repo.add_tokens("u1", 50)
    assert await repo.add_tokens("u1", 0) == 50
    assert await repo.add_tokens("u1", -10) == 50
