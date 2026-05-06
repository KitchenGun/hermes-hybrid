"""Intent Router — diagram-aligned wrapper of the existing RuleLayer +
SkillRegistry + forced_profile gating.

In the all-via-master design (Phase 6 of the migration plan), the
JobFactory v2 dispatcher is removed; the master LLM does free-text
classification itself. The Intent Router's remaining job is to handle
the *deterministic* short-circuits before the master is even called:

  * ``/ping`` and other RuleLayer matches (instant reply, no LLM)
  * ``/memo``, ``/kanban``, etc. — slash skills that have their own
    handler and don't need master reasoning
  * forced_profile (Discord channel-pinned) — short-circuit to a
    specific profile

Anything that doesn't match these short-circuits goes to the master
with ``trigger_type=discord_message`` and the master decides which
profile / job / skill to invoke.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.config import Settings
from src.router import RuleLayer, RuleMatch
from src.skills import SkillContext, SkillMatch, SkillRegistry, default_registry


@dataclass
class IntentResult:
    """What the Intent Router decided about an incoming user message.

    Fields mirror the routing context columns in ExperienceRecord, so the
    HermesMasterOrchestrator can stamp them onto the task verbatim.
    """
    # short-circuit handling — when set, the master is bypassed
    handled_by: str | None = None        # "rule" / "skill:hybrid-memo" / etc.
    response: str | None = None          # populated when handled_by is set
    rule_match: RuleMatch | None = None
    skill_match: tuple[Any, SkillMatch] | None = None  # (Skill, SkillMatch)

    # routing context for the master path
    trigger_type: str = "discord_message"
    trigger_source: str | None = None
    profile_id: str | None = None
    forced_profile: str | None = None
    job_id: str | None = None
    job_category: str | None = None
    slash_skill: str | None = None
    skill_ids: list[str] = field(default_factory=list)

    @property
    def short_circuited(self) -> bool:
        """True if the response was determined without calling the master."""
        return self.handled_by is not None


class IntentRouter:
    """Wrapper around RuleLayer + SkillRegistry + forced_profile.

    The master orchestrator calls :meth:`route` once per incoming message
    and either returns the short-circuit response (RuleLayer / slash
    skill matched) or proceeds to LLM dispatch with the IntentResult's
    routing context.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        rules: RuleLayer | None = None,
        skills: SkillRegistry | None = None,
    ):
        self.settings = settings
        self.rules = rules if rules is not None else RuleLayer()
        self.skills = skills if skills is not None else default_registry(settings)

    async def route(
        self,
        *,
        user_message: str,
        user_id: str,
        session_id: str,
        forced_profile: str | None = None,
        heavy: bool = False,
        memory: Any = None,
        repo: Any = None,
        orchestrator: Any = None,
    ) -> IntentResult:
        """Resolve the message into an IntentResult.

        Order of precedence (must match orchestrator's existing behavior):
          1. RuleLayer (instant deterministic reply)
          2. Slash skills (HybridMemoSkill, KanbanSkill, ...)
          3. forced_profile (channel-pinned, short-circuits LLM choice)
          4. heavy (user-explicit, signals C2-style dispatch)
          5. fallthrough — discord_message, master decides
        """
        # 1. RuleLayer
        rule_match = self.rules.match(user_message)
        if rule_match is not None:
            return IntentResult(
                handled_by="rule",
                response=rule_match.response,
                rule_match=rule_match,
                trigger_type="discord_message",
                trigger_source=f"user:{user_id}",
            )

        # 2. Slash skills
        skill_hit = self.skills.match(user_message)
        if skill_hit is not None:
            skill, match = skill_hit
            return IntentResult(
                handled_by=f"skill:{skill.name}",
                response=None,  # caller invokes the skill — async I/O
                skill_match=skill_hit,
                trigger_type="discord_message",
                trigger_source=f"user:{user_id}",
                slash_skill=skill.name,
                job_id=skill.name,
                job_category="chat",
            )

        # 3. forced_profile (Discord channel-pinned, e.g. journal_ops on #일기)
        if forced_profile:
            return IntentResult(
                trigger_type="forced_profile",
                trigger_source=forced_profile,
                profile_id=forced_profile,
                forced_profile=forced_profile,
            )

        # 4. heavy
        if heavy:
            return IntentResult(
                trigger_type="discord_message",
                trigger_source=f"heavy:{user_id}",
            )

        # 5. fallthrough — master decides profile/job/skill
        return IntentResult(
            trigger_type="discord_message",
            trigger_source=f"user:{user_id}",
        )

    def build_skill_context(
        self,
        *,
        user_id: str,
        session_id: str,
        memory: Any,
        repo: Any = None,
        orchestrator: Any = None,
    ) -> SkillContext:
        """Construct the context that a matched slash skill expects."""
        return SkillContext(
            settings=self.settings,
            repo=repo,
            memory=memory,
            user_id=user_id,
            session_id=session_id,
            orchestrator=orchestrator,
        )


__all__ = ["IntentResult", "IntentRouter"]
