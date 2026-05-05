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


# ---- orchestrator inject -------------------------------------------------


@pytest.mark.asyncio
async def test_memory_inject_into_history_window(tmp_path, monkeypatch):
    """When ``memory_inject_enabled=True``, the orchestrator must call
    memory.search and prepend a system-role entry on the task's
    history_window. We mock the dispatcher so the test stays in pure
    Python and exercises only the inject branch."""
    from src.config import Settings
    from src.memory import InMemoryMemory
    from src.orchestrator.orchestrator import Orchestrator

    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        memory_inject_enabled=True,
        memory_inject_top_k=2,
        require_allowlist=False,
        ollama_enabled=False,
        experience_log_enabled=False,
        experience_log_root=tmp_path / "experience",
    )
    memory = InMemoryMemory()
    await memory.save("u1", "내일 회의 9시")
    await memory.save("u1", "전혀 관련 없는 내용")

    o = Orchestrator(s, memory=memory)

    captured: dict = {}

    async def _fake_locked(task):
        captured["history_window"] = list(task.history_window)
        from src.orchestrator.orchestrator import OrchestratorResult
        task.final_response = "stub"
        task.status = "succeeded"
        return OrchestratorResult(task=task, response="stub", handled_by="rule")

    o._handle_locked = _fake_locked  # type: ignore[assignment]

    await o.handle("회의 일정 알려줘", user_id="u1")

    hw = captured["history_window"]
    assert hw, "history_window should have been prepended with memory"
    first = hw[0]
    assert first["role"] == "system"
    assert "회의" in first["content"]
    # Unrelated memo should NOT be injected (substring match filtered).
    assert "전혀 관련 없는" not in first["content"]


@pytest.mark.asyncio
async def test_memory_inject_disabled_keeps_history_window_unchanged(tmp_path):
    from src.config import Settings
    from src.memory import InMemoryMemory
    from src.orchestrator.orchestrator import Orchestrator

    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        memory_inject_enabled=False,  # default
        require_allowlist=False,
        ollama_enabled=False,
        experience_log_enabled=False,
        experience_log_root=tmp_path / "experience",
    )
    memory = InMemoryMemory()
    await memory.save("u1", "회의 9시")
    o = Orchestrator(s, memory=memory)

    captured: dict = {}

    async def _fake_locked(task):
        captured["history_window"] = list(task.history_window)
        from src.orchestrator.orchestrator import OrchestratorResult
        task.final_response = "stub"
        task.status = "succeeded"
        return OrchestratorResult(task=task, response="stub", handled_by="rule")

    o._handle_locked = _fake_locked  # type: ignore[assignment]
    await o.handle("회의 알려줘", user_id="u1", history=[{"role": "user", "content": "안녕"}])
    # history_window should be exactly what the caller passed — no inject.
    assert captured["history_window"] == [{"role": "user", "content": "안녕"}]


@pytest.mark.asyncio
async def test_memory_inject_swallows_search_errors(tmp_path):
    """memory is an enrichment — if search raises, the orchestrator must
    still process the request, just without injection."""
    from src.config import Settings
    from src.orchestrator.orchestrator import Orchestrator

    s = Settings(
        _env_file=None,  # type: ignore[call-arg]
        memory_inject_enabled=True,
        require_allowlist=False,
        ollama_enabled=False,
        experience_log_enabled=False,
        experience_log_root=tmp_path / "experience",
    )

    class _BoomMemory:
        async def search(self, *a, **kw):
            raise RuntimeError("boom")
        async def save(self, *a, **kw):
            raise NotImplementedError
        async def list_memos(self, *a, **kw):
            return []
        async def clear(self, *a, **kw):
            return 0

    o = Orchestrator(s, memory=_BoomMemory())  # type: ignore[arg-type]

    async def _fake_locked(task):
        from src.orchestrator.orchestrator import OrchestratorResult
        task.final_response = "stub"
        task.status = "succeeded"
        return OrchestratorResult(task=task, response="stub", handled_by="rule")

    o._handle_locked = _fake_locked  # type: ignore[assignment]
    result = await o.handle("anything", user_id="u1")
    assert result.handled_by == "rule"
