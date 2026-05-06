"""``/hybrid-budget`` — per-user cloud-token budget readout.

Shows how close the user is to the daily cap (R4). Useful for users to
decide "can I still !heavy today?" before burning the request.

Sources:
  - Daily spent: ``Repository.used_tokens_today(user_id)`` (R4 ledger)
  - Daily cap:   ``settings.cloud_token_budget_daily``
  - Session cap: ``settings.cloud_token_budget_session`` (informational)

When the Repository isn't wired (tests / CLI without --repo) the skill
still returns a readable message stating that the ledger is unavailable
rather than erroring out.
"""
from __future__ import annotations

import re

from .base import Skill, SkillContext, SkillMatch

_PATTERN = re.compile(r"^\s*/hybrid-budget\s*$", re.IGNORECASE)


class HybridBudgetSkill(Skill):
    name = "hybrid-budget"

    def match(self, message: str) -> SkillMatch | None:
        if _PATTERN.match(message):
            return SkillMatch(skill_name=self.name, args={})
        return None

    async def invoke(self, match: SkillMatch, ctx: SkillContext) -> str:
        cap_daily = ctx.settings.cloud_token_budget_daily

        if ctx.repo is None:
            return (
                "**hybrid-budget**\n"
                f"- daily cap          : {cap_daily:,} tokens\n"
                "_(repository not configured — live usage unavailable)_"
            )

        try:
            used_today = await ctx.repo.used_tokens_today(ctx.user_id)
        except Exception as e:  # noqa: BLE001
            return f"**hybrid-budget** — error reading ledger: `{type(e).__name__}`"

        pct = (used_today / cap_daily * 100.0) if cap_daily else 0.0
        remaining = max(0, cap_daily - used_today)
        return (
            "**hybrid-budget**\n"
            f"- daily used  : {used_today:,} / {cap_daily:,} tokens  ({pct:.1f}%)\n"
            f"- remaining   : {remaining:,} tokens"
        )
