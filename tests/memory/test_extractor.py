"""Tests for src.memory.ingestion.extractor (P1)."""
from __future__ import annotations

import pytest

from src.memory.ingestion.extractor import RuleExtractor
from src.memory.ingestion.sources import SourceItem


def _item(content: str, source: str = "claude", path: str = "/x.md") -> SourceItem:
    return SourceItem(source=source, source_path=path, content=content)


def test_heading_failure_pattern() -> None:
    text = (
        "# Failure pattern: acceptEdits hardcoded\n"
        "Repeated misuse of acceptEdits leading to hidden mode confusion.\n"
        "\n"
        "## next section\n"
    )
    cands = RuleExtractor().extract(_item(text))
    types = [c.type for c in cands]
    assert "failure_pattern" in types
    fp = next(c for c in cands if c.type == "failure_pattern")
    assert "acceptEdits hardcoded" in fp.title
    assert fp.confidence == "medium"
    assert fp.source_sha16  # populated


def test_heading_decision_korean_label() -> None:
    text = "## 결정: kanban 사용\n팀이 kanban 보드로 전환했다.\n"
    cands = RuleExtractor().extract(_item(text))
    decisions = [c for c in cands if c.type == "decision"]
    assert decisions
    assert "kanban" in decisions[0].title


def test_inline_preferred_line_low_confidence() -> None:
    text = "Preferred: short Korean answers"
    cands = RuleExtractor().extract(_item(text))
    prefs = [c for c in cands if c.type == "user_preference"]
    assert prefs
    assert prefs[0].confidence == "low"


def test_inline_language_tag() -> None:
    text = "Language: korean"
    cands = RuleExtractor().extract(_item(text))
    langs = [c for c in cands if c.type == "user_preference" and "language" in c.tags]
    assert langs


def test_inline_style_maps_to_response_style() -> None:
    text = "Style: terse, no trailing summary"
    cands = RuleExtractor().extract(_item(text))
    styles = [c for c in cands if c.type == "response_style"]
    assert styles


def test_prompt_fence_extracted() -> None:
    text = (
        "Some intro.\n"
        "Prompt:\n"
        "```\n"
        "You are a senior reviewer. Be terse.\n"
        "```\n"
    )
    cands = RuleExtractor().extract(_item(text))
    prompts = [c for c in cands if c.type == "prompt_template"]
    assert prompts
    assert "senior reviewer" in prompts[0].body


def test_extractor_dedupes_same_type_same_title() -> None:
    text = (
        "# Decision: use kanban\nfirst body\n"
        "## something else\n"
        "# Decision: use kanban\nsecond body\n"
    )
    cands = RuleExtractor().extract(_item(text))
    decisions = [c for c in cands if c.type == "decision"]
    # dedup is title-casefold-based; same title → one entry.
    assert len(decisions) == 1


def test_clean_text_yields_no_candidates() -> None:
    text = "just a random paragraph with no structured markers."
    cands = RuleExtractor().extract(_item(text))
    assert cands == []
