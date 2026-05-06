"""``/kanban`` — Discord-side surface for the SQLite-backed Kanban store.

Phase 2-A (Nous Hermes Agent 모델 정렬, 2026-05-07).

Verbs:
  /kanban list [--tenant X] [--status Y] [--assignee Z]
  /kanban show <task_id|prefix>
  /kanban create <assignee> <title> [--body ...] [--schedule ISO] [--priority N]
                 [--tenant X] [--idem KEY] [--parent ID]...
  /kanban comment <task_id> <body...>
  /kanban complete <task_id> [<summary...>]
  /kanban block <task_id> <reason...>
  /kanban unblock <task_id>
  /kanban runs <task_id>
  /kanban link <parent_id> <child_id>
  /kanban archive <task_id>

User-driven complete/block transition the status directly even when no
worker run is active. Worker-driven complete/block (with run termination)
goes through ``scripts/kanban_cli.py`` from a worker subprocess.
"""
from __future__ import annotations

import re
import shlex

from src.core.kanban import KanbanDB, KanbanTask
from src.core.kanban.tools import (
    KanbanToolError,
    kanban_comment as tool_comment,
    kanban_create as tool_create,
    kanban_link as tool_link,
)

from .base import Skill, SkillContext, SkillMatch


_PATTERN = re.compile(
    r"^\s*/kanban\s+"
    r"(?P<verb>list|show|create|comment|complete|block|unblock|runs|link|archive)"
    r"(?:\s+(?P<rest>.+))?\s*$",
    re.IGNORECASE | re.DOTALL,
)

_ID_PREFIX_LEN = 8


def _parse_flags(text: str) -> tuple[list[str], dict[str, list[str]]]:
    """Lightweight `pos1 pos2 --flag1 v1 --flag2 v2` splitter.

    ``--flag`` repeats accumulate (e.g. multiple ``--parent``).
    """
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = text.split()
    positional: list[str] = []
    flags: dict[str, list[str]] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            key = tok[2:]
            i += 1
            val = tokens[i] if i < len(tokens) else ""
            flags.setdefault(key, []).append(val)
            i += 1
        else:
            positional.append(tok)
            i += 1
    return positional, flags


