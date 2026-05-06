"""Intent Router — diagram-aligned wrapper of RuleLayer + SkillRegistry.

Phase 9 (2026-05-06): ``@handle`` mention 파싱 + AgentRegistry 검증.
Phase 11 (2026-05-06): heavy 분기 폐기 (master = single lane).
Phase 12 (2026-05-07): pipeline trigger_keyword 매치 — sequential agent workflow.

  * ``/ping`` 등 RuleLayer 매치 — instant reply, no LLM
  * ``/memo`` / ``/kanban`` 등 슬래시 skill — 자체 handler 호출
  * ``@coder`` / ``@reviewer`` 등 멘션 — 인식해서 IntentResult 에 stamp
    (master 가 prompt 에 SKILL.md inject) — **명시 mention 우선**
  * pipeline trigger_keyword 매치 → IntentResult.pipeline_id stamp
    (master 가 PipelineRunner 로 sequential 실행) — mention 없을 때만
  * forced_profile 분기 폐기 — channel-pinned 자동화는 더 이상 없음
  * 그 외 모든 자유 텍스트는 master 에 전달

``forced_profile`` 인자는 호환을 위해 시그니처에 남겨두지만 무시됨.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from typing import TYPE_CHECKING

from src.agents import AgentRegistry
from src.config import Settings
from src.router import RuleLayer, RuleMatch
from src.skills import SkillContext, SkillMatch, SkillRegistry, default_registry

if TYPE_CHECKING:
    # ``src.orchestrator/__init__.py`` 가 IntentRouter 를 import 하므로
    # 직접 import 하면 circular. 런타임엔 IntentRouter.__init__ 안에서
    # lazy import.
    from src.orchestrator.pipelines import PipelineCatalog


# `@handle` mention 추출 — 부정 lookbehind 로 email-ish (`user@example.com`)
# 패턴 회피. `\w` 또는 `.` 직전이면 매치하지 않음.
_MENTION_RE = re.compile(r"(?<![\w.])@(\w+)")


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
    # Phase 9: 사용자 입력에서 발견된 ``@handle`` 중 실제 AgentRegistry 에
    # 등록된 sub-agent 핸들들. 등록 외 mention 은 필터링됨 (e.g. email).
    agent_handles: list[str] = field(default_factory=list)
    # Phase 12: pipeline trigger_keyword 매치 시 stamp.
    # @handle 명시 mention 이 있으면 stamp X (mention 우선).
    pipeline_id: str | None = None

    @property
    def short_circuited(self) -> bool:
        """True if the response was determined without calling the master."""
        return self.handled_by is not None


class IntentRouter:
    """Wrapper around RuleLayer + SkillRegistry + AgentRegistry.

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
        agents: AgentRegistry | None = None,
        pipelines: "PipelineCatalog | None" = None,
    ):
        self.settings = settings
        self.rules = rules if rules is not None else RuleLayer()
        self.skills = skills if skills is not None else default_registry(settings)
        # Phase 9 — agent registry 로 ``@handle`` 멘션 검증.
        self.agents = agents if agents is not None else AgentRegistry()
        # Phase 12 — pipeline catalog (yaml 로드). 순환 import 회피용 lazy.
        if pipelines is None:
            from src.orchestrator.pipelines import PipelineCatalog as _PC
            pipelines = _PC()
        self.pipelines = pipelines

    async def route(
        self,
        *,
        user_message: str,
        user_id: str,
        session_id: str,
        forced_profile: str | None = None,  # 호환 — Phase 8 후 무시
        memory: Any = None,
        repo: Any = None,
        orchestrator: Any = None,
    ) -> IntentResult:
        """Resolve the message into an IntentResult.

        Order of precedence:
          1. RuleLayer (instant deterministic reply)
          2. Slash skills (HybridMemoSkill, KanbanSkill, ...)
          3. fallthrough — discord_message, master decides

        ``@handle`` 멘션은 모든 분기에서 동일 파싱 (RuleLayer / slash skill
        도 stamp 만 — 실제 inject 는 master 가 결정).
        """
        agent_handles = self._parse_agent_handles(user_message)

        # 1. RuleLayer
        rule_match = self.rules.match(user_message)
        if rule_match is not None:
            return IntentResult(
                handled_by="rule",
                response=rule_match.response,
                rule_match=rule_match,
                trigger_type="discord_message",
                trigger_source=f"user:{user_id}",
                agent_handles=agent_handles,
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
                agent_handles=agent_handles,
            )

        # 3. Phase 12: pipeline trigger_keyword 매치 — @handle 명시
        # mention 이 없을 때만 (mention 이 있으면 단일/병렬 dispatch 우선).
        pipeline_id: str | None = None
        if not agent_handles:
            matched = self.pipelines.match(user_message)
            if matched is not None:
                pipeline_id = matched.pipeline_id

        # 4. fallthrough — master decides agent/skill
        return IntentResult(
            trigger_type="discord_message",
            trigger_source=f"user:{user_id}",
            agent_handles=agent_handles,
            pipeline_id=pipeline_id,
        )

    def _parse_agent_handles(self, message: str) -> list[str]:
        """Extract `@handle` mentions that resolve to known sub-agents.

        Returns canonical handles (e.g. ``"@coder"``) preserving first-seen
        order; duplicates dropped. Unknown handles (typos, email-ish text
        that snuck past the regex) are silently filtered.
        """
        if not message:
            return []
        seen: dict[str, str] = {}  # lower → canonical
        for m in _MENTION_RE.finditer(message):
            candidate = "@" + m.group(1)
            entry = self.agents.by_handle(candidate)
            if entry is None:
                continue
            key = entry.handle.lower()
            if key not in seen:
                seen[key] = entry.handle
        return list(seen.values())

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
