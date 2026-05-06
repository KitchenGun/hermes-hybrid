"""Intent Router — diagram-aligned wrapper of RuleLayer + SkillRegistry.

Phase 8 (2026-05-06) 후 책임 축소:

  * ``/ping`` 등 RuleLayer 매치 — instant reply, no LLM
  * ``/memo`` / ``/kanban`` 등 슬래시 skill — 자체 handler 호출
  * forced_profile 분기 폐기 — channel-pinned 자동화는 더 이상 없음
  * 그 외 모든 자유 텍스트는 master 에 전달 (master 가 어떤 agent /
    어떤 skill 을 호출할지 결정)

``forced_profile`` 인자는 호환을 위해 시그니처에 남겨두지만 무시됨 (gateway
의 일부 코드가 한동안 이 인자를 넘길 수 있음 — 단계적 정리).
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

    Fields mirror the routing context columns in ExperienceRecord so the
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
    profile_id: str | None = None        # 호환 필드. Phase 8 후 미사용.
    forced_profile: str | None = None    # 호환 필드. Phase 8 후 미사용.
    job_id: str | None = None
    job_category: str | None = None
    slash_skill: str | None = None
    skill_ids: list[str] = field(default_factory=list)

    @property
    def short_circuited(self) -> bool:
        """True if the response was determined without calling the master."""
        return self.handled_by is not None


class IntentRouter:
    """Wrapper around RuleLayer + SkillRegistry.

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
        forced_profile: str | None = None,  # 호환 — Phase 8 후 무시
        heavy: bool = False,
        memory: Any = None,
        repo: Any = None,
        orchestrator: Any = None,
    ) -> IntentResult:
        """Resolve the message into an IntentResult.

        Order of precedence:
          1. RuleLayer (instant deterministic reply)
          2. Slash skills (HybridMemoSkill, KanbanSkill, ...)
          3. heavy (user-explicit, signals C2-style dispatch)
          4. fallthrough — discord_message, master decides
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

        # 3. heavy
        if heavy:
            return IntentResult(
                trigger_type="discord_message",
                trigger_source=f"heavy:{user_id}",
            )

        # 4. fallthrough — master decides agent/skill
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
