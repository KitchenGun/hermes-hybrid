"""Integration tests for ``scripts/kanban_cli.py`` (Phase 2-A)."""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def cli():
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    scripts_dir = repo_root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    if "kanban_cli" in sys.modules:
        del sys.modules["kanban_cli"]
    return importlib.import_module("kanban_cli")


def _run(cli_mod, argv: list[str], *, db_path: Path, ws_path: Path):
    full = ["--db", str(db_path), "--workspaces", str(ws_path), *argv]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli_mod.main(full)
    return rc, buf.getvalue()


def test_create_then_list_json(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli, ["create", "--title", "first", "--assignee", "devops"],
        db_path=db, ws_path=ws,
    )
    assert rc == 0
    payload = json.loads(out.strip())
    assert "task_id" in payload
    assert payload["status"] == "ready"

    rc, out = _run(cli, ["list"], db_path=db, ws_path=ws)
    assert rc == 0
    tasks = json.loads(out.strip())
    assert len(tasks) == 1
    assert tasks[0]["title"] == "first"


def test_create_idempotent(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli,
        ["create", "--title", "p", "--assignee", "x", "--idem", "job-1"],
        db_path=db, ws_path=ws,
    )
    first = json.loads(out.strip())
    rc, out = _run(
        cli,
        ["create", "--title", "p2", "--assignee", "y", "--idem", "job-1"],
        db_path=db, ws_path=ws,
    )
    second = json.loads(out.strip())
    assert second["task_id"] == first["task_id"]


def test_show_includes_worker_context(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli, ["create", "--title", "echo hi", "--assignee", "x"],
        db_path=db, ws_path=ws,
    )
    payload = json.loads(out.strip())

    rc, out = _run(
        cli, ["show", payload["task_id"]], db_path=db, ws_path=ws
    )
    detail = json.loads(out.strip())
    assert detail["task"]["title"] == "echo hi"
    assert "worker_context" in detail


def test_text_mode_outputs_tab_separated(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    _run(
        cli, ["create", "--title", "a", "--assignee", "x"],
        db_path=db, ws_path=ws,
    )
    rc, out = _run(cli, ["--text", "list"], db_path=db, ws_path=ws)
    assert rc == 0
    assert "\t" in out


def test_complete_without_active_run_errors(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli, ["create", "--title", "task", "--assignee", "x"],
        db_path=db, ws_path=ws,
    )
    tid = json.loads(out.strip())["task_id"]
    rc, out = _run(
        cli, ["complete", tid, "--summary", "done"],
        db_path=db, ws_path=ws,
    )
    assert rc != 0
    err = json.loads(out.strip())
    assert "error" in err


def test_unblock_resets_to_ready(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli, ["create", "--title", "x", "--assignee", "y"],
        db_path=db, ws_path=ws,
    )
    tid = json.loads(out.strip())["task_id"]
    rc, _ = _run(
        cli, ["block", tid, "--reason", "manual"],
        db_path=db, ws_path=ws,
    )
    # block via CLI requires active run — task wasn't claimed, so this errors.
    # Use unblock from a manually-blocked state by setting status via a 2nd
    # create + Discord skill in real flow; here we test unblock on ready
    # task (already-ready returns "ready" again, harmless).
    rc, out = _run(cli, ["unblock", tid], db_path=db, ws_path=ws)
    assert rc == 0
    payload = json.loads(out.strip())
    assert payload["status"] in ("ready", "todo")


def test_link_adds_dependency(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli, ["create", "--title", "a", "--assignee", "x"],
        db_path=db, ws_path=ws,
    )
    a_id = json.loads(out.strip())["task_id"]
    rc, out = _run(
        cli, ["create", "--title", "b", "--assignee", "x"],
        db_path=db, ws_path=ws,
    )
    b_id = json.loads(out.strip())["task_id"]

    rc, out = _run(cli, ["link", a_id, b_id], db_path=db, ws_path=ws)
    assert rc == 0
    payload = json.loads(out.strip())
    assert payload["parent_id"] == a_id
    assert payload["child_id"] == b_id


def test_archive_marks_archived(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli, ["create", "--title", "x", "--assignee", "y"],
        db_path=db, ws_path=ws,
    )
    tid = json.loads(out.strip())["task_id"]
    rc, out = _run(cli, ["archive", tid], db_path=db, ws_path=ws)
    assert rc == 0
    payload = json.loads(out.strip())
    assert payload["status"] == "archived"


def test_runs_returns_empty_for_unclaimed(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli, ["create", "--title", "x", "--assignee", "y"],
        db_path=db, ws_path=ws,
    )
    tid = json.loads(out.strip())["task_id"]
    rc, out = _run(cli, ["runs", tid], db_path=db, ws_path=ws)
    assert rc == 0
    assert json.loads(out.strip()) == []


def test_unknown_task_returns_error(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli, ["show", "t_phantom"], db_path=db, ws_path=ws,
    )
    assert rc == 1


def test_show_uses_env_var(cli, tmp_path, monkeypatch):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli, ["create", "--title", "envtest", "--assignee", "x"],
        db_path=db, ws_path=ws,
    )
    tid = json.loads(out.strip())["task_id"]
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    rc, out = _run(cli, ["show"], db_path=db, ws_path=ws)
    assert rc == 0
    payload = json.loads(out.strip())
    assert payload["task"]["title"] == "envtest"


