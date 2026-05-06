from typing import Any

from .base import Skill, SkillContext, SkillMatch
from .hybrid_budget import HybridBudgetSkill
from .hybrid_memo import HybridMemoSkill
from .hybrid_status import HybridStatusSkill
from .kanban_skill import KanbanSkill
from .registry import SkillRegistry


def default_registry(settings: Any | None = None) -> SkillRegistry:
    """Factory for the standard set of slash skills.

    Phase 8 (2026-05-06) 후 CalendarSkill 폐기 — 캘린더 조회/CRUD 는 master
    가 직접 @researcher / @devops 를 통해 처리. 슬래시 skill 은 deterministic
    short-circuit 만 — /memo, /kanban, /hybrid-* 류.
    """
    skills: list[Skill] = [
        HybridStatusSkill(),
        HybridBudgetSkill(),
        HybridMemoSkill(),
        KanbanSkill(),
    ]
    return SkillRegistry(skills)


__all__ = [
    "Skill",
    "SkillContext",
    "SkillMatch",
    "SkillRegistry",
    "HybridStatusSkill",
    "HybridBudgetSkill",
    "HybridMemoSkill",
    "KanbanSkill",
    "default_registry",
]
