"""Tests for feedback_keywords.match_text (Phase 20).

Locks down:
  * negative keywords (한/영) → "negative"
  * positive keywords → "positive"
  * mixed → negative wins
  * no match → None
  * empty / None-ish → None
"""
from __future__ import annotations

import pytest

from src.core.feedback_keywords import match_text


@pytest.mark.parametrize("text", [
    "이거 틀려",
    "그건 틀렸어",
    "오답입니다",
    "wrong answer",
    "incorrect output",
    "다시 해주세요",
])
def test_negative_keywords_match(text: str):
    assert match_text(text) == "negative"


@pytest.mark.parametrize("text", [
    "정확해요",
    "감사합니다",
    "perfect, thanks",
    "완벽한 답변",
    "최고에요",
])
def test_positive_keywords_match(text: str):
    assert match_text(text) == "positive"


def test_negative_wins_when_mixed():
    assert match_text("perfect but wrong") == "negative"
    assert match_text("정확하지만 틀려") == "negative"


@pytest.mark.parametrize("text", [
    "안녕하세요 오늘 날씨 어때요?",
    "fizzbuzz 짜줘",
    "@coder fix bug",
    "",
])
def test_no_match_returns_none(text: str):
    assert match_text(text) is None


def test_none_input_safe():
    # match_text typed as str but legacy callers might pass empty.
    assert match_text("") is None
