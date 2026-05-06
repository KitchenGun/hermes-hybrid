"""``/hybrid-status`` — snapshot of the orchestrator's live state.

Returns a compact multiline block with:
  - master_enabled (gpt-5.5 via opencode CLI)
  - ollama_enabled (ollama lane for cron / Hermes profile sub-calls)
  - require_allowlist (gateway fail-closed flag)
  - Registered skill count + names

Users see this when troubleshooting. Dev-facing skill; output format is
tuned for readability over a machine-readable contract.
"""
from __future__ import annotations

import re

from .base import Skill, SkillContext, SkillMatch

_PATTERN = re.compile(r"^\s*/hybrid-status\s*$", re.IGNORECASE)


class HybridStatusSkill(Skill):
    name = "hybrid-status"

    def match(self, message: str) -> SkillMatch | None:
        if _PATTERN.match(message):
            return SkillMatch(skill_name=self.name, args={})
        return None

    async def invoke(self, match: SkillMatch, ctx: SkillContext) -> str:
        s = ctx.settings
        lines = [
            "**hybrid-status**",
            f"- master_enabled        : {getattr(s, 'master_enabled', False)}",
            f"- master_model          : {getattr(s, 'master_model', '—')}",
            f"- ollama_enabled        : {s.ollama_enabled}",
            f"- memory_inject_enabled : {getattr(s, 'memory_inject_enabled', False)}",
            f"- require_allowlist     : {s.require_allowlist}",
        ]
        if ctx.orchestrator is not None:
            reg = getattr(ctx.orchestrator, "skills", None)
            if reg is not None:
                lines.append(
                    f"- skills registered     : {len(reg)} "
                    f"({', '.join(reg.names())})"
                )
        return "\n".join(lines)
