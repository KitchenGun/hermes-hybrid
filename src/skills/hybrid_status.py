"""``/hybrid-status`` — snapshot of the orchestrator's live state.

Returns a compact multiline block with:
  - Flag state (USE_HERMES_FOR_LOCAL, USE_HERMES_FOR_C1, ollama_enabled)
  - Registered skill count + names
  - Heavy-session registry size
  - Recent task status from the repo (if available)

Users see this when troubleshooting ("did the flag flip?"). It's a
dev-facing skill; output format is tuned for readability over a
machine-readable contract.
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
            f"- ollama_enabled        : {s.ollama_enabled}",
            f"- use_hermes_for_local  : {getattr(s, 'use_hermes_for_local', False)}"
            f" (eff: {getattr(s, 'effective_use_hermes_for_local', False)})",
            f"- use_hermes_for_c1     : {getattr(s, 'use_hermes_for_c1', False)}"
            f" (eff: {getattr(s, 'effective_use_hermes_for_c1', False)})",
            f"- use_hermes_for_heavy  : {getattr(s, 'use_hermes_for_heavy', False)}"
            f" (eff: {getattr(s, 'effective_use_hermes_for_heavy', False)})",
            f"- use_hermes_everywhere : {getattr(s, 'use_hermes_everywhere', False)}",
            f"- trust_hermes_reflect  : {getattr(s, 'trust_hermes_reflection', False)}",
            f"- require_allowlist     : {s.require_allowlist}",
        ]
        if ctx.orchestrator is not None:
            reg = getattr(ctx.orchestrator, "skills", None)
            if reg is not None:
                lines.append(f"- skills registered     : {len(reg)} ({', '.join(reg.names())})")
            hs = getattr(ctx.orchestrator, "heavy_sessions", None)
            if hs is not None:
                lines.append(f"- heavy sessions active : {hs.size()}")
        return "\n".join(lines)
