"""Tests for src.memory.ingestion.retriever (P2)."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.memory.ingestion.retriever import KeywordRetriever
from src.memory.ingestion.writer import ProcessedMemoryWriter


@pytest.fixture
def populated_root(tmp_path: Path) -> Path:
    root = tmp_path / "processed_memory"
    w = ProcessedMemoryWriter(root)
    w.write(
        type="failure_pattern",
        title="acceptEdits hardcoded confusion",
        body="When the user says acceptEdits Hermes used to assume hardcoded mode.",
        source="claude",
        source_sha16="aa" * 8,
        confidence="medium",
    )
    w.write(
        type="prompt_template",
        title="senior reviewer template",
        body="You are a senior code reviewer. Be terse.",
        source="claude",
        source_sha16="bb" * 8,
    )
    w.write(
        type="user_preference",
        title="prefers korean",
        body="user prefers Korean responses",
        source="claude",
        source_sha16="cc" * 8,
    )
    return root


def test_retriever_finds_failure_pattern_by_keyword(populated_root: Path) -> None:
    r = KeywordRetriever(populated_root)
    hits = r.search("acceptEdits hardcoded mode question", k=5)
    assert hits
    top = hits[0]
    assert top.item.type == "failure_pattern"
    assert "acceptEdits" in top.item.title


def test_retriever_finds_prompt_by_keyword(populated_root: Path) -> None:
    r = KeywordRetriever(populated_root)
    hits = r.search("can you write a senior reviewer prompt", k=5)
    types = [h.item.type for h in hits]
    assert "prompt_template" in types


def test_retriever_respects_k_limit(populated_root: Path) -> None:
    r = KeywordRetriever(populated_root)
    hits = r.search("user korean prefers acceptEdits prompt", k=2)
    assert len(hits) <= 2


def test_retriever_empty_query_returns_empty(populated_root: Path) -> None:
    r = KeywordRetriever(populated_root)
    assert r.search("", k=5) == []
    assert r.search("   ", k=5) == []


def test_retriever_no_match_returns_empty(populated_root: Path) -> None:
    r = KeywordRetriever(populated_root)
    assert r.search("absolutely-unrelated-zzzzzz", k=5) == []


def test_retriever_excludes_quarantined(tmp_path: Path) -> None:
    root = tmp_path / "processed_memory"
    w = ProcessedMemoryWriter(root)
    # PII forces needs_review → must NOT appear in retrieval results.
    w.write(
        type="user_preference",
        title="contact pref",
        body="email me at alice@example.com",
        source="claude",
        source_sha16="aa" * 8,
        pii_candidate=True,
    )
    # Active item to ensure the retriever is otherwise functioning.
    w.write(
        type="user_preference",
        title="korean response",
        body="user prefers korean responses",
        source="claude",
        source_sha16="bb" * 8,
    )
    r = KeywordRetriever(root)
    hits = r.search("email contact prefer korean", k=5)
    titles = [h.item.title for h in hits]
    assert "contact pref" not in titles


def test_retriever_failure_pattern_priority_bias(populated_root: Path) -> None:
    """Ties broken in favour of failure_pattern / decision."""
    r = KeywordRetriever(populated_root)
    # query overlaps "user" + "korean" + "acceptEdits"
    hits = r.search("user prefer korean acceptEdits failure", k=5)
    # failure_pattern should rank above the user_preference for this query.
    top_types = [h.item.type for h in hits[:2]]
    assert "failure_pattern" in top_types