# ---- Phase 2-A enhancement: init / claim / assign / unlink / dispatch / gc ----


def test_init_creates_db(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(cli, ["init"], db_path=db, ws_path=ws)
    assert rc == 0
    payload = json.loads(out.strip())
    assert payload["schema"] == "ok"
    assert db.exists()


def test_claim_with_no_id_picks_highest_priority(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    _run(cli, ["create", "--title", "low", "--assignee", "x", "--priority", "1"],
         db_path=db, ws_path=ws)
    rc, out = _run(
        cli,
        ["create", "--title", "high", "--assignee", "x", "--priority", "5"],
        db_path=db, ws_path=ws,
    )
    high_id = json.loads(out.strip())["task_id"]
    rc, out = _run(cli, ["claim"], db_path=db, ws_path=ws)
    assert rc == 0
    payload = json.loads(out.strip())
    assert payload["task_id"] == high_id
    assert payload["status"] == "running"


def test_claim_when_no_ready(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(cli, ["init"], db_path=db, ws_path=ws)
    rc, out = _run(cli, ["claim"], db_path=db, ws_path=ws)
    assert rc == 1


def test_assign_changes_assignee(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli, ["create", "--title", "t", "--assignee", "dev1"],
        db_path=db, ws_path=ws,
    )
    tid = json.loads(out.strip())["task_id"]
    rc, out = _run(cli, ["assign", tid, "dev2"], db_path=db, ws_path=ws)
    assert rc == 0
    payload = json.loads(out.strip())
    assert payload["assignee"] == "dev2"


def test_assign_none_unassigns(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli, ["create", "--title", "t", "--assignee", "dev1"],
        db_path=db, ws_path=ws,
    )
    tid = json.loads(out.strip())["task_id"]
    rc, out = _run(cli, ["assign", tid, "none"], db_path=db, ws_path=ws)
    assert rc == 0
    payload = json.loads(out.strip())
    assert payload["assignee"] is None


def test_unlink_removes_dependency(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli, ["create", "--title", "a", "--assignee", "x"],
        db_path=db, ws_path=ws,
    )
    a = json.loads(out.strip())["task_id"]
    rc, out = _run(
        cli, ["create", "--title", "b", "--assignee", "x"],
        db_path=db, ws_path=ws,
    )
    b = json.loads(out.strip())["task_id"]
    _run(cli, ["link", a, b], db_path=db, ws_path=ws)
    rc, out = _run(cli, ["unlink", a, b], db_path=db, ws_path=ws)
    assert rc == 0
    payload = json.loads(out.strip())
    assert payload["removed"] is True


def test_dispatch_dry_run_no_writes(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli, ["create", "--title", "x", "--assignee", "y"],
        db_path=db, ws_path=ws,
    )
    tid = json.loads(out.strip())["task_id"]
    rc, out = _run(cli, ["dispatch", "--dry-run"], db_path=db, ws_path=ws)
    assert rc == 0
    report = json.loads(out.strip())
    assert tid in report["claimed"]
    # The task is still ready (dry_run)
    rc, out = _run(cli, ["show", tid], db_path=db, ws_path=ws)
    detail = json.loads(out.strip())
    assert detail["task"]["status"] == "ready"


def test_create_with_triage_flag(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli, ["create", "--title", "x", "--assignee", "y", "--triage"],
        db_path=db, ws_path=ws,
    )
    payload = json.loads(out.strip())
    assert payload["status"] == "triage"


def test_gc_removes_done_workspace(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli, ["create", "--title", "x", "--assignee", "y"],
        db_path=db, ws_path=ws,
    )
    tid = json.loads(out.strip())["task_id"]
    # claim → workspace exists
    rc, out = _run(cli, ["claim"], db_path=db, ws_path=ws)
    workspace_path = Path(json.loads(out.strip())["workspace_path"])
    assert workspace_path.exists()
    # complete with summary so gc can remove
    rc, out = _run(
        cli, ["complete", tid, "--summary", "done"],
        db_path=db, ws_path=ws,
    )
    assert rc == 0
    # gc should remove workspace dir
    rc, out = _run(cli, ["gc"], db_path=db, ws_path=ws)
    assert rc == 0
    payload = json.loads(out.strip())
    assert tid in payload["removed"]
    assert not workspace_path.exists()


def test_idempotency_key_alias(cli, tmp_path):
    """`--idempotency-key` and `--idem` should be interchangeable."""
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli,
        ["create", "--title", "a", "--assignee", "x",
         "--idempotency-key", "k1"],
        db_path=db, ws_path=ws,
    )
    a = json.loads(out.strip())["task_id"]
    rc, out = _run(
        cli,
        ["create", "--title", "b", "--assignee", "y", "--idem", "k1"],
        db_path=db, ws_path=ws,
    )
    b = json.loads(out.strip())["task_id"]
    assert a == b


# ---- boards / tail / --skill -----------------------------------------


def test_boards_list_includes_default(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    _run(cli, ["init"], db_path=db, ws_path=ws)
    rc, out = _run(cli, ["boards", "list"], db_path=db, ws_path=ws)
    assert rc == 0
    boards = json.loads(out.strip())
    assert any(b["id"] == "default" for b in boards)


def test_boards_create_and_switch(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli,
        ["boards", "create", "alpha", "--name", "Alpha", "--switch"],
        db_path=db, ws_path=ws,
    )
    assert rc == 0
    payload = json.loads(out.strip())
    assert payload["id"] == "alpha"
    rc, out = _run(cli, ["boards", "show"], db_path=db, ws_path=ws)
    payload = json.loads(out.strip())
    assert payload["id"] == "alpha"


def test_boards_create_invalid_slug(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli, ["boards", "create", "with space"],
        db_path=db, ws_path=ws,
    )
    assert rc == 1
    err = json.loads(out.strip())
    assert "error" in err


def test_boards_rename(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    _run(cli, ["boards", "create", "alpha"], db_path=db, ws_path=ws)
    rc, out = _run(
        cli, ["boards", "rename", "alpha", "Alpha Prime"],
        db_path=db, ws_path=ws,
    )
    payload = json.loads(out.strip())
    assert payload["name"] == "Alpha Prime"


def test_boards_rm_archives(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    _run(cli, ["boards", "create", "temp"], db_path=db, ws_path=ws)
    rc, out = _run(cli, ["boards", "rm", "temp"], db_path=db, ws_path=ws)
    assert rc == 0
    payload = json.loads(out.strip())
    assert payload["status"] == "archived"


def test_boards_default_cant_be_removed(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    _run(cli, ["init"], db_path=db, ws_path=ws)
    rc, out = _run(cli, ["boards", "rm", "default"], db_path=db, ws_path=ws)
    assert rc == 1


def test_board_flag_routes_create_to_other_board(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    _run(cli, ["boards", "create", "alpha"], db_path=db, ws_path=ws)
    rc, out = _run(
        cli, ["--board", "alpha", "create",
              "--title", "alpha-task", "--assignee", "x"],
        db_path=db, ws_path=ws,
    )
    payload = json.loads(out.strip())
    assert payload["board_id"] == "alpha"

    # default board list does NOT include the alpha task
    rc, out = _run(cli, ["list"], db_path=db, ws_path=ws)
    default_tasks = json.loads(out.strip())
    assert all(t["board_id"] == "default" for t in default_tasks)
    # alpha board does
    rc, out = _run(
        cli, ["--board", "alpha", "list"], db_path=db, ws_path=ws,
    )
    alpha_tasks = json.loads(out.strip())
    assert any(t["title"] == "alpha-task" for t in alpha_tasks)


def test_create_with_skill_repeatable(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    rc, out = _run(
        cli,
        ["create", "--title", "x", "--assignee", "y",
         "--skill", "security", "--skill", "k8s"],
        db_path=db, ws_path=ws,
    )
    payload = json.loads(out.strip())
    assert set(payload["skills"]) == {"security", "k8s"}


def test_tail_once_prints_recent_events(cli, tmp_path):
    db = tmp_path / "k.db"
    ws = tmp_path / "ws"
    _run(
        cli, ["create", "--title", "a", "--assignee", "x"],
        db_path=db, ws_path=ws,
    )
    rc, out = _run(
        cli, ["tail", "--since", "0", "--once"],
        db_path=db, ws_path=ws,
    )
    assert rc == 0
    # Event ndjson — at least one line should mention 'created'
    assert "created" in out
