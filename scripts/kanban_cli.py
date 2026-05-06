#!/usr/bin/env python3
"""CLI front for the cross-profile Kanban store.

Hermes profile prompts (advisor_ops, installer_ops) call this from
their ``terminal`` tool to create / list / comment / complete tasks.
JSON output mode (``--json``) is the default for prompt parsing — the
text mode is for human ad-hoc use.

Examples (from a profile prompt):
    python3 /mnt/e/hermes-hybrid/scripts/kanban_cli.py list --tenant advisor_ops --status todo
    python3 /mnt/e/hermes-hybrid/scripts/kanban_cli.py add --tenant advisor_ops --title "ripgrep 설치" --body "..." --tags tooling,search
    python3 /mnt/e/hermes-hybrid/scripts/kanban_cli.py get <task_id>
    python3 /mnt/e/hermes-hybrid/scripts/kanban_cli.py comment <task_id> --author installer_ops --text "install plan ..."
    python3 /mnt/e/hermes-hybrid/scripts/kanban_cli.py done <task_id>

Exit codes:
    0  success
    1  not found / no result
    2  invalid arguments
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from src.config import Settings  # noqa: E402
from src.core import KanbanStore, KanbanTask  # noqa: E402


def _emit(obj, *, as_json: bool) -> None:
    if as_json:
        if isinstance(obj, KanbanTask):
            print(json.dumps(obj.model_dump(mode="json"), ensure_ascii=False))
        elif isinstance(obj, list):
            print(
                json.dumps(
                    [
                        t.model_dump(mode="json") if isinstance(t, KanbanTask) else t
                        for t in obj
                    ],
                    ensure_ascii=False,
                )
            )
        elif obj is None:
            print("null")
        else:
            print(json.dumps(obj, ensure_ascii=False))
    else:
        if isinstance(obj, KanbanTask):
            print(f"{obj.id}\t{obj.tenant}\t{obj.status}\t{obj.title}")
        elif isinstance(obj, list):
            for t in obj:
                if isinstance(t, KanbanTask):
                    print(f"{t.id}\t{t.tenant}\t{t.status}\t{t.title}")
                else:
                    print(t)
        elif obj is None:
            print("(none)")
        else:
            print(obj)


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store", type=Path, default=None, help="Override kanban_store_path")
    p.add_argument("--text", action="store_true", help="Plain-text output (default: JSON)")

    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List tasks")
    p_list.add_argument("--tenant")
    p_list.add_argument(
        "--status",
        choices=["triage", "todo", "in_progress", "review", "done", "cancelled"],
    )
    p_list.add_argument("--assigned-to")

    p_add = sub.add_parser("add", help="Create a task")
    p_add.add_argument("--tenant", required=True)
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--body", default="")
    p_add.add_argument("--tags", default="", help="comma-separated")
    p_add.add_argument("--created-by", default="")
    p_add.add_argument("--assigned-to", default=None)
    p_add.add_argument(
        "--status",
        default="todo",
        choices=["triage", "todo", "in_progress", "review", "done", "cancelled"],
        help='Initial status (default "todo"). advisor_ops uses "triage".',
    )

    p_get = sub.add_parser("get", help="Get a task by id (or unique prefix)")
    p_get.add_argument("task_id")

    p_comment = sub.add_parser("comment", help="Append a comment")
    p_comment.add_argument("task_id")
    p_comment.add_argument("--author", required=True)
    p_comment.add_argument("--text", required=True)

    p_done = sub.add_parser("done", help="Mark complete")
    p_done.add_argument("task_id")

    p_cancel = sub.add_parser("cancel", help="Mark cancelled")
    p_cancel.add_argument("task_id")

    p_status = sub.add_parser("status", help="Set status")
    p_status.add_argument("task_id")
    p_status.add_argument(
        "--to",
        required=True,
        choices=["triage", "todo", "in_progress", "review", "done", "cancelled"],
    )

    args = p.parse_args(argv)

    settings = Settings()
    store_path = args.store or settings.kanban_store_path
    if not Path(store_path).is_absolute():
        store_path = _REPO / store_path
    store = KanbanStore(Path(store_path))
    as_json = not args.text

    def _resolve(prefix: str):
        # Direct id first
        t = store.get(prefix)
        if t is not None:
            return t
        # Prefix lookup
        candidates = [x for x in store.list() if x.id.startswith(prefix)]
        if len(candidates) == 1:
            return candidates[0]
        return None  # not found or ambiguous

    if args.cmd == "list":
        tasks = store.list(
            tenant=args.tenant,
            status=args.status,
            assigned_to=args.assigned_to,
        )
        _emit(tasks, as_json=as_json)
        return 0

    if args.cmd == "add":
        task = store.create(
            tenant=args.tenant,
            title=args.title,
            body=args.body,
            tags=_parse_tags(args.tags),
            created_by=args.created_by,
            assigned_to=args.assigned_to,
            status=args.status,
        )
        _emit(task, as_json=as_json)
        return 0

    if args.cmd == "get":
        t = _resolve(args.task_id)
        if t is None:
            _emit(None, as_json=as_json)
            return 1
        _emit(t, as_json=as_json)
        return 0

    if args.cmd == "comment":
        t = _resolve(args.task_id)
        if t is None:
            _emit(None, as_json=as_json)
            return 1
        updated = store.comment(t.id, author=args.author, text=args.text)
        _emit(updated, as_json=as_json)
        return 0 if updated is not None else 1

    if args.cmd == "done":
        t = _resolve(args.task_id)
        if t is None:
            _emit(None, as_json=as_json)
            return 1
        updated = store.complete(t.id)
        _emit(updated, as_json=as_json)
        return 0 if updated is not None else 1

    if args.cmd == "cancel":
        t = _resolve(args.task_id)
        if t is None:
            _emit(None, as_json=as_json)
            return 1
        updated = store.cancel(t.id)
        _emit(updated, as_json=as_json)
        return 0 if updated is not None else 1

    if args.cmd == "status":
        t = _resolve(args.task_id)
        if t is None:
            _emit(None, as_json=as_json)
            return 1
        updated = store.set_status(t.id, status=args.to)
        _emit(updated, as_json=as_json)
        return 0 if updated is not None else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
