"""Tests for src.memory.ingestion.normalizer (P0-B)."""
from __future__ import annotations

from src.memory.ingestion.normalizer import (
    claude_to_hermes,
    dedupe_items,
    normalize_body_for_compare,
)
from src.memory.ingestion.writer import (
    MemoryItem,
    make_item_id,
)


def test_normalize_body_collapses_whitespace_and_punctuation() -> None:
    a = "Hello   world."
    b = "hello\nworld"
    assert normalize_body_for_compare(a) == normalize_body_for_compare(b)


def test_normalize_body_empty() -> None:
    assert normalize_body_for_compare("") == ""
    assert normalize_body_for_compare("   ") == ""


def test_claude_to_hermes_collapses_double_at() -> None:
    assert claude_to_hermes("@@coder please review") == "@coder please review"


def test_claude_to_hermes_rewrites_slash_skill() -> None:
    out = claude_to_hermes("Use /memo save 'today notes' to record")
    assert "[skill: memo]" in out


def test_claude_to_hermes_preserves_fenced_code() -> None:
    text = "Run this:\n```\n/memo save 'in code'\n```\nelse /memo save outside"
    out = claude_to_hermes(text)
    # Inside the fence, slash skill stays verbatim.
    assert "/memo save 'in code'" in out
    # Outside the fence, slash skill becomes [skill: memo].
    assert "[skill: memo] save outside" in out


def test_claude_to_hermes_keeps_path_links() -> None:
    text = "See [config](src/config.py:182) for details."
    assert claude_to_hermes(text) == text


def _item(item_id: str) -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        type="user_preference",
        title="t",
        body="b",
        source="claude",
        source_sha16="aa" * 8,
        created_at="2026-05-09T00:00:00+00:00",
        updated_at="2026-05-09T00:00:00+00:00",
    )


def test_dedupe_items_keeps_first_occurrence() -> None:
    iid = make_item_id(type="user_preference", source_sha16="aa" * 8, title="t")
    items = [_item(iid), _item(iid), _item("user_preference:bbbbbbbbbbbbbbbb:other")]
    out = dedupe_items(items)
    assert len(out) == 2
    assert out[0].item_id == iid


def test_dedupe_preserves_input_order() -> None:
    a = make_item_id(type="user_preference", source_sha16="aa" * 8, title="alpha")
    b = make_item_id(type="user_preference", source_sha16="bb" * 8, title="beta")
    items = [_item(b), _item(a), _item(b)]
    out = dedupe_items(items)
    assert [it.item_id for it in out] == [b, a]
