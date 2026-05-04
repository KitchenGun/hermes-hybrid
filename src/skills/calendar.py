"""``CalendarSkill`` — route calendar/schedule queries to the ``calendar_ops``
Hermes profile so the ``google-workspace`` skill (with OAuth) can actually
reach Google Calendar.

Why a Skill and not a Router / Tier?
  The default Orchestrator pipeline (Router → L2/L3/C1/C2) runs through
  models that have no authenticated Google Calendar access. Even with the
  Phase 2 Hermes C1 path (``use_hermes_for_c1=true``), the default profile
  doesn't have ``google-workspace`` + OAuth wired up — only the
  ``calendar_ops`` profile does. A skill sits in front of all that: if
  the message looks calendar-ish, we short-circuit straight to the
  calendar_ops Hermes profile via the official ``-p <name>`` flag, with
  ``-s productivity/google-workspace`` to preload the skill for the turn.

Invocation (per the official Hermes CLI reference):

    hermes -p calendar_ops chat -q "<query>" -Q \
        -m <model> --provider <provider> -s productivity/google-workspace

References:
  - https://hermes-agent.nousresearch.com/docs/reference/cli-commands
  - https://hermes-agent.nousresearch.com/docs/user-guide/profiles
  - https://hermes-agent.nousresearch.com/docs/user-guide/features/skills

Intent detection is deliberately conservative — Korean + English keywords
with word-boundary hints — so we don't hijack unrelated "meeting room"
small-talk into the heavier calendar_ops subprocess. False negatives are
fine (the message just flows through the normal pipeline); false
positives would surprise the user with a Hermes subprocess invocation.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from src.hermes_adapter.adapter import (
    HermesAdapterError,
    HermesAuthError,
    HermesBudgetExceeded,
    HermesMalformedResult,
    HermesProviderMismatch,
    HermesTimeout,
)
from src.obs import get_logger

from .base import Skill, SkillContext, SkillMatch

log = get_logger(__name__)

_KST = timezone(timedelta(hours=9))  # Asia/Seoul은 DST 없음, 항상 UTC+9
_DAYS_KR = ["월", "화", "수", "목", "금", "토", "일"]

_READ_RE = re.compile(
    r"(알려줘|보여줘|있어\??|뭐\s*있|뭐있|tell\s+me|show\s+me|what.{0,4}s\s+on|list)",
    re.IGNORECASE,
)


def _date_ctx() -> str:
    now = datetime.now(_KST)
    day = _DAYS_KR[now.weekday()]
    return (
        f"[현재 날짜: {now.strftime('%Y-%m-%d')} ({day}요일), "
        f"현재 시각: {now.strftime('%H:%M')} KST]\n\n"
    )


def _is_read(query: str) -> bool:
    return bool(_READ_RE.search(query))


# Conservative pattern set. Each alternative is a strong calendar signal
# by itself — we don't require combinations, which would be fragile across
# Korean/English mixing.
_PATTERNS = [
    # Korean: 일정 (schedule), 캘린더, 미팅, 약속, 회의 일정
    re.compile(r"(일정|캘린더|스케줄|미팅|약속|회의\s*일정)", re.IGNORECASE),
    # Korean: 삭제/수정 발화 (일정 문맥이 생략돼도 라우팅되도록)
    # — "중복 제거해줘", "아까 그거 지워줘", "14시 약속 취소" 등
    re.compile(
        r"(제거|삭제|지워|지울|취소|변경|수정|옮겨|옮겨줘|바꿔)",
        re.IGNORECASE,
    ),
    # English: calendar / schedule / meeting / appointment / agenda / event
    # Require word boundaries so "escalate" doesn't match "calen*"; use
    # ``meet(ing)?`` rather than bare "meet" to avoid random verb uses.
    re.compile(
        r"\b(calendar|schedule|agenda|appointment|meeting|meetings|"
        r"event|events)\b",
        re.IGNORECASE,
    ),
    # Natural-language "what's on my calendar" / "오늘 뭐 있어" style
    re.compile(
        r"(오늘|내일|이번\s*주|다음\s*주|이번주|다음주|this\s+week|"
        r"next\s+week|today|tomorrow)\s*[^.?!\n]{0,12}"
        r"(일정|뭐\s*있|what\'?s?\s+on|free|busy)",
        re.IGNORECASE,
    ),
]


class CalendarSkill(Skill):
    """Dispatch calendar/schedule queries to the ``calendar_ops`` profile.

    The skill is **enabled by a settings flag** (``calendar_skill_enabled``)
    rather than always-on: registering it unconditionally would intercept
    calendar queries even on machines that haven't authenticated OAuth,
    leaving the user with a less helpful error than the baseline LLM reply.
    """

    name = "calendar"

    def match(self, message: str) -> SkillMatch | None:
        text = message.strip()
        if not text:
            return None
        for pat in _PATTERNS:
            if pat.search(text):
                return SkillMatch(skill_name=self.name, args={"query": text})
        return None

    async def invoke(self, match: SkillMatch, ctx: SkillContext) -> str:
        s = ctx.settings
        orch = ctx.orchestrator
        if orch is None:
            return (
                "⚠️ CalendarSkill needs an Orchestrator in ctx "
                "(pass ctx.orchestrator in SkillContext)."
            )

        query = match.args.get("query", "").strip()
        if not query:
            return "⚠️ CalendarSkill: empty query."

        # 날짜 컨텍스트를 앞에 붙여 LLM의 요일/날짜 오계산을 방지한다.
        query = _date_ctx() + query

        # 2026-05-04: 로컬 우선 + Claude CLI fallback 정책.
        #   1) Hermes via Ollama 로 먼저 시도. active 모드에서는 무료 + 빠름.
        #   2) HermesTimeout / HermesAdapterError (ollama 다운 — 게임모드 quiet
        #      포함 — 또는 LLM 이 처리 못하는 상황) 가 발생하면 Claude CLI +
        #      cocal MCP 경로로 fallback. Max OAuth ($0 marginal) 라 비용 부담
        #      없이 신뢰성 보강.
        # 인증/예산 등 retry 해도 결과가 안 바뀌는 예외(HermesAuthError,
        # HermesProviderMismatch, HermesBudgetExceeded, HermesMalformedResult)
        # 는 fallback 시도 안 하고 그대로 propagate — 가짜 성공이 metric 을
        # 더럽히는 걸 막고, ``task.status="failed"`` + ``degraded=True`` 표식이
        # 정확히 남는다.
        try:
            return await self._invoke_via_hermes(query, ctx)
        except (HermesTimeout, HermesAdapterError) as e:
            # 로컬 LLM 실패 — Claude CLI 로 fallback.
            log.warning(
                "calendar_skill.fallback_to_claude_cli",
                err_type=type(e).__name__,
                err=str(e)[:200],
            )
            return await self._invoke_via_claude_cli(query, ctx)

    async def _invoke_via_claude_cli(
        self, query: str, ctx: SkillContext
    ) -> str:
        """Claude CLI subprocess + cocal google_calendar MCP — Hermes 실패 시
        fallback 경로. ``invoke()`` 의 except 블록에서만 호출된다.

        Hermes 실패 케이스:
          - 게임모드(system_mode=quiet) 에서 ollama 가 꺼진 상태 → 호출 즉시
            ``HermesAdapterError`` (connection refused).
          - active 모드인데 ollama 가 LLM 답변을 못 만들거나 timeout
            (``HermesTimeout``) 으로 끊긴 경우.
          - hermes-gateway 자체가 죽었거나 binary 가 missing 한 setup 오류.

        Max OAuth ($0 marginal) 로 동작하면서 ``--mcp-config`` 로 cocal
        google_calendar MCP 를 turn 단위로 attach. 비용/quota 부담 없이 1차
        실패를 흡수한다.
        """
        from src.claude_adapter.adapter import (
            ClaudeCodeAdapter,
            ClaudeCodeAdapterError,
            ClaudeCodeAuthError,
            ClaudeCodeTimeout,
        )

        s = ctx.settings
        orch = ctx.orchestrator
        # C1 인스턴스를 재사용 — 별도 semaphore로 heavy 경로와 격리되어 있음.
        adapter: ClaudeCodeAdapter = getattr(orch, "claude_code_c1", None)
        if adapter is None:
            return (
                "⚠️ CalendarSkill: orchestrator.claude_code_c1 미설정. "
                "Claude CLI 경로를 쓰려면 어댑터가 필요하다."
            )

        prompt = (
            "You are a calendar assistant. Use the google_calendar MCP "
            "tools to fulfill the user's request. Tools available include "
            "list-events, create-event, update-event, delete-event, "
            "get-event. Default calendar id is 'primary'. Reply in Korean "
            "with a short confirmation including event time/title.\n\n"
            f"User request: {query}"
        )

        try:
            result = await adapter.run(
                prompt=prompt,
                model=s.calendar_skill_claude_model,
                timeout_ms=s.calendar_skill_timeout_ms,
                persist_session=False,
                mcp_config_path=s.calendar_skill_mcp_config_path,
                allowed_tools=["mcp__google_calendar"],
            )
        except ClaudeCodeAuthError as e:
            return (
                "⚠️ Calendar (Claude CLI) auth/quota 실패. Max OAuth 토큰 "
                f"또는 사용량 한도를 확인해주세요.\n```\n{str(e)[:400]}\n```"
            )
        except ClaudeCodeTimeout as e:
            return f"⚠️ Calendar 호출이 시간 초과됐습니다.\n```\n{str(e)[:200]}\n```"
        except ClaudeCodeAdapterError as e:
            return (
                f"⚠️ Calendar 호출 실패: `{type(e).__name__}`\n"
                f"```\n{str(e)[:400]}\n```"
            )

        return result.text or "⚠️ Claude CLI returned an empty response."

    async def _invoke_via_hermes(
        self, query: str, ctx: SkillContext
    ) -> str:
        """Primary 경로: Hermes ``-p calendar_ops`` (= 로컬 ollama) 호출.

        ``invoke()`` 가 항상 이 경로부터 시도한다. ``HermesTimeout`` /
        ``HermesAdapterError`` 로 떨어지면 caller 가 잡아서 Claude CLI
        fallback 으로 내려보낸다 (게임모드 quiet, ollama 다운 등).
        """
        s = ctx.settings
        orch = ctx.orchestrator
        if not hasattr(orch, "hermes"):
            return "⚠️ CalendarSkill: orchestrator.hermes 미설정."

        max_turns = (
            s.calendar_skill_read_max_turns if _is_read(query)
            else s.calendar_skill_max_turns
        )

        try:
            result = await orch.hermes.run(
                query,
                model=s.calendar_skill_model or None,
                provider=s.calendar_skill_provider or None,
                profile=s.calendar_skill_profile,
                preload_skills=[s.calendar_skill_preload] if s.calendar_skill_preload else [],
                max_turns=max_turns,
                timeout_ms=s.calendar_skill_timeout_ms,
            )
        except (
            HermesTimeout,
            HermesAuthError,
            HermesProviderMismatch,
            HermesBudgetExceeded,
            HermesMalformedResult,
        ):
            raise
        except HermesAdapterError as e:
            return (
                f"⚠️ Calendar lookup failed: `{type(e).__name__}`\n"
                f"```\n{str(e)[:400]}\n```\n"
                f"Check that `hermes -p {s.calendar_skill_profile} chat` "
                f"runs manually and that Google OAuth is authenticated."
            )

        return result.text or "⚠️ Calendar profile returned an empty response."
