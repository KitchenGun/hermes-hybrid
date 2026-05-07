"""Skill Critic Rerun — Phase 18 (2026-05-07).

SkillPromoter 가 만든 SKILL.md draft 가 auto-install 가치 있는지 평가.

설계 결정: master LLM 호출 없이 형식 + 콘텐츠 minimal 검증으로 0~1 score.
이유:
  * SkillPromoter run_weekly 자체가 일요일 23:30 단발 — 추가 LLM 호출 비용
    크고, draft 가 자기 자신을 채점하면 회로가 자기 강화 됨.
  * SKILL.md 의 품질 신호는 frontmatter 완결성 + when_to_use/outputs 충실도
    로 충분히 측정 가능. 효과는 진짜 사용 시 self_score 가 알려줌 — 그래서
    Phase 18 의 두 번째 가드 (auto-revert at 5 uses + score<0.3) 가 따로 있음.

Score breakdown:
  * frontmatter parse 가능 + required keys 8개 → base 0~0.8
  * when_to_use 항목 ≥ 3 → +0.05
  * outputs 항목 ≥ 1 → +0.05
  * description 길이 ≥ 20 → +0.05
  * not_for 항목 ≥ 1 → +0.05
  * 최대 1.0

threshold ≥ 0.85 (config.skill_auto_promotion_threshold) 면 auto-install.
"""
from __future__ import annotations

from typing import Any

import yaml


_REQUIRED_KEYS = (
    "name",
    "agent_handle",
    "category",
    "role",
    "description",
    "when_to_use",
    "not_for",
    "inputs",
    "outputs",
)


def score_draft(draft_text: str) -> float:
    """Return 0.0~1.0 quality score for a SKILL.md draft.

    Robust against malformed frontmatter — returns 0.0 instead of raising.
    """
    if not draft_text or not draft_text.startswith("---"):
        return 0.0
    end = draft_text.find("\n---", 4)
    if end == -1:
        return 0.0
    try:
        fm: Any = yaml.safe_load(draft_text[4:end])
    except yaml.YAMLError:
        return 0.0
    if not isinstance(fm, dict):
        return 0.0

    present = sum(1 for k in _REQUIRED_KEYS if fm.get(k))
    base = (present / len(_REQUIRED_KEYS)) * 0.8

    bonus = 0.0
    when_to_use = fm.get("when_to_use")
    if isinstance(when_to_use, list) and len(when_to_use) >= 3:
        bonus += 0.05
    outputs = fm.get("outputs")
    if isinstance(outputs, list) and len(outputs) >= 1:
        bonus += 0.05
    desc = fm.get("description")
    if isinstance(desc, str) and len(desc.strip()) >= 20:
        bonus += 0.05
    not_for = fm.get("not_for")
    if isinstance(not_for, list) and len(not_for) >= 1:
        bonus += 0.05

    return min(1.0, round(base + bonus, 3))


__all__ = ["score_draft"]
