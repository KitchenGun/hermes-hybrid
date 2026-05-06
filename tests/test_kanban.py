"""Tests for the Kanban store (Phase 6 stub).

advisor_ops → installer_ops hand-off channel. Locks down:
  * create stamps id/created_at/updated_at and lands on disk
  * list filters by tenant / status / assigned_to (composable AND)
  * get returns None for missing id
  * comment appends and updates updated_at
  * set_status / complete / cancel transition the task and persist
  * malformed JSON file → fresh state (no crash)
  * round-trip across two KanbanStore instances on the same file
    (durability over process restarts)
"""
from __future__ import annotations

import json
from pathlib import Path

from src.core import KanbanStore, KanbanTask


def test_create_persists_task(tmp_path: Path):
    store = KanbanStore(tmp_path / "k.json")
    task = store.create(
        tenant="advisor_ops",
        title="install ripgrep",
        body="search performance gap",
        created_by="advisor_ops",
        tags=["tooling", "search"],
    )
    assert isinstance(task, KanbanTask)
    assert task.id
    assert task.status == "todo"
    assert task.tenant == "advisor_ops"

    # Disk reflects it
    raw = json.loads((tmp_path / "k.json").read_text(encoding="utf-8"))
    assert len(raw["tasks"]) == 1
    assert raw["tasks"][0]["title"] == "install ripgrep"


def test_list_filters_by_tenant_status_assigned(tmp_path: Path):
    store = KanbanStore(tmp_path / "k.json")
    a1 = store.create(tenant="advisor_ops", title="a1")
    a2 = store.create(tenant="advisor_ops", title="a2", assigned_to="installer_ops")
    store.create(tenant="installer_ops", title="i1")

    assert {t.title for t in store.list(tenant="advisor_ops")} == {"a1", "a2"}
    assert [t.title for t in store.list(assigned_to="installer_ops")] == ["a2"]
    # Composable AND: tenant + assigned_to
    assert [t.title for t in store.list(
        tenant="advisor_ops", assigned_to="installer_ops"
    )] == ["a2"]


def test_get_returns_none_for_missing_id(tmp_path: Path):
    store = KanbanStore(tmp_path / "k.json")
    store.create(tenant="x", title="t")
    assert store.get("does-not-exist") is None


def test_comment_appends_and_bumps_updated_at(tmp_path: Path):
    store = KanbanStore(tmp_path / "k.json")
    task = store.create(tenant="advisor_ops", title="t")
    original_updated = task.updated_at

    updated = store.comment(task.id, author="kang", text="approve")
    assert updated is not None
    assert len(updated.comments) == 1
    assert updated.comments[0].text == "approve"
    assert updated.comments[0].author == "kang"
    # updated_at should be >= original (ISO seconds — equal on fast systems)
    assert updated.updated_at >= original_updated


def test_set_status_transitions(tmp_path: Path):
    store = KanbanStore(tmp_path / "k.json")
    task = store.create(tenant="advisor_ops", title="t")
    assert task.status == "todo"

    in_prog = store.set_status(task.id, status="in_progress")
    assert in_prog and in_prog.status == "in_progress"

    done = store.complete(task.id)
    assert done and done.status == "done"

    # cancel still works after done — caller responsibility to gate.
    cancelled = store.cancel(task.id)
    assert cancelled and cancelled.status == "cancelled"


def test_corrupt_json_starts_fresh(tmp_path: Path):
    bad = tmp_path / "k.json"
    bad.write_text("{ this is not valid json", encoding="utf-8")
    store = KanbanStore(bad)
    # No crash on read; create still works, file gets rewritten.
    task = store.create(tenant="x", title="t")
    assert task is not None
    raw = json.loads(bad.read_text(encoding="utf-8"))
    assert len(raw["tasks"]) == 1


def test_durability_across_instances(tmp_path: Path):
    path = tmp_path / "k.json"
    store1 = KanbanStore(path)
    task = store1.create(tenant="advisor_ops", title="install grep")

    store2 = KanbanStore(path)
    listed = store2.list(tenant="advisor_ops")
    assert len(listed) == 1
    assert listed[0].id == task.id
    assert listed[0].title == "install grep"


def test_list_skips_malformed_task_entries(tmp_path: Path):
    path = tmp_path / "k.json"
    store = KanbanStore(path)
    store.create(tenant="x", title="good")
    # Manually corrupt one entry — should be skipped, not crash list().
    raw = json.loads(path.read_text("utf-8"))
    raw["tasks"].append({"id": "bad-no-tenant"})
    path.write_text(json.dumps(raw), encoding="utf-8")

    listed = store.list()
    assert len(listed) == 1
    assert listed[0].title == "good"