class KanbanSkill(Skill):
    name = "kanban"

    def match(self, message: str) -> SkillMatch | None:
        m = _PATTERN.match(message)
        if m is None:
            return None
        args: dict[str, str] = {"verb": m.group("verb").lower()}
        if m.group("rest"):
            args["rest"] = m.group("rest").strip()
        return SkillMatch(skill_name=self.name, args=args)

    async def invoke(self, match: SkillMatch, ctx: SkillContext) -> str:
        verb = match.args.get("verb", "")
        rest = match.args.get("rest", "").strip()
        db = await self._db(ctx)
        try:
            if verb == "list":
                return await self._do_list(db, rest)
            if verb == "show":
                return await self._do_show(db, rest)
            if verb == "create":
                return await self._do_create(db, rest, ctx.user_id)
            if verb == "comment":
                return await self._do_comment(db, rest, ctx.user_id)
            if verb == "complete":
                return await self._do_complete(db, rest)
            if verb == "block":
                return await self._do_block(db, rest)
            if verb == "unblock":
                return await self._do_unblock(db, rest)
            if verb == "runs":
                return await self._do_runs(db, rest)
            if verb == "link":
                return await self._do_link(db, rest)
            if verb == "archive":
                return await self._do_archive(db, rest)
        except KanbanToolError as e:
            return f"⚠️ {e}"
        return _USAGE

    @staticmethod
    async def _db(ctx: SkillContext) -> KanbanDB:
        db = KanbanDB(
            ctx.settings.kanban_db_path,
            workspaces_root=ctx.settings.kanban_workspaces_root,
        )
        await db.migrate()
        return db

    async def _do_list(self, db: KanbanDB, rest: str) -> str:
        positional, flags = _parse_flags(rest)
        tenant = (flags.get("tenant") or [None])[0]
        status = (flags.get("status") or [None])[0]
        assignee = (flags.get("assignee") or [None])[0]
        if not flags and positional:
            tenant = positional[0]
        if status:
            tasks = await db.list_tasks(
                status=status, tenant=tenant, assignee=assignee
            )
        else:
            triage = await db.list_tasks(
                status="triage", tenant=tenant, assignee=assignee
            )
            todo = await db.list_tasks(
                status="todo", tenant=tenant, assignee=assignee
            )
            ready = await db.list_tasks(
                status="ready", tenant=tenant, assignee=assignee
            )
            running = await db.list_tasks(
                status="running", tenant=tenant, assignee=assignee
            )
            blocked = await db.list_tasks(
                status="blocked", tenant=tenant, assignee=assignee
            )
            tasks = triage + todo + ready + running + blocked
        if not tasks:
            scope = (
                f"`{tenant or status or assignee}`"
                if (tenant or status or assignee)
                else "all"
            )
            return f"_({scope}: 진행 중 task 없음)_"
        lines = [f"**Kanban — {len(tasks)} open**"]
        for t in tasks:
            lines.append(_short(t))
        return "\n".join(lines)

    async def _do_show(self, db: KanbanDB, rest: str) -> str:
        task = await self._resolve(db, rest)
        if isinstance(task, str):
            return task
        parents = await db.parents_of(task.id)
        runs = await db.list_runs(task.id)
        comments = await db.list_comments(task.id)
        return _detail(
            task, parents=parents, runs=runs, comments=comments
        )

    async def _do_create(
        self, db: KanbanDB, rest: str, created_by: str
    ) -> str:
        positional, flags = _parse_flags(rest)
        if len(positional) < 2:
            return (
                "Usage: `/kanban create <assignee> <title> "
                "[--body ...] [--schedule ISO] [--priority N] "
                "[--tenant X] [--idem KEY] [--parent ID]...`"
            )
        assignee = positional[0]
        title = " ".join(positional[1:])
        body = (flags.get("body") or [""])[0]
        schedule = (flags.get("schedule") or [None])[0]
        priority_raw = (flags.get("priority") or ["0"])[0]
        try:
            priority = int(priority_raw)
        except ValueError:
            return f"⚠️ priority must be int, got `{priority_raw}`"
        tenant = (flags.get("tenant") or [None])[0]
        idem = (flags.get("idem") or [None])[0]
        parents = flags.get("parent") or None
        out = await tool_create(
            db,
            title=title, assignee=assignee, body=body,
            parents=parents, priority=priority, tenant=tenant,
            idempotency_key=idem, scheduled_at=schedule,
            created_by=str(created_by),
        )
        return (
            f"✅ created `{out['task_id'][:_ID_PREFIX_LEN]}` "
            f"({out['assignee']}, status: {out['status']}): "
            f"{_oneline(title, 100)}"
        )

    async def _do_comment(
        self, db: KanbanDB, rest: str, author: str
    ) -> str:
        parts = rest.split(maxsplit=1)
        if len(parts) < 2:
            return "Usage: `/kanban comment <task_id> <body>`"
        task = await self._resolve(db, parts[0])
        if isinstance(task, str):
            return task
        body = parts[1].strip()
        await tool_comment(
            db, task_id=task.id, body=body, author=str(author)
        )
        return (
            f"💬 commented `{task.id[:_ID_PREFIX_LEN]}`: "
            f"{_oneline(body, 120)}"
        )

    async def _do_complete(self, db: KanbanDB, rest: str) -> str:
        parts = rest.split(maxsplit=1)
        if not parts:
            return "Usage: `/kanban complete <task_id> [<summary>]`"
        task = await self._resolve(db, parts[0])
        if isinstance(task, str):
            return task
        summary = parts[1].strip() if len(parts) > 1 else None
        if task.current_run_id:
            await db.end_run(
                task.current_run_id, outcome="completed", summary=summary
            )
        await db.set_status(task.id, "done", actor="human", reason=summary)
        return (
            f"✅ `{task.id[:_ID_PREFIX_LEN]}` → **done** "
            f"({_oneline(task.title, 80)})"
        )

    async def _do_block(self, db: KanbanDB, rest: str) -> str:
        parts = rest.split(maxsplit=1)
        if len(parts) < 2:
            return "Usage: `/kanban block <task_id> <reason>`"
        task = await self._resolve(db, parts[0])
        if isinstance(task, str):
            return task
        reason = parts[1].strip()
        if task.current_run_id:
            await db.end_run(
                task.current_run_id, outcome="blocked", error=reason
            )
        await db.set_status(
            task.id, "blocked", actor="human", reason=reason
        )
        return (
            f"🛑 `{task.id[:_ID_PREFIX_LEN]}` → **blocked** "
            f"({_oneline(reason, 80)})"
        )

    async def _do_unblock(self, db: KanbanDB, rest: str) -> str:
        task = await self._resolve(db, rest.strip())
        if isinstance(task, str):
            return task
        parents = await db.parents_of(task.id)
        if parents and not all(p.status == "done" for p in parents):
            new_status = "todo"
        else:
            new_status = "ready"
        await db.set_status(task.id, new_status, actor="human")
        return f"🔓 `{task.id[:_ID_PREFIX_LEN]}` → **{new_status}**"

    async def _do_runs(self, db: KanbanDB, rest: str) -> str:
        task = await self._resolve(db, rest.strip())
        if isinstance(task, str):
            return task
        runs = await db.list_runs(task.id)
        if not runs:
            return f"_(`{task.id[:_ID_PREFIX_LEN]}` 에 실행 이력 없음)_"
        lines = [
            f"**Runs for `{task.id[:_ID_PREFIX_LEN]}` — {task.title}**"
        ]
        for i, r in enumerate(runs, 1):
            outcome = r.outcome or "running"
            extra = ""
            if r.summary:
                extra += f" · {_oneline(r.summary, 80)}"
            if r.error:
                extra += f" · err: {_oneline(r.error, 60)}"
            lines.append(
                f"{i}. `{r.id[:_ID_PREFIX_LEN]}` — {outcome}{extra}"
            )
        return "\n".join(lines)

    async def _do_link(self, db: KanbanDB, rest: str) -> str:
        parts = rest.split()
        if len(parts) < 2:
            return "Usage: `/kanban link <parent_id> <child_id>`"
        parent = await self._resolve(db, parts[0])
        child = await self._resolve(db, parts[1])
        if isinstance(parent, str):
            return parent
        if isinstance(child, str):
            return child
        await tool_link(db, parent_id=parent.id, child_id=child.id)
        return (
            f"🔗 linked `{parent.id[:_ID_PREFIX_LEN]}` → "
            f"`{child.id[:_ID_PREFIX_LEN]}`"
        )

    async def _do_archive(self, db: KanbanDB, rest: str) -> str:
        task = await self._resolve(db, rest.strip())
        if isinstance(task, str):
            return task
        await db.set_status(task.id, "archived", actor="human")
        return f"📦 `{task.id[:_ID_PREFIX_LEN]}` → **archived**"

    @staticmethod
    async def _resolve(db: KanbanDB, raw_id: str) -> KanbanTask | str:
        prefix = (raw_id or "").strip().lower()
        if not prefix:
            return "task_id 필요. 앞 8자 prefix 도 OK."
        exact = await db.get_task(prefix)
        if exact is not None:
            return exact
        all_tasks = await db.list_tasks(include_archived=True)
        candidates = [
            t for t in all_tasks if t.id.lower().startswith(prefix)
        ]
        if not candidates:
            return f"⚠️ task `{raw_id}` 못 찾음."
        if len(candidates) > 1:
            ids = ", ".join(
                f"`{t.id[:_ID_PREFIX_LEN]}`" for t in candidates[:5]
            )
            return f"⚠️ prefix `{raw_id}` 모호 — {len(candidates)}개 매치: {ids}"
        return candidates[0]


