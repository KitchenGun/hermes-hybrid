"""Tests for the persistent SqliteMemory backend.

Verifies the same contract as the InMemoryMemory tests (save/list/clear,
per-user isolation, validation), plus the durability invariant: notes
must survive a "bot restart" (closing and reopening the backend against
the same DB path).
"""
from __future__ import annotations

import pytest

from src.memory import Memo, MemoryTooLarge, SqliteMemory


@pytest.mark.asyncio
async def test_sqlite_save_and_list_roundtrip(tmp_path):
    mem = SqliteMemory(tmp_path / "memo.db")
    await mem.init()
    m = await mem.save("u1", "first note")
    assert m.user_id == "u1"
    assert m.text == "first note"

    listed = await mem.list_memos("u1")
    assert [x.text for x in listed] == ["first note"]
    # timezone-aware datetime round-trip
    assert listed[0].created_at.tzinfo is not None


@pytest.mark.asyncio
async def test_sqlite_per_user_isolation(tmp_path):
    mem = SqliteMemory(tmp_path / "memo.db")
    await mem.save("u1", "alice note")
    await mem.save("u2", "bob note")

    assert [x.text for x in await mem.list_memos("u1")] == ["alice note"]
    assert [x.text for x in await mem.list_memos("u2")] == ["bob note"]


@pytest.mark.asyncio
async def test_sqlite_validation_rules(tmp_path):
    mem = SqliteMemory(tmp_path / "memo.db")
    with pytest.raises(ValueError):
        await mem.save("u1", "   ")
    with pytest.raises(MemoryTooLarge):
        await mem.save("u1", "x" * 2001)


@pytest.mark.asyncio
async def test_sqlite_list_limit_preserves_insertion_order(tmp_path):
    mem = SqliteMemory(tmp_path / "memo.db")
    for i in range(25):
        await mem.save("u1", f"note-{i}")
    listed = await mem.list_memos("u1", limit=10)
    # Same semantics as InMemoryMemory: last 10 in insertion order
    assert [m.text for m in listed] == [f"note-{i}" for i in range(15, 25)]


@pytest.mark.asyncio
async def test_sqlite_clear_returns_count_and_isolates_users(tmp_path):
    mem = SqliteMemory(tmp_path / "memo.db")
    await mem.save("u1", "a")
    await mem.save("u1", "b")
    await mem.save("u2", "keep")

    n = await mem.clear("u1")
    assert n == 2
    assert await mem.list_memos("u1") == []
    assert [m.text for m in await mem.list_memos("u2")] == ["keep"]
    # Clearing an empty user returns 0 — not an error.
    assert await mem.clear("u1") == 0


@pytest.mark.asyncio
async def test_sqlite_survives_restart(tmp_path):
    """The durability story — two backend instances against the same DB
    path must see the same notes. This is the reason SqliteMemory exists
    over InMemoryMemory."""
    db = tmp_path / "memo.db"

    mem1 = SqliteMemory(db)
    await mem1.save("u1", "before restart")
    # Explicitly do NOT share state — simulate process restart.
    del mem1

    mem2 = SqliteMemory(db)
    listed = await mem2.list_memos("u1")
    assert [x.text for x in listed] == ["before restart"]


@pytest.mark.asyncio
async def test_sqlite_auto_creates_schema_without_init(tmp_path):
    """init() is optional — save/list/clear must work on a fresh DB
    without an explicit init() call (drop-in compat with InMemoryMemory,
    which has no init step)."""
    mem = SqliteMemory(tmp_path / "memo.db")
    # No mem.init() here.
    m = await mem.save("u1", "works without init")
    assert isinstance(m, Memo)
    listed = await mem.list_memos("u1")
    assert [x.text for x in listed] == ["works without init"]
