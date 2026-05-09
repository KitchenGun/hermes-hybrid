#!/usr/bin/env python3
"""CLI front for the SQLite Kanban store (Phase 2-A, Nous Hermes Agent 정렬).

Used by:
  - human ad-hoc operations
  - master Claude CLI worker subprocess (worker mode invokes via terminal tool)

Verbs (JSON output by default; ``--text`` gives human-readable output):
  list / show / create / comment / complete / block / unblock /
  heartbeat / runs / link / archive

Worker convention: ``HERMES_KANBAN_TASK`` env var supplies the task_id when
none is passed positionally. Workers should call:
  show               → read task + worker_context
  heartbeat          → refresh claim TTL
  complete / block   → end the run

Exit codes:
  0  success
  1  not found / tool error
  2  invalid arguments
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from src.config import Settings  # noqa: E402
from src.core.kanban import (  # noqa: E402
    InvalidSlugError,
    KanbanDB,
    KanbanTask,
    cleanup_scratch_workspace,
    normalize_board_slug,
)
from src.core.kanban.workspace import (  # noqa: E402
    WorkspaceError,
    parse_workspace_spec,
)
from src.core.kanban.dispatcher import KanbanDispatcher  # noqa: E402
from src.core.kanban.tools import (  # noqa: E402
    KanbanToolError,
    kanban_block,
    kanban_comment,
    kanban_complete,
    kanban_create,
    kanban_heartbeat,
    kanban_link,
    kanban_show,
)


def _parse_csv(raw: str | None) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


async def _no_spawn(task):
    """Placeholder spawn_runner — used by `dispatch` CLI verb where we
    only want to inspect / promote, not actually fork a worker."""
    raise RuntimeError(
        "kanban dispatch CLI verb does not spawn workers; use --dry-run "
        "or run the gateway for real worker spawn"
    )


async def _handle_boards(db: KanbanDB, args, *, as_json: bool) -> int:
    cmd = args.boards_cmd
    if cmd in ("list", "ls"):
        boards = await db.list_boards(include_archived=args.archived)
        _emit(
            [b.model_dump(mode="json") for b in boards],
            as_json=as_json,
        )
        return 0
    if cmd == "create":
        try:
            board = await db.create_board(
                args.slug, name=args.name, icon=args.icon,
                description=args.description,
            )
        except (InvalidSlugError, ValueError) as e:
            _emit({"error": str(e)}, as_json=as_json)
            return 1
        if args.switch:
            db.set_current_board(args.slug)
        _emit(board.model_dump(mode="json"), as_json=as_json)
        return 0
    if cmd in ("switch", "use"):
        try:
            slug = normalize_board_slug(args.slug)
        except InvalidSlugError as e:
            _emit({"error": str(e)}, as_json=as_json)
            return 1
        if await db.get_board(slug) is None:
            _emit({"error": f"board {slug!r} not found"}, as_json=as_json)
            return 1
        db.set_current_board(slug)
        _emit({"current": slug}, as_json=as_json)
        return 0
    if cmd in ("show", "current"):
        cur = db.get_current_board()
        b = await db.get_board(cur)
        if b is None:
            _emit(
                {"current": cur, "exists": False}, as_json=as_json,
            )
            return 0
        _emit(
            {**b.model_dump(mode="json"), "is_current": True},
            as_json=as_json,
        )
        return 0
    if cmd == "rename":
        try:
            slug = normalize_board_slug(args.slug)
        except InvalidSlugError as e:
            _emit({"error": str(e)}, as_json=as_json)
            return 1
        b = await db.rename_board(slug, args.name)
        if b is None:
            _emit({"error": f"board {slug!r} not found"}, as_json=as_json)
            return 1
        _emit(b.model_dump(mode="json"), as_json=as_json)
        return 0
    if cmd == "rm":
        try:
            slug = normalize_board_slug(args.slug)
        except InvalidSlugError as e:
            _emit({"error": str(e)}, as_json=as_json)
            return 1
        if args.delete:
            ok = await db.hard_delete_board(slug)
            verb = "deleted"
        else:
            ok = await db.archive_board(slug)
            verb = "archived"
        if not ok:
            _emit(
                {"error": (
                    f"could not {verb} {slug!r} "
                    "(default board can't be removed, or already gone)"
                )},
                as_json=as_json,
            )
            return 1
        _emit({"slug": slug, "status": verb}, as_json=as_json)
        return 0
    return 2


async def _handle_tail(db: KanbanDB, args, *, as_json: bool) -> int:
    since = args.since
    if since is None:
        since = await db.latest_event_id()
    while True:
        events = await db.list_events_since(
            since, task_id=args.task, limit=200,
        )
        for ev in events:
            if as_json:
                print(
                    json.dumps(ev.model_dump(mode="json"), ensure_ascii=False),
                    flush=True,
                )
            else:
                print(
                    f"#{ev.id} [{ev.created_at}] {ev.task_id} "
                    f"{ev.kind} {ev.actor or ''}",
                    flush=True,
                )
            since = max(since, ev.id)
        if args.once:
            return 0
        try:
            await asyncio.sleep(args.interval)
        except (asyncio.CancelledError, KeyboardInterrupt):
            return 0


def _emit(obj, *, as_json: bool) -> None:
    if as_json:
        if isinstance(obj, KanbanTask):
            print(json.dumps(obj.model_dump(mode="json"), ensure_ascii=False))
        elif isinstance(obj, list):
            print(json.dumps([
                t.model_dump(mode="json") if hasattr(t, "model_dump") else t
                for t in obj
            ], ensure_ascii=False))
        elif isinstance(obj, dict):
            print(json.dumps(obj, ensure_ascii=False))
        elif obj is None:
            print("null")
        else:
            print(json.dumps(obj, ensure_ascii=False))
        return

    # text mode
    if isinstance(obj, KanbanTask):
        print(
            f"{obj.id}\t{obj.tenant or '-'}\t{obj.status}\t"
            f"{obj.assignee or '-'}\t{obj.title}"
        )
    elif isinstance(obj, list):
        for t in obj:
            if isinstance(t, KanbanTask):
                print(
                    f"{t.id}\t{t.tenant or '-'}\t{t.status}\t"
                    f"{t.assignee or '-'}\t{t.title}"
                )
            elif isinstance(t, dict):
                print(json.dumps(t, ensure_ascii=False))
            else:
                print(t)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            print(f"{k}: {v}")
    elif obj is None:
        print("(none)")
    else:
        print(obj)


async def _amain(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=None, help="Override kanban_db_path")
    p.add_argument(
        "--workspaces", type=Path, default=None,
        help="Override kanban_workspaces_root",
    )
    p.add_argument(
        "--board", default=None,
        help="Board slug (default: current pointer or 'default')",
    )
    p.add_argument("--text", action="store_true", help="Text output (default JSON)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Initialize Kanban DB schema (idempotent)")

    # ---- boards subcommand ----
    p_boards = sub.add_parser("boards", help="Board management")
    boards_sub = p_boards.add_subparsers(dest="boards_cmd", required=True)

    bp_list = boards_sub.add_parser("list", aliases=["ls"], help="List boards")
    bp_list.add_argument("--archived", action="store_true")

    bp_create = boards_sub.add_parser("create", help="Create a new board")
    bp_create.add_argument("slug")
    bp_create.add_argument("--name", default=None)
    bp_create.add_argument("--icon", default=None)
    bp_create.add_argument("--description", default=None)
    bp_create.add_argument(
        "--switch", action="store_true",
        help="Set as current board after create",
    )

    bp_switch = boards_sub.add_parser(
        "switch", aliases=["use"], help="Set current board",
    )
    bp_switch.add_argument("slug")

    boards_sub.add_parser(
        "show", aliases=["current"], help="Show current board info",
    )

    bp_rename = boards_sub.add_parser(
        "rename", help="Rename board display name (slug stays)",
    )
    bp_rename.add_argument("slug")
    bp_rename.add_argument("name")

    bp_rm = boards_sub.add_parser(
        "rm", help="Archive a board (or hard delete with --delete)",
    )
    bp_rm.add_argument("slug")
    bp_rm.add_argument(
        "--delete", action="store_true",
        help="Permanently delete (cascades all tasks); irreversible",
    )

    # ---- tail ----
    p_tail = sub.add_parser(
        "tail", help="Stream task events (Ctrl+C to stop)",
    )
    p_tail.add_argument("--task", default=None, help="Filter by task id")
    p_tail.add_argument(
        "--since", type=int, default=None,
        help="Start from event id (default: current latest)",
    )
    p_tail.add_argument(
        "--interval", type=float, default=1.0,
        help="Polling interval in seconds",
    )
    p_tail.add_argument(
        "--once", action="store_true",
        help="Print events since --since and exit (no loop)",
    )

    p_list = sub.add_parser("list", help="List tasks")
    p_list.add_argument("--tenant")
    p_list.add_argument("--status")
    p_list.add_argument("--assignee")

    p_show = sub.add_parser("show", help="Show task + worker_context")
    p_show.add_argument(
        "task_id", nargs="?", default=None,
        help="default: $HERMES_KANBAN_TASK",
    )

    p_create = sub.add_parser("create", help="Create a task")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--assignee", required=True)
    p_create.add_argument("--body", default="")
    p_create.add_argument("--tenant", default=None)
    p_create.add_argument("--priority", type=int, default=0)
    p_create.add_argument("--scheduled-at", default=None)
    p_create.add_argument(
        "--max-runtime-seconds", "--max-runtime",
        type=int, default=None, dest="max_runtime_seconds",
    )
    p_create.add_argument(
        "--idem", "--idempotency-key",
        default=None, dest="idempotency_key",
    )
    p_create.add_argument(
        "--parents", default="", help="comma-separated task ids",
    )
    p_create.add_argument(
        "--skill", action="append", default=[], dest="skills",
        help="Per-task skill (repeatable) — exposed via $HERMES_KANBAN_SKILLS",
    )
    p_create.add_argument(
        "--triage", action="store_true",
        help="Force initial status to triage (parking lot for rough ideas)",
    )
    p_create.add_argument(
        "--workspace", default="scratch",
        help=(
            "Workspace spec: 'scratch' or 'dir:<absolute-path>'. "
            "scratch = fresh tmp per task; dir = shared persistent directory."
        ),
    )
    p_create.add_argument(
        "--max-retries", type=int, default=3, dest="max_retries",
        help=(
            "Per-task retry budget for incomplete-exit (TTL/crash). "
            "Default 3. When exhausted dispatcher auto-blocks instead of "
            "cycling forever."
        ),
    )
    p_create.add_argument("--created-by", default="cli")

    p_comm = sub.add_parser("comment", help="Append a comment")
    p_comm.add_argument("task_id")
    p_comm.add_argument("--author", default="cli")
    p_comm.add_argument("--body", required=True)

    p_done = sub.add_parser(
        "complete", help="Mark current run completed (worker tool)",
    )
    p_done.add_argument("task_id", nargs="?", default=None)
    p_done.add_argument("--summary", default="")
    p_done.add_argument("--metadata", default=None, help="JSON object string")
    p_done.add_argument(
        "--created-cards", default="",
        help="comma-separated created task ids",
    )

    p_block = sub.add_parser("block", help="Block current run (worker tool)")
    p_block.add_argument("task_id", nargs="?", default=None)
    p_block.add_argument("--reason", required=True)

    p_unblock = sub.add_parser("unblock", help="Unblock — back to ready/todo")
    p_unblock.add_argument("task_id")

    p_hb = sub.add_parser("heartbeat", help="Refresh claim TTL")
    p_hb.add_argument("--ttl", type=int, default=300, dest="ttl_seconds")
    p_hb.add_argument("--note", default="")
    p_hb.add_argument("task_id", nargs="?", default=None)

    p_runs = sub.add_parser("runs", help="List task runs")
    p_runs.add_argument("task_id")

    p_link = sub.add_parser("link", help="Add parent → child dependency")
    p_link.add_argument("parent_id")
    p_link.add_argument("child_id")

    p_unlink = sub.add_parser("unlink", help="Remove parent → child dependency")
    p_unlink.add_argument("parent_id")
    p_unlink.add_argument("child_id")

    p_arch = sub.add_parser("archive", help="Archive (soft delete)")
    p_arch.add_argument("task_id")

    p_assign = sub.add_parser(
        "assign", help="Set assignee on a task (use 'none' to unassign)",
    )
    p_assign.add_argument("task_id")
    p_assign.add_argument("assignee", help="profile name or 'none'")

    p_claim = sub.add_parser(
        "claim", help="Atomically claim a ready task (manual worker entry)",
    )
    p_claim.add_argument(
        "task_id", nargs="?", default=None,
        help="optional — if omitted, claims the highest-priority ready task",
    )
    p_claim.add_argument(
        "--ttl", type=int, default=300, dest="claim_ttl_seconds",
    )

    p_disp = sub.add_parser(
        "dispatch", help="Run one dispatcher pass (promote/claim/reclaim)",
    )
    p_disp.add_argument(
        "--dry-run", action="store_true",
        help="Inspect only — no writes",
    )
    p_disp.add_argument(
        "--max", type=int, default=None, dest="max_claims",
        help="Cap step 4 (atomic claim + spawn) to N tasks",
    )

    sub.add_parser(
        "gc", help="Remove scratch workspaces of done/archived tasks",
    )

    args = p.parse_args(argv)

    settings = Settings()
    db_path = args.db or settings.kanban_db_path
    if not Path(db_path).is_absolute():
        db_path = _REPO / db_path
    ws_root = args.workspaces or settings.kanban_workspaces_root
    if not Path(ws_root).is_absolute():
        ws_root = _REPO / ws_root
    db = KanbanDB(Path(db_path), workspaces_root=Path(ws_root))
    await db.migrate()
    as_json = not args.text

    # Resolve effective board: explicit --board flag > current pointer > 'default'
    effective_board = args.board or db.get_current_board()

    async def _resolve(prefix: str | None) -> KanbanTask | None:
        if not prefix:
            prefix = os.environ.get("HERMES_KANBAN_TASK")
        if not prefix:
            return None
        t = await db.get_task(prefix)
        if t is not None and t.board_id == effective_board:
            return t
        all_tasks = await db.list_tasks(
            include_archived=True, board_id=effective_board,
        )
        cand = [x for x in all_tasks if x.id.startswith(prefix)]
        return cand[0] if len(cand) == 1 else None

    try:
        if args.cmd == "init":
            _emit(
                {"db": str(db_path), "workspaces": str(ws_root),
                 "schema": "ok", "current_board": effective_board},
                as_json=as_json,
            )
            return 0

        if args.cmd == "boards":
            return await _handle_boards(db, args, as_json=as_json)

        if args.cmd == "tail":
            return await _handle_tail(db, args, as_json=as_json)

        if args.cmd == "list":
            tasks = await db.list_tasks(
                status=args.status, tenant=args.tenant,
                assignee=args.assignee, board_id=effective_board,
            )
            _emit(tasks, as_json=as_json)
            return 0

        if args.cmd == "show":
            tid = args.task_id or os.environ.get("HERMES_KANBAN_TASK")
            if not tid:
                _emit({"error": "task_id required"}, as_json=as_json)
                return 2
            out = await kanban_show(db, task_id=tid)
            _emit(out, as_json=as_json)
            return 0

        if args.cmd == "create":
            parents = _parse_csv(args.parents) or None
            try:
                ws_kind, ws_path = parse_workspace_spec(args.workspace)
            except WorkspaceError as e:
                _emit({"error": str(e)}, as_json=as_json)
                return 2
            out = await kanban_create(
                db,
                title=args.title, assignee=args.assignee, body=args.body,
                parents=parents, priority=args.priority,
                tenant=args.tenant,
                idempotency_key=args.idempotency_key,
                scheduled_at=args.scheduled_at,
                max_runtime_seconds=args.max_runtime_seconds,
                created_by=args.created_by,
                skills=args.skills or None,
                board_id=effective_board,
                workspace_kind=ws_kind,
                workspace_path=ws_path,
                max_retries=args.max_retries,
            )
            if args.triage and out["status"] != "triage":
                await db.set_status(out["task_id"], "triage", actor="cli")
                out["status"] = "triage"
            _emit(out, as_json=as_json)
            return 0

        if args.cmd == "comment":
            t = await _resolve(args.task_id)
            if t is None:
                _emit(
                    {"error": f"task {args.task_id!r} not found"},
                    as_json=as_json,
                )
                return 1
            out = await kanban_comment(
                db, task_id=t.id, body=args.body, author=args.author,
            )
            _emit(out, as_json=as_json)
            return 0

        if args.cmd == "complete":
            tid = args.task_id or os.environ.get("HERMES_KANBAN_TASK")
            if not tid:
                _emit({"error": "task_id required"}, as_json=as_json)
                return 2
            metadata = json.loads(args.metadata) if args.metadata else None
            created_cards = _parse_csv(args.created_cards) or None
            out = await kanban_complete(
                db, task_id=tid, summary=args.summary,
                metadata=metadata, created_cards=created_cards,
            )
            _emit(out, as_json=as_json)
            return 0

        if args.cmd == "block":
            tid = args.task_id or os.environ.get("HERMES_KANBAN_TASK")
            if not tid:
                _emit({"error": "task_id required"}, as_json=as_json)
                return 2
            out = await kanban_block(db, task_id=tid, reason=args.reason)
            _emit(out, as_json=as_json)
            return 0

        if args.cmd == "unblock":
            t = await _resolve(args.task_id)
            if t is None:
                _emit(
                    {"error": f"task {args.task_id!r} not found"},
                    as_json=as_json,
                )
                return 1
            parents = await db.parents_of(t.id)
            new_status = (
                "ready"
                if (not parents or all(p.status == "done" for p in parents))
                else "todo"
            )
            await db.set_status(t.id, new_status, actor="cli")
            _emit(
                {"task_id": t.id, "status": new_status},
                as_json=as_json,
            )
            return 0

        if args.cmd == "heartbeat":
            tid = args.task_id or os.environ.get("HERMES_KANBAN_TASK")
            if not tid:
                _emit({"error": "task_id required"}, as_json=as_json)
                return 2
            out = await kanban_heartbeat(
                db, task_id=tid,
                ttl_seconds=args.ttl_seconds, note=args.note,
            )
            _emit(out, as_json=as_json)
            return 0

        if args.cmd == "runs":
            t = await _resolve(args.task_id)
            if t is None:
                _emit(
                    {"error": f"task {args.task_id!r} not found"},
                    as_json=as_json,
                )
                return 1
            runs = await db.list_runs(t.id)
            payload = [r.model_dump(mode="json") for r in runs]
            _emit(payload, as_json=as_json)
            return 0

        if args.cmd == "link":
            parent = await _resolve(args.parent_id)
            child = await _resolve(args.child_id)
            if parent is None or child is None:
                _emit(
                    {"error": "parent or child not found"},
                    as_json=as_json,
                )
                return 1
            out = await kanban_link(
                db, parent_id=parent.id, child_id=child.id,
            )
            _emit(out, as_json=as_json)
            return 0

        if args.cmd == "unlink":
            parent = await _resolve(args.parent_id)
            child = await _resolve(args.child_id)
            if parent is None or child is None:
                _emit(
                    {"error": "parent or child not found"},
                    as_json=as_json,
                )
                return 1
            removed = await db.remove_link(
                parent_id=parent.id, child_id=child.id
            )
            _emit(
                {"parent_id": parent.id, "child_id": child.id,
                 "removed": removed},
                as_json=as_json,
            )
            return 0 if removed else 1

        if args.cmd == "archive":
            t = await _resolve(args.task_id)
            if t is None:
                _emit(
                    {"error": f"task {args.task_id!r} not found"},
                    as_json=as_json,
                )
                return 1
            await db.set_status(t.id, "archived", actor="cli")
            _emit(
                {"task_id": t.id, "status": "archived"},
                as_json=as_json,
            )
            return 0

        if args.cmd == "assign":
            t = await _resolve(args.task_id)
            if t is None:
                _emit(
                    {"error": f"task {args.task_id!r} not found"},
                    as_json=as_json,
                )
                return 1
            new_assignee = (
                None if args.assignee.lower() == "none"
                else args.assignee
            )
            updated = await db.set_assignee(
                t.id, new_assignee, actor="cli"
            )
            _emit(
                {"task_id": t.id, "assignee": updated.assignee},
                as_json=as_json,
            )
            return 0

        if args.cmd == "claim":
            if args.task_id:
                t = await _resolve(args.task_id)
                if t is None:
                    _emit(
                        {"error": f"task {args.task_id!r} not found"},
                        as_json=as_json,
                    )
                    return 1
                if t.status != "ready":
                    _emit(
                        {"error": f"task {t.id} status={t.status} (need ready)"},
                        as_json=as_json,
                    )
                    return 1
                # No exclude — caller asked for this specific id, and the
                # atomic-claim path picks the top-priority ready task.
                # We can't pin a specific id directly without changing
                # atomic_claim_one_ready, so we set priority temporarily.
                # Simpler: let the dispatcher claim the highest-priority
                # ready task and if it isn't this one, abort.
            claimed = await db.atomic_claim_one_ready(
                claim_ttl_seconds=args.claim_ttl_seconds,
            )
            if claimed is None:
                _emit({"error": "no ready task to claim"}, as_json=as_json)
                return 1
            if args.task_id and claimed.id != t.id:
                # Roll back — wrong task picked. Mark it ready again.
                await db.end_run(
                    claimed.current_run_id, outcome="reclaimed",
                    error="wrong-target rollback",
                )
                await db.set_status(claimed.id, "ready", actor="cli")
                _emit(
                    {"error": (
                        f"could not claim {t.id}; another higher-priority "
                        f"task ({claimed.id}) was at the head"
                    )},
                    as_json=as_json,
                )
                return 1
            _emit(
                {"task_id": claimed.id,
                 "run_id": claimed.current_run_id,
                 "workspace_path": claimed.workspace_path,
                 "status": "running"},
                as_json=as_json,
            )
            return 0

        if args.cmd == "dispatch":
            disp = KanbanDispatcher(
                db,
                spawn_runner=_no_spawn,
                poll_seconds=settings.kanban_dispatcher_poll_seconds,
                claim_ttl_seconds=settings.kanban_claim_ttl_seconds,
                spawn_failure_limit=settings.kanban_spawn_failure_limit,
                max_inflight=999 if args.dry_run else 1,
            )
            report = await disp.tick(
                dry_run=args.dry_run, max_claims=args.max_claims,
            )
            _emit(report, as_json=as_json)
            return 0

        if args.cmd == "gc":
            removed: list[str] = []
            done = await db.list_tasks(
                status="done", board_id=effective_board,
            )
            archived = await db.list_tasks(
                status="archived", include_archived=True,
                board_id=effective_board,
            )
            # gc only touches scratch workspaces — never delete user-owned
            # dir: paths.
            for t in [t for t in done + archived if t.workspace_kind == "scratch"]:
                if cleanup_scratch_workspace(
                    settings.kanban_workspaces_root
                    if Path(settings.kanban_workspaces_root).is_absolute()
                    else _REPO / settings.kanban_workspaces_root,
                    t.id,
                ) or cleanup_scratch_workspace(ws_root, t.id):
                    removed.append(t.id)
            _emit(
                {"removed": removed, "count": len(removed)},
                as_json=as_json,
            )
            return 0
    except KanbanToolError as e:
        _emit({"error": str(e)}, as_json=as_json)
        return 1
    except json.JSONDecodeError as e:
        _emit({"error": f"invalid JSON: {e}"}, as_json=as_json)
        return 2

    return 2


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    raise SystemExit(main())
