"""Tests for the Phase 2 memory stub (``src/memory``).

Covers:
  - save/list/clear round-trip per user
  - per-user isolation (u1 can't see u2's notes)
  - empty text rejected (ValueError)
  - oversize text rejected (MemoryTooLarge)
  - list limit honored (most-recent first via tail slice)
  - clear returns count, leaves other users untouched
"""
from __future__ import annotations

import pytest

from src.memory import InMemoryMemory, MemoryTooLarge


@pytest.mark.asyncio
async def test_save_and_list_roundtrip():
    mem = InMemoryMemory()
    m = await mem.save("u1", "first note")
    assert m.user_id == "u1"
    assert m.text == "first note"

    listed = await mem.list_memos("u1")
    assert [x.text for x in listed] == ["first note"]


@pytest.mark.asyncio
async def test_per_user_isolation():
    mem = InMemoryMemory()
    await mem.save("u1", "alice note")
    await mem.save("u2", "bob note")
    assert [x.text for x in await mem.list_memos("u1")] == ["alice note"]
    assert [x.text for x in await mem.list_memos("u2")] == ["bob note"]


@pytest.mark.asyncio
async def test_empty_text_rejected():
    mem = InMemoryMemory()
    with pytest.raises(ValueError):
        await mem.save("u1", "   ")


@pytest.mark.asyncio
async def test_oversize_text_rejected():
    mem = InMemoryMemory()
    with pytest.raises(MemoryTooLarge):
        await mem.save("u1", "x" * 2001)


@pytest.mark.asyncio
async def test_list_limit():
    mem = InMemoryMemory()
    for i in range(25):
        await mem.save("u1", f"note-{i}")
    listed = await mem.list_memos("u1", limit=10)
    # Tail slice → last 10 in insertion order
    assert [m.text for m in listed] == [f"note-{i}" for i in range(15, 25)]


@pytest.mark.asyncio
async def test_clear_returns_count_and_isolates_users():
    mem = InMemoryMemory()
    await mem.save("u1", "a")
    await mem.save("u1", "b")
    await mem.save("u2", "keep")

    n = await mem.clear("u1")
    assert n == 2
    assert await mem.list_memos("u1") == []
    assert [m.text for m in await mem.list_memos("u2")] == ["keep"]

    # Clearing an empty user returns 0 — not an error.
    assert await mem.clear("u1") == 0
