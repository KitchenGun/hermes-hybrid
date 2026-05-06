"""``/kanban`` — Discord-side CRUD for the cross-profile Kanban store.

Syntax (case-insensitive verb; arguments are space-separated, ``title``
and ``text`` may contain spaces — last argument soaks up the rest):

  ``/kanban list [tenant]``                  pending + in_progress tasks
  ``/kanban add <tenant> <title>``           create a todo task
  ``/kanban view <task_id>``                 task detail + comments
  ``/kanban comment <task_id> <text>``       append a review comment
  ``/kanban done <task_id>``                 mark complete
  ``/kanban cancel <task_id>``               mark cancelled

The ``task_id`` is a UUID — the bot displays the first 8 chars only,
but accepts any unique prefix on lookup so the user can type
``/kanban view abc12345`` instead of pasting the full id.

Why this lives next to other slash skills (and not as a profile job):
  * the Kanban store is process-local single-file JSON (Phase 6)
  * the bot's Discord process is the one that owns it
  * profile jobs that *write* to the store (advisor_ops kanban_create,
    installer_ops kanban_complete) are a separate code path (yaml
    prompts that shell out to ``python -m`` or hermes tools — outside
    this slash command's scope)
"""
from __future__ import annotations

import re

from src.core import KanbanStore, KanbanTask

from .base import Skill, SkillContext, SkillMatch


_PATTERN = re.compile(
    r"^\s*/kanban\s+(?P<verb>list|add|view|comment|done|cancel)"
    r"(?:\s+(?P<rest>.+))?\s*$",
    re.IGNORECASE | re.DOTALL,
)

_ID_PREFIX_LEN = 8


class KanbanSkill(Skill):
    name = "kanban"

    def match(self, message: str) -> SkillMatch | None:
        m = _PATTERN.match(message)
        if m is None:
            return None
        args = {"verb": m.group("verb").lower()}
        rest = m.group("rest")
        if rest:
            args["rest"] = rest.strip()
        return SkillMatch(skill_name=self.name, args=args)

    async def invoke(self, match: SkillMatch, ctx: SkillContext) -> str:
        verb = match.args.get("verb", "")
        rest = match.args.get("rest", "").strip()
        store = self._store(ctx)

        if verb == "list":
            return self._do_list(store, rest)
        if verb == "add":
            return self._do_add(store, rest, ctx.user_id)
        if verb == "view":
            return self._do_view(store, rest)
        if verb == "comment":
            return self._do_comment(store, rest, ctx.user_id)
        if verb == "done":
            return self._do_set_status(store, rest, "done")
        if verb == "cancel":
            return self._do_set_status(store, rest, "cancelled")
        return _USAGE

    # ---- helpers --------------------------------------------------

    @staticmethod
    def _store(ctx: SkillContext) -> KanbanStore:
        return KanbanStore(ctx.settings.kanban_store_path)

    @staticmethod
    def _do_list(store: KanbanStore, rest: str) -> str:
        tenant = rest.strip() or None
        # "open" = 사용자 액션이 필요한 모든 단계 (triage / todo / in_progress / review).
        # done / cancelled 는 list 에서 빠짐.
        triage = store.list(tenant=tenant, status="triage")
        todo = store.list(tenant=tenant, status="todo")
        in_prog = store.list(tenant=tenant, status="in_progress")
        review = store.list(tenant=tenant, status="review")
        all_open = triage + todo + in_prog + review
        if not all_open:
            scope = f"`{tenant}`" if tenant else "all tenants"
            return f"_({scope}: 진행 중 task 없음)_"
        lines = [f"**Kanban — {len(all_open)} open**"]
        for t in all_open:
            lines.append(_short(t))
        return "\n".join(lines)

    @staticmethod
    def _do_add(store: KanbanStore, rest: str, created_by: str) -> str:
        # First whitespace splits tenant from title; rest is the title
        # (allowed to contain spaces).
        parts = rest.split(maxsplit=1)
        if len(parts) < 2:
            return "Usage: `/kanban add <tenant> <title>`"
        tenant, title = parts[0], parts[1].strip()
        if not title:
            return "Usage: `/kanban add <tenant> <title>`"
        task = store.create(
            tenant=tenant,
            title=title,
            created_by=str(created_by),
        )
        return f"✅ added `{task.id[:_ID_PREFIX_LEN]}` ({task.tenant}): {_oneline(task.title, 100)}"

    def _do_view(self, store: KanbanStore, rest: str) -> str:
        task = self._find_by_prefix(store, rest)
        if isinstance(task, str):
            return task  # error message
        return _detail(task)

    def _do_comment(
        self, store: KanbanStore, rest: str, author: str
    ) -> str:
        parts = rest.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return "Usage: `/kanban comment <task_id> <text>`"
        task = self._find_by_prefix(store, parts[0])
        if isinstance(task, str):
            return task
        text = parts[1].strip()
        updated = store.comment(task.id, author=str(author), text=text)
        if updated is None:
            return f"⚠️ task `{parts[0]}` 이 사라졌습니다."
        return (
            f"💬 commented `{updated.id[:_ID_PREFIX_LEN]}` "
            f"({len(updated.comments)}개): {_oneline(text, 120)}"
        )

    def _do_set_status(
        self, store: KanbanStore, rest: str, status: str
    ) -> str:
        task = self._find_by_prefix(store, rest.strip())
        if isinstance(task, str):
            return task
        method = (
            store.complete if status == "done" else store.cancel
        )
        updated = method(task.id)
        if updated is None:
            return f"⚠️ task `{rest}` 이 사라졌습니다."
        emoji = "✅" if status == "done" else "🛑"
        return (
            f"{emoji} `{updated.id[:_ID_PREFIX_LEN]}` → **{status}** "
            f"({_oneline(updated.title, 80)})"
        )

    @staticmethod
    def _find_by_prefix(
        store: KanbanStore, raw_id: str
    ) -> KanbanTask | str:
        """Resolve a UUID prefix to a unique task. Returns the task on
        match, or an error message string on miss / ambiguity."""
        prefix = (raw_id or "").strip().lower()
        if not prefix:
            return "Usage: `/kanban view <task_id>` (앞 8자리만 입력해도 됩니다)"
        # First try exact id, then prefix.
        exact = store.get(prefix)
        if exact is not None:
            return exact
        candidates = [
            t for t in store.list() if t.id.lower().startswith(prefix)
        ]
        if not candidates:
            return f"⚠️ task `{raw_id}` 못 찾음."
        if len(candidates) > 1:
            ids = ", ".join(f"`{t.id[:_ID_PREFIX_LEN]}`" for t in candidates[:5])
            return f"⚠️ prefix `{raw_id}` 모호 — {len(candidates)}개 매치: {ids}"
        return candidates[0]


