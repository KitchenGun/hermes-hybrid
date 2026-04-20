"""``/memo`` — jot / list / clear short personal notes.

Syntax (all case-insensitive):
  ``/memo save <text>``   store a note (≤ 2000 chars)
  ``/memo list``          show up to 20 most recent notes
  ``/memo clear``         drop all notes

Notes are per-user and stored in the active :class:`MemoryBackend`
(InMemoryMemory by default — non-durable across bot restarts). Phase 3
is expected to migrate this to Hermes' native memory so the model
sees notes in-context instead of requiring an explicit ``/memo list``.
"""
from __future__ import annotations

import re

from src.memory import MemoryTooLarge

from .base import Skill, SkillContext, SkillMatch

_PATTERN = re.compile(
    r"^\s*/memo\s+(?P<verb>save|list|clear)(?:\s+(?P<text>.*))?\s*$",
    re.IGNORECASE | re.DOTALL,
)


class HybridMemoSkill(Skill):
    name = "hybrid-memo"

    def match(self, message: str) -> SkillMatch | None:
        m = _PATTERN.match(message)
        if m is None:
            return None
        args = {"verb": m.group("verb").lower()}
        if m.group("text"):
            args["text"] = m.group("text").strip()
        return SkillMatch(skill_name=self.name, args=args)

    async def invoke(self, match: SkillMatch, ctx: SkillContext) -> str:
        verb = match.args.get("verb", "")
        if verb == "save":
            text = match.args.get("text", "")
            if not text:
                return "Usage: `/memo save <text>`"
            try:
                memo = await ctx.memory.save(ctx.user_id, text)
            except MemoryTooLarge as e:
                return f"⚠️ memo too large: {e}"
            except ValueError as e:
                return f"⚠️ {e}"
            ts = memo.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            return f"📝 saved ({ts}): {_oneline(memo.text, 120)}"

        if verb == "list":
            memos = await ctx.memory.list_memos(ctx.user_id, limit=20)
            if not memos:
                return "_(no memos yet — try `/memo save <text>`)_"
            lines = ["**your memos**"]
            for i, memo in enumerate(memos, start=1):
                ts = memo.created_at.strftime("%m-%d %H:%M")
                lines.append(f"{i}. [{ts}] {_oneline(memo.text, 140)}")
            return "\n".join(lines)

        if verb == "clear":
            n = await ctx.memory.clear(ctx.user_id)
            return f"🧹 cleared {n} memo(s)" if n else "_(nothing to clear)_"

        return "Usage: `/memo save <text>` · `/memo list` · `/memo clear`"


def _oneline(s: str, limit: int) -> str:
    flat = s.replace("\n", " ").strip()
    return flat if len(flat) <= limit else flat[:limit] + "..."
