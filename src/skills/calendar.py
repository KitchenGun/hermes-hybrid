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

from .base import Skill, SkillContext, SkillMatch


# Conservative pattern set. Each alternative is a strong calendar signal
# by itself — we don't require combinations, which would be fragile across
# Korean/English mixing.
_PATTERNS = [
    # Korean: 일정 (schedule), 캘린더, 미팅, 약속, 회의 일정
    re.compile(r"(일정|캘린더|스케줄|미팅|약속|회의\s*일정)", re.IGNORECASE),
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
        if orch is None or not hasattr(orch, "hermes"):
            # Shouldn't happen in production — Orchestrator always passes
            # itself in the context. Fail loud for tests that forget.
            return (
                "⚠️ CalendarSkill needs Orchestrator.hermes wired up "
                "(pass ctx.orchestrator in SkillContext)."
            )

        query = match.args.get("query", "").strip()
        if not query:
            return "⚠️ CalendarSkill: empty query."

        # Fire a one-shot Hermes invocation against the calendar_ops profile
        # with the google-workspace skill preloaded. Provider + model are
        # configurable so operators can flip between ollama-local and an
        # openai-direct custom_provider without editing this skill.
        try:
            # Empty-string settings → None to the adapter → the
            # corresponding ``-m``/``--provider`` flag is omitted and the
            # profile's own ``config.yaml`` drives selection. This is the
            # documented path for using custom providers like
            # ``ollama-local`` that aren't valid ``--provider`` argparse
            # choices on the Hermes CLI.
            result = await orch.hermes.run(
                query,
                model=s.calendar_skill_model or None,
                provider=s.calendar_skill_provider or None,
                profile=s.calendar_skill_profile,
                preload_skills=[s.calendar_skill_preload],
                max_turns=s.calendar_skill_max_turns,
                timeout_ms=s.calendar_skill_timeout_ms,
            )
        except Exception as e:  # noqa: BLE001
            # Skills are expected to render their own user-facing error
            # strings rather than propagate raw exceptions to the user.
            return (
                f"⚠️ Calendar lookup failed: `{type(e).__name__}`\n"
                f"```\n{str(e)[:400]}\n```\n"
                f"Check that `hermes -p {s.calendar_skill_profile} chat` "
                f"runs manually and that Google OAuth is authenticated "
                f"(`python ~/.hermes/profiles/{s.calendar_skill_profile}"
                f"/skills/productivity/google-workspace/scripts/setup.py --check`)."
            )

        return result.text or "⚠️ Calendar profile returned an empty response."