# ---- formatting -------------------------------------------------------


_USAGE = (
    "**Kanban**\n"
    "`/kanban list [tenant]`\n"
    "`/kanban add <tenant> <title>`\n"
    "`/kanban view <task_id>`\n"
    "`/kanban comment <task_id> <text>`\n"
    "`/kanban done <task_id>`\n"
    "`/kanban cancel <task_id>`"
)


def _short(t: KanbanTask) -> str:
    status_emoji = {
        "triage": "🆕",
        "todo": "⬜",
        "in_progress": "🔄",
        "review": "👀",
        "done": "✅",
        "cancelled": "🛑",
    }.get(t.status, "•")
    return (
        f"{status_emoji} `{t.id[:_ID_PREFIX_LEN]}` [{t.tenant}] "
        f"{_oneline(t.title, 80)}"
    )


def _detail(t: KanbanTask) -> str:
    lines = [
        f"**`{t.id[:_ID_PREFIX_LEN]}` — {t.title}**",
        f"tenant: `{t.tenant}` · status: **{t.status}**",
        f"created: {t.created_at} by `{t.created_by or '—'}`",
        f"updated: {t.updated_at}",
    ]
    if t.assigned_to:
        lines.append(f"assigned: `{t.assigned_to}`")
    if t.tags:
        lines.append("tags: " + ", ".join(f"`{tag}`" for tag in t.tags))
    if t.body:
        lines.append("")
        lines.append(t.body)
    if t.comments:
        lines.append("")
        lines.append(f"**comments ({len(t.comments)})**")
        for i, c in enumerate(t.comments, 1):
            lines.append(
                f"{i}. [{c.at}] `{c.author}`: {_oneline(c.text, 200)}"
            )
    return "\n".join(lines)


def _oneline(s: str, limit: int) -> str:
    flat = (s or "").replace("\n", " ").strip()
    return flat if len(flat) <= limit else flat[:limit] + "..."


__all__ = ["KanbanSkill"]
