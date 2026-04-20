"""Skill registry — an ordered list of skills the Orchestrator consults.

Iteration is in registration order; the first skill whose ``match()``
returns non-None wins. Skills are stateless aside from the backends they
close over, so the registry itself is trivially safe to share across
requests.
"""
from __future__ import annotations

from .base import Skill, SkillMatch


class SkillRegistry:
    def __init__(self, skills: list[Skill] | None = None):
        self._skills: list[Skill] = list(skills or [])

    def register(self, skill: Skill) -> None:
        self._skills.append(skill)

    def match(self, message: str) -> tuple[Skill, SkillMatch] | None:
        for s in self._skills:
            hit = s.match(message)
            if hit is not None:
                return s, hit
        return None

    def names(self) -> list[str]:
        return [s.name for s in self._skills]

    def __len__(self) -> int:
        return len(self._skills)
