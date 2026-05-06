"""Tests for MemoryBackend.search — P0-C.

Locks down:
  * substring matching (not whole-token only — Korean uses no spaces)
  * empty/whitespace query → no fake "match all" behavior
  * per-user isolation (search must not leak rows across users)
  * k cap
  * SqliteMemory escapes LIKE wildcards in user input
  * InMemoryMemory most-recent-first ordering

orchestrator-level inject is exercised separately via the dedicated
``test_memory_inject_into_history_window`` to keep this file's deps to
the memory module only.
"""
from __future__ import annotations

import pytest

from src.memory import InMemoryMemory, SqliteMemory


@pytest.mark.asyncio
async def test_inmemory_search_substring_match_korean():
    mem = InMemoryMemory()
    await mem.save("u1", "내일 회의 준비")
    await mem.save("u1", "오늘 점심 메뉴 정하기")
    hits = await mem.search("u1", "회의")
    assert len(hits) == 1
    assert "회의" in hits[0].text


@pytest.mark.asyncio
async def test_inmemory_search_empty_query_returns_empty():
    mem = InMemoryMemory()
    await mem.save("u1", "anything")
    assert await mem.search("u1", "") == []
    assert await mem.search("u1", "   ") == []


@pytest.mark.asyncio
async def test_inmemory_search_per_user_isolation():
    mem = InMemoryMemory()
    await mem.save("alice", "secret note")
    await mem.save("bob", "secret note")
    hits_alice = await mem.search("alice", "secret")
    hits_bob = await mem.search("bob", "secret")
    assert len(hits_alice) == 1
    assert len(hits_bob) == 1
    assert hits_alice[0].user_id == "alice"
    assert hits_bob[0].user_id == "bob"


@pytest.mark.asyncio
async def test_inmemory_search_most_recent_first_on_ties():
    mem = InMemoryMemory()
    await mem.save("u1", "회의 1차")
    await mem.save("u1", "회의 2차")
    await mem.save("u1", "회의 3차")
    hits = await mem.search("u1", "회의", k=2)
    # Most-recent-first: "3차" before "2차"
    assert [h.text for h in hits] == ["회의 3차", "회의 2차"]


@pytest.mark.asyncio
async def test_inmemory_search_k_cap_truncates():
    mem = InMemoryMemory()
    for i in range(10):
        await mem.save("u1", f"공통키워드 {i}")
    hits = await mem.search("u1", "공통키워드", k=3)
    assert len(hits) == 3


@pytest.mark.asyncio
async def test_sqlite_search_substring_korean(tmp_path):
    mem = SqliteMemory(tmp_path / "m.db")
    await mem.save("u1", "내일 회의 준비")
    await mem.save("u1", "오늘 점심 메뉴")
    hits = await mem.search("u1", "회의")
    assert len(hits) == 1
    assert "회의" in hits[0].text


@pytest.mark.asyncio
async def test_sqlite_search_empty_query_returns_empty(tmp_path):
    mem = SqliteMemory(tmp_path / "m.db")
    await mem.save("u1", "x")
    assert await mem.search("u1", "") == []


@pytest.mark.asyncio
async def test_sqlite_search_escapes_like_wildcards(tmp_path):
    """A literal ``%`` in the query must only match literal ``%`` in
    storage — not act as a SQL wildcard. Otherwise any user could exfil
    every memo by searching for ``%``."""
    mem = SqliteMemory(tmp_path / "m.db")
    await mem.save("u1", "할인율 50% 적용")
    await mem.save("u1", "다른 메모 내용")
    # ``%`` alone with naive concat would match everything via LIKE
    # ``%%%`` (string % %). Properly escaped, it only matches the row
    # that contains a literal % character.
    hits = await mem.search("u1", "%")
    assert len(hits) == 1
    assert "50%" in hits[0].text


@pytest.mark.asyncio
async def test_sqlite_search_per_user_isolation(tmp_path):
    mem = SqliteMemory(tmp_path / "m.db")
    await mem.save("alice", "공통 키워드")
    await mem.save("bob", "공통 키워드")
    a = await mem.search("alice", "공통")
    assert len(a) == 1
    assert a[0].user_id == "alice"


@pytest.mark.asyncio
async def test_sqlite_search_k_cap(tmp_path):
    mem = SqliteMemory(tmp_path / "m.db")
    for i in range(8):
        await mem.save("u1", f"keyword {i}")
    hits = await mem.search("u1", "keyword", k=4)
    assert len(hits) == 4
