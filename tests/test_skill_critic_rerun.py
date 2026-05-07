"""Tests for skill_critic_rerun.score_draft (Phase 18, 2026-05-07).

Locks down:
  * malformed input → 0.0 (never raises)
  * missing required keys lower the base proportionally
  * complete frontmatter + bonuses → 1.0
  * threshold semantic — score ≥ 0.85 means auto-install candidate
"""
from __future__ import annotations

from src.jobs.skill_critic_rerun import score_draft


_GOOD = """\
---
name: testbot
agent_handle: "@testbot"
category: implementation
role: do_testing
description: 자동 테스트 추가를 담당하는 sub-agent. 충분히 길어 description 보너스 받음.
when_to_use:
  - 신규 모듈에 테스트 추가
  - 회귀 테스트 누락 보완
  - PR 직전 커버리지 점검
not_for:
  - 운영 디버깅
inputs:
  - 테스트 대상 함수
  - 기대 동작 명세
outputs:
  - 추가된 pytest 케이스
---

# @testbot

본문...
"""


def test_complete_frontmatter_scores_at_least_promotion_threshold():
    score = score_draft(_GOOD)
    assert score >= 0.85, f"expected ≥0.85 (auto-install), got {score:.2f}"


def test_empty_input_returns_zero():
    assert score_draft("") == 0.0
    assert score_draft("not yaml at all") == 0.0


def test_no_frontmatter_returns_zero():
    assert score_draft("# Heading only\n\nbody text") == 0.0


def test_unterminated_frontmatter_returns_zero():
    text = "---\nname: oops\nagent_handle: \"@oops\"\n"
    assert score_draft(text) == 0.0


def test_invalid_yaml_returns_zero():
    text = "---\nname: ok\n  : ok ill formed\n---\n"
    assert score_draft(text) == 0.0


def test_missing_keys_lower_score():
    minimal = """\
---
name: a
agent_handle: "@a"
category: implementation
---
"""
    score = score_draft(minimal)
    # Has 3/9 required keys present + no bonuses → ~0.27
    assert 0.20 <= score <= 0.40, f"got {score:.2f}"
    # Definitely not auto-install grade
    assert score < 0.85


def test_score_below_threshold_when_when_to_use_too_short():
    """when_to_use 1개만 → +0.05 보너스 못 받음 → 더 낮음."""
    text = _GOOD.replace(
        "when_to_use:\n  - 신규 모듈에 테스트 추가\n  - 회귀 테스트 누락 보완\n  - PR 직전 커버리지 점검",
        "when_to_use:\n  - 단 하나",
    )
    score_full = score_draft(_GOOD)
    score_short = score_draft(text)
    assert score_short < score_full
    assert score_full - score_short >= 0.04   # ~0.05 bonus delta


def test_short_description_loses_bonus():
    text = _GOOD.replace(
        "description: 자동 테스트 추가를 담당하는 sub-agent. 충분히 길어 description 보너스 받음.",
        "description: 짧음",
    )
    assert score_draft(text) < score_draft(_GOOD)
