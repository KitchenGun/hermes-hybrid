"""Tests for src.memory.ingestion.conflict (P1)."""
from __future__ import annotations

import pytest

from src.memory.ingestion.conflict import detect_pairs, select_runtime_winner
from src.memory.ingestion.extractor import Candidate
from src.memory.ingestion.writer import MemoryItem, make_item_id


def _cand(title: str, body: str, sha: str = "aa" * 8, type_: str = "decision") -> Candidate:
    return Candidate(
        type=type_,
        title=title,
        body=body,
        source="claude",
        source_sha16=sha,
        source_path="/x.md",
    )


def test_detect_pairs_flags_diverging_bodies() -> None:
    cands = [
        _cand("DB engine", "we use postgres", sha="aa" * 8),
        _cand("DB engine", "we use sqlite", sha="bb" * 8),
    ]
    pairs = detect_pairs(cands)
    assert len(pairs) == 1
    assert pairs[0].bodies_equivalent is False


def test_detect_pairs_ignores_normalised_equivalent_bodies() -> None:
    cands = [
        _cand("DB engine", "we use postgres.", sha="aa" * 8),
        _cand("DB engine", "we   use   postgres", sha="bb" * 8),
    ]
    pairs = detect_pairs(cands)
    # Bodies normalise to the same string → not a conflict.
    assert pairs == []


def test_detect_pairs_groups_by_type_and_slug() -> None:
    cands = [
        _cand("policy", "p1", type_="decision"),
        _cand("policy", "p2", sha="bb" * 8, type_="decision"),
        _cand("policy", "u1", type_="user_preference"),  # same slug, different type → no conflict
    ]
    pairs = detect_pairs(cands)
    assert len(pairs) == 1
    assert pairs[0].type == "decision"


def _item(
    item_id: str,
    *,
    status: str = "active",
    updated_at: str = "2026-05-09T00:00:00+00:00",
    body: str = "b",
) -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        type="decision",
        title="t",
        body=body,
        source="claude",
        source_sha16="aa" * 8,
        created_at=updated_at,
        updated_at=updated_at,
        status=status,
    )


def test_runtime_winner_picks_freshest_active() -> None:
    a = _item("decision:aa:foo", updated_at="2026-05-01T00:00:00+00:00")
    b = _item("decision:bb:foo", updated_at="2026-05-09T00:00:00+00:00")
    win = select_runtime_winner([a, b])
    assert win is b


def test_runtime_winner_skips_needs_review() -> None:
    a = _item(
        "decision:aa:foo",
        status="needs_review",
        updated_at="2026-05-09T00:00:00+00:00",
    )
    b = _item(
        "decision:bb:foo",
        status="active",
        updated_at="2026-05-01T00:00:00+00:00",
    )
    win = select_runtime_winner([a, b])
    assert win is b


def test_runtime_winner_returns_none_when_only_quarantined() -> None:
    a = _item("decision:aa:foo", status="needs_review")
    b = _item("decision:bb:foo", status="superseded")
    assert select_runtime_winner([a, b]) is None
