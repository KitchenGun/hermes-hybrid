from .base import Skill, SkillContext, SkillMatch
from .hybrid_budget import HybridBudgetSkill
from .hybrid_memo import HybridMemoSkill
from .hybrid_status import HybridStatusSkill
from .registry import SkillRegistry


def default_registry() -> SkillRegistry:
    """Factory for the standard set of Phase 2 skills.

    Order matters — earlier entries win on ambiguous prefixes. The
    hybrid-* commands are all disjoint so the order is cosmetic here,
    but we keep ``hybrid-status`` first because it's the most
    frequently invoked dev tool.
    """
    return SkillRegistry([
        HybridStatusSkill(),
        HybridBudgetSkill(),
        HybridMemoSkill(),
    ])


__all__ = [
    "Skill",
    "SkillContext",
    "SkillMatch",
    "SkillRegistry",
    "HybridStatusSkill",
    "HybridBudgetSkill",
    "HybridMemoSkill",
    "default_registry",
]
