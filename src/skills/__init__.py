from typing import Any

from .base import Skill, SkillContext, SkillMatch
from .calendar import CalendarSkill
from .hybrid_budget import HybridBudgetSkill
from .hybrid_memo import HybridMemoSkill
from .hybrid_status import HybridStatusSkill
from .kanban_skill import KanbanSkill
from .registry import SkillRegistry


def default_registry(settings: Any | None = None) -> SkillRegistry:
    """Factory for the standard set of Phase 2 skills.

    Order matters — earlier entries win on ambiguous prefixes. The
    hybrid-* commands are all disjoint so the order is cosmetic among
    them, but **CalendarSkill is listed first** because its match is a
    keyword regex (not a slash prefix), and we want it to win over any
    future skill that might happen to scan the same words.

    The Calendar skill is **gated on ``settings.calendar_skill_enabled``**
    — if the caller doesn't pass settings, or the flag is off, the skill
    isn't registered at all. That way existing tests (most of which
    build the default registry without settings) don't accidentally
    pick up calendar regex matches against unrelated messages.
    """
    skills: list[Skill] = []
    if settings is not None and getattr(settings, "calendar_skill_enabled", False):
        skills.append(CalendarSkill())
    skills += [
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
    "CalendarSkill",
    "HybridStatusSkill",
    "HybridBudgetSkill",
    "HybridMemoSkill",
    "KanbanSkill",
    "default_registry",
]
