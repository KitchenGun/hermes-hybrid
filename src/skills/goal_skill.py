"""``/goal`` — deterministic goal → Kanban task decomposition (P3 mini).

Initial scope: parse the message into ``(flags, goal_text)``, hand it
to :func:`src.orchestrator.goal_planner.plan_goal`, and either preview
the plan (``--dry-run``) or persist each task to Kanban as ``ready``.
The skill itself never executes a task — workers pick up via the
existing dispatcher path.

Supported flags (anywhere before the goal text):
  * ``--dry-run`` — print plan, no DB write
  * ``--workspace scratch`` or ``--workspace dir:<absolute-path>``
  * ``--max-retries N`` (clamped [0, 10] inside ``plan_goal``)

Examples (from the user-facing usage doc):
    /goal 인스타 자동화 파이프라인 만들기
    /goal --dry-run 성장하는 에이전트 구조 개선
    /goal --workspace dir:E:\\hermes-workspaces\\instagram-digest 인스타 자동화 파이프라인 만들기
    /goal --max-retries 5 Discord session auto-resume 고도화
"""
from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from src.core.kanban import KanbanDB
from src.core.kanban.workspace import WorkspaceError, parse_workspace_spec

from .base import Skill, SkillContext, SkillMatch

if TYPE_CHECKING:  # pragma: no cover
    # Lazy at runtime to avoid the
    #   src.skills → src.orchestrator → IntentRouter → src.skills
    # circular import. ``src/orchestrator/__init__.py`` pulls Orchestrator,
    # which transitively imports the skills registry.
    from src.orchestrator.goal_planner import GoalPlan, GoalTask


_PATTERN = re.compile(
    r"^\s*/goal(?:\s+(?P<rest>.+))?\s*$",
    re.IGNORECASE | re.DOTALL,
)


_USAGE = (
    "**Goal**\n"
    "`/goal <자연어 목표>`\n"
    "옵션: `--dry-run`, `--workspace scratch|dir:<absolute-path>`, "
    "`--max-retries N` (0~10)"
)


class GoalSkill(Skill):
    name = "goal"

    def match(self, message: str) -> SkillMatch | None:
        m = _PATTERN.match(message)
        if m is None:
            return None
        rest = (m.group("rest") or "").strip()
        return SkillMatch(skill_name=self.name, args={"rest": rest})

    async def invoke(self, match: SkillMatch, ctx: SkillContext) -> str:
        # Lazy import (see TYPE_CHECKING note above).
        from src.orchestrator.goal_planner import (
            GoalPlannerError,
            plan_goal,
        )

        rest = match.args.get("rest", "").strip()
        if not rest:
            return _USAGE
        try:
            dry_run, workspace, max_retries, goal_text = _parse_args(rest)
        except (GoalPlannerError, WorkspaceError) as e:
            return f"⚠️ {e}"
        if not goal_text:
            return _USAGE
        try:
            plan = plan_goal(
                goal_text=goal_text,
                workspace=workspace,
                max_retries=max_retries,
            )
        except GoalPlannerError as e:
            return f"⚠️ {e}"

        if dry_run:
            return _format_plan(plan, dry_run=True, created_ids=[])

        # Real mode: persist each task into Kanban as ``ready``.
        db = KanbanDB(
            ctx.settings.kanban_db_path,
            workspaces_root=ctx.settings.kanban_workspaces_root,
        )
        await db.migrate()
        try:
            ws_kind, ws_path = parse_workspace_spec(workspace or "scratch")
        except WorkspaceError as e:
            return f"⚠️ {e}"

        created_ids: list[str] = []
        for gt in plan.tasks:
            task = await db.create_task(
                title=gt.title,
                assignee=gt.suggested_profile,
                body=_render_body(gt),
                status="ready",
                priority=gt.priority,
                workspace_kind=ws_kind,
                workspace_path=ws_path,
                max_retries=gt.max_retries,
                created_by=str(getattr(ctx, "user_id", "goal-skill")),
            )
            created_ids.append(task.id)
        return _format_plan(plan, dry_run=False, created_ids=created_ids)


# ---- arg parsing ------------------------------------------------------


def _parse_args(rest: str) -> tuple[bool, str | None, int, str]:
    """Return ``(dry_run, workspace_spec_or_None, max_retries, goal_text)``.

    ``--max-retries`` defaults to 3. ``--workspace`` is validated here
    via :func:`parse_workspace_spec` so a relative ``dir:`` path raises
    ``WorkspaceError`` before we even build the plan.
    """
    # Lazy import — see TYPE_CHECKING block at module top.
    from src.orchestrator.goal_planner import GoalPlannerError

    # ``posix=False`` so Windows-style ``dir:C:\path`` keeps its
    # backslashes (POSIX mode treats them as escape chars and drops them).
    # Quoted segments still work — quotes are simply preserved in tokens.
    try:
        tokens = shlex.split(rest, posix=False)
    except ValueError:
        tokens = rest.split()
    dry_run = False
    workspace: str | None = None
    max_retries = 3
    leftover: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--dry-run":
            dry_run = True
            i += 1
            continue
        if tok == "--workspace":
            if i + 1 >= len(tokens):
                raise GoalPlannerError("--workspace requires a value")
            workspace = tokens[i + 1]
            i += 2
            continue
        if tok == "--max-retries":
            if i + 1 >= len(tokens):
                raise GoalPlannerError("--max-retries requires a value")
            try:
                max_retries = int(tokens[i + 1])
            except ValueError:
                raise GoalPlannerError(
                    f"--max-retries must be int, got {tokens[i + 1]!r}"
                ) from None
            i += 2
            continue
        leftover.append(tok)
        i += 1
    goal_text = " ".join(leftover).strip()
    if workspace:
        # Raises WorkspaceError on invalid spec — bubble up to invoke().
        parse_workspace_spec(workspace)
    return dry_run, workspace, max_retries, goal_text


# ---- formatting -------------------------------------------------------


def _render_body(gt) -> str:  # gt: GoalTask (forward-ref'd)
    lines = [gt.description, "", "**수용 기준**"]
    for c in gt.acceptance_criteria:
        lines.append(f"- {c}")
    return "\n".join(lines)


def _format_plan(
    plan,
    *,
    dry_run: bool,
    created_ids: list[str],
) -> str:  # plan: GoalPlan
    if dry_run:
        header = f"🧭 **Goal Plan** (--dry-run, no task created · {len(plan.tasks)})"
    else:
        header = f"🧭 **Goal Plan** ({len(created_ids)} task(s) created)"
    lines = [header, f"_{plan.goal_title}_", ""]
    for idx, t in enumerate(plan.tasks, 1):
        tag = ""
        if not dry_run and idx <= len(created_ids):
            tag = f" `{created_ids[idx - 1][:8]}`"
        lines.append(f"**{idx}.** {t.title}{tag}")
        lines.append(
            f"   profile: `{t.suggested_profile}` · priority: {t.priority} "
            f"· workspace: `{t.workspace}` · max_retries: {t.max_retries}"
        )
        if t.acceptance_criteria:
            lines.append(f"   AC: {', '.join(t.acceptance_criteria)}")
    if dry_run:
        lines.append("")
        lines.append(
            "_같은 메시지에서 `--dry-run` 을 빼고 다시 보내면 위 task 들이 "
            "Kanban 에 등록됩니다._"
        )
    elif created_ids:
        lines.append("")
        lines.append(
            f"다음 실행 후보: `{created_ids[0][:8]}` "
            "(`/kanban claim` 으로 진입 또는 dispatcher 가 자동 픽업)"
        )
    return "\n".join(lines)


__all__ = ["GoalSkill"]
