"""Tests for embedding-based memory search (Phase 4).

We mock the embedder so the test stays in pure Python — no ollama
dependency. The contract we lock down:
  * cosine ranks more-similar pairs above less-similar
  * search returns top-k by cosine
  * embedder failure falls back to inner backend's LIKE search
  * empty query → empty result (no fake "match all")
  * pass-through: save/list/clear delegate to inner unchanged
  * cache deduplicates repeat embedding calls within a single search
"""
from __future__ import annotations

import pytest

from src.memory import (
    EmbeddingMemoryBackend,
    InMemoryMemory,
    cosine,
)


# ---- cosine helper -------------------------------------------------------


def test_cosine_identical_vectors_is_one():
    assert cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)


def test_cosine_orthogonal_is_zero():
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_empty_returns_zero():
    assert cosine([], [1.0]) == 0.0
    assert cosine([1.0], []) == 0.0


def test_cosine_zero_vector_returns_zero():
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_cosine_dimension_mismatch_returns_zero():
    assert cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0


# ---- EmbeddingMemoryBackend ---------------------------------------------


def _stub_embedder(table: dict[str, list[float]]):
    """Return an embedder that looks up exact text in a table.
    Strings not in the table → raises ValueError (treated as embed failure)."""
    def _embed(text: str) -> list[float]:
        if text in table:
            return list(table[text])
        raise ValueError(f"no embedding for {text!r}")
    return _embed


@pytest.mark.asyncio
async def test_search_ranks_by_cosine(tmp_path):
    embeddings = {
        "회의 일정": [1.0, 0.0, 0.0],
        "내일 회의 9시":      [0.95, 0.05, 0.0],   # close to query
        "전혀 무관한 내용":    [0.0, 1.0, 0.0],     # orthogonal
        "회의실 예약":         [0.7, 0.3, 0.0],     # somewhat close
    }
    inner = InMemoryMemory()
    await inner.save("u1", "내일 회의 9시")
    await inner.save("u1", "전혀 무관한 내용")
    await inner.save("u1", "회의실 예약")

    mem = EmbeddingMemoryBackend(
        inner, model="bge-m3", base_url="http://x",
        embedder=_stub_embedder(embeddings),
    )
    hits = await mem.search("u1", "회의 일정", k=2)
    assert len(hits) == 2
    assert hits[0].text == "내일 회의 9시"
    assert hits[1].text == "회의실 예약"


@pytest.mark.asyncio
async def test_search_falls_back_to_inner_when_embedder_fails():
    """If query embedding raises, the wrapper must use inner's search
    so the user never sees an empty result from a backend hiccup."""
    inner = InMemoryMemory()
    await inner.save("u1", "내일 회의 9시")
    await inner.save("u1", "전혀 무관한 내용")

    def _broken(_text: str) -> list[float]:
        raise OSError("ollama down")

    mem = EmbeddingMemoryBackend(
        inner, model="bge-m3", base_url="http://x", embedder=_broken,
    )
    # Inner's LIKE search will match the token "회의" → 1 hit.
    hits = await mem.search("u1", "회의 일정")
    assert len(hits) == 1
    assert "회의" in hits[0].text


@pytest.mark.asyncio
async def test_search_empty_query_returns_empty():
    inner = InMemoryMemory()
    await inner.save("u1", "anything")
    mem = EmbeddingMemoryBackend(
        inner, model="bge-m3", base_url="http://x",
        embedder=lambda _t: [1.0],
    )
    assert await mem.search("u1", "") == []
    assert await mem.search("u1", "   ") == []


@pytest.mark.asyncio
async def test_save_list_clear_pass_through_to_inner():
    inner = InMemoryMemory()
    mem = EmbeddingMemoryBackend(
        inner, model="bge-m3", base_url="http://x",
        embedder=lambda _t: [1.0],
    )
    m = await mem.save("u1", "hello")
    assert m.text == "hello"
    listed = await mem.list_memos("u1")
    assert [x.text for x in listed] == ["hello"]
    n = await mem.clear("u1")
    assert n == 1


@pytest.mark.asyncio
async def test_search_cache_deduplicates_calls(tmp_path):
    """Repeated embedding requests for the same text must hit the LRU
    cache rather than re-calling the embedder."""
    calls: list[str] = []
    embeddings = {
        "q": [1.0, 0.0],
        "memo": [0.9, 0.1],
    }

    def _counting_embedder(text: str) -> list[float]:
        calls.append(text)
        if text in embeddings:
            return list(embeddings[text])
        raise ValueError("missing")

    inner = InMemoryMemory()
    await inner.save("u1", "memo")
    mem = EmbeddingMemoryBackend(
        inner, model="bge-m3", base_url="http://x",
        embedder=_counting_embedder,
    )

    # First call: 1 query embed + 1 memo embed = 2 calls
    await mem.search("u1", "q")
    # Second call same query + same memos: 0 new calls (both cached)
    await mem.search("u1", "q")

    assert calls.count("q") == 1
    assert calls.count("memo") == 1


@pytest.mark.asyncio
async def test_search_handles_per_memo_embed_failure():
    """One memo's embed throws → we skip that memo, not the whole search."""
    embeddings = {
        "q": [1.0, 0.0],
        "ok memo": [0.9, 0.1],
    }

    def _embedder(text: str) -> list[float]:
        if text in embeddings:
            return list(embeddings[text])
        raise OSError("transient")

    inner = InMemoryMemory()
    await inner.save("u1", "ok memo")
    await inner.save("u1", "broken memo")

    mem = EmbeddingMemoryBackend(
        inner, model="bge-m3", base_url="http://x", embedder=_embedder,
    )
    hits = await mem.search("u1", "q")
    assert len(hits) == 1
    assert hits[0].text == "ok memo"