_USAGE = (
    "**Kanban**\n"
    "`/kanban list [--tenant X] [--status Y] [--assignee Z]`\n"
    "`/kanban show <task_id>`\n"
    "`/kanban create <assignee> <title> [flags...]`\n"
    "`/kanban comment <task_id> <body>`\n"
    "`/kanban complete <task_id> [<summary>]`\n"
    "`/kanban block <task_id> <reason>`\n"
    "`/kanban unblock <task_id>`\n"
    "`/kanban runs <task_id>`\n"
    "`/kanban link <parent_id> <child_id>`\n"
    "`/kanban archive <task_id>`"
)

_STATUS_EMOJI = {
    "triage": "🆕",
    "todo": "⬜",
    "ready": "🟢",
    "running": "🔄",
    "blocked": "🛑",
    "done": "✅",
    "archived": "📦",
}


def _short(t: KanbanTask) -> str:
    emoji = _STATUS_EMOJI.get(t.status, "•")
    sched = f" (⏲ {t.scheduled_at})" if t.scheduled_at else ""
    return (
        f"{emoji} `{t.id[:_ID_PREFIX_LEN]}` "
        f"[{t.tenant or '—'}] ({t.assignee or '—'}) "
        f"{_oneline(t.title, 70)}{sched}"
    )


def _detail(t: KanbanTask, *, parents=None, runs=None, comments=None) -> str:
    lines = [
        f"**`{t.id[:_ID_PREFIX_LEN]}` — {t.title}**",
        (
            f"status: **{t.status}** · assignee: `{t.assignee or '—'}` "
            f"· tenant: `{t.tenant or '—'}`"
        ),
        f"created: {t.created_at} by `{t.created_by or '—'}`",
        f"updated: {t.updated_at}",
    ]
    if t.scheduled_at:
        lines.append(f"scheduled_at: {t.scheduled_at}")
    if t.priority:
        lines.append(f"priority: {t.priority}")
    if t.body:
        lines.append("")
        lines.append(t.body)
    if parents:
        lines.append("")
        lines.append("**Parents**")
        for p in parents:
            lines.append(
                f"- `{p.id[:_ID_PREFIX_LEN]}` [{p.status}] "
                f"{_oneline(p.title, 70)}"
            )
    if runs:
        lines.append("")
        lines.append(f"**Runs ({len(runs)})**")
        for i, r in enumerate(runs, 1):
            outcome = r.outcome or "running"
            lines.append(
                f"{i}. `{r.id[:_ID_PREFIX_LEN]}` — {outcome}"
            )
            if r.summary:
                lines.append(f"   {_oneline(r.summary, 200)}")
    if comments:
        lines.append("")
        lines.append(f"**Comments ({len(comments)})**")
        for c in comments:
            lines.append(
                f"- [{c.created_at}] `{c.author}`: "
                f"{_oneline(c.body, 200)}"
            )
    return "\n".join(lines)


def _oneline(s: str | None, limit: int) -> str:
    if not s:
        return ""
    flat = s.replace("\n", " ").strip()
    return flat if len(flat) <= limit else flat[:limit] + "..."


__all__ = ["KanbanSkill"]
