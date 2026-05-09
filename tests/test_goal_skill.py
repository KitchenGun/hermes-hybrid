"""Tests for GoalSkill (P3)."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Settings
from src.core.kanban import KanbanDB
from src.skills.base import SkillContext
from src.skills.goal_skill import GoalSkill


def _ctx(tmp_path: Path) -> SkillContext:
    settings = Settings(
        kanban_db_path=tmp_path / "k.db",
        kanban_workspaces_root=tmp_path / "ws",
    )
    return SkillContext(
        settings=settings, repo=None, memory=None,
        user_id="user1", session_id="sess1",
    )


# ---- match -----------------------------------------------------------


def test_match_basic():
    skill = GoalSkill()
    m = skill.match("/goal 인스타 자동화")
    assert m is not None
    assert m.skill_name == "goal"
    assert "인스타 자동화" in m.args["rest"]


def test_match_no_args_still_matches():
    skill = GoalSkill()
    m = skill.match("/goal")
    assert m is not None
    assert m.args["rest"] == ""


def test_match_case_insensitive():
    skill = GoalSkill()
    assert GoalSkill().match("/Goal hello") is not None
    assert GoalSkill().match("/GOAL hello") is not None


def test_no_match_for_unrelated():
    skill = GoalSkill()
    assert skill.match("/notgoal hello") is None
    assert skill.match("hello /goal") is None


# ---- invoke: dry-run -------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_empty_returns_usage(tmp_path: Path):
    skill = GoalSkill()
    out = await skill.invoke(skill.match("/goal"), _ctx(tmp_path))
    assert "Goal" in out
    assert "/goal" in out


@pytest.mark.asyncio
async def test_invoke_dry_run_does_not_persist(tmp_path: Path):
    skill = GoalSkill()
    ctx = _ctx(tmp_path)
    out = await skill.invoke(
        skill.match("/goal --dry-run 인스타 자동화"), ctx
    )
    assert "dry-run" in out
    assert "Goal Plan" in out
    # No DB created (the plan path never opened a KanbanDB).
    assert not (tmp_path / "k.db").exists()


@pytest.mark.asyncio
async def test_invoke_dry_run_shows_six_tasks(tmp_path: Path):
    skill = GoalSkill()
    out = await skill.invoke(
        skill.match("/goal --dry-run 빌드 안정화"), _ctx(tmp_path),
    )
    # Each numbered line `**N.**` for stages 1..6
    for n in range(1, 7):
        assert f"**{n}.**" in out


# ---- invoke: real persistence ----------------------------------------


@pytest.mark.asyncio
async def test_invoke_persists_tasks_as_ready(tmp_path: Path):
    skill = GoalSkill()
    ctx = _ctx(tmp_path)
    out = await skill.invoke(
        skill.match("/goal Discord session auto-resume 고도화"), ctx,
    )
    assert "task(s) created" in out
    db = KanbanDB(ctx.settings.kanban_db_path,
                  workspaces_root=ctx.settings.kanban_workspaces_root)
    await db.migrate()
    tasks = await db.list_tasks()
    assert len(tasks) == 6
    assert all(t.status == "ready" for t in tasks)
    # ID prefixes appear in output
    for t in tasks:
        assert t.id[:8] in out


@pytest.mark.asyncio
async def test_invoke_workspace_dir_propagates(tmp_path: Path):
    skill = GoalSkill()
    ctx = _ctx(tmp_path)
    target = tmp_path / "shared"
    out = await skill.invoke(
        skill.match(
            f"/goal --workspace dir:{target} 인스타 자동화"
        ),
        ctx,
    )
    assert "task(s) created" in out
    db = KanbanDB(ctx.settings.kanban_db_path,
                  workspaces_root=ctx.settings.kanban_workspaces_root)
    await db.migrate()
    tasks = await db.list_tasks()
    assert all(t.workspace_kind == "dir" for t in tasks)
    assert all(t.workspace_path == str(target) for t in tasks)


@pytest.mark.asyncio
async def test_invoke_relative_workspace_rejected(tmp_path: Path):
    skill = GoalSkill()
    out = await skill.invoke(
        skill.match("/goal --workspace dir:relative 인스타 자동화"),
        _ctx(tmp_path),
    )
    assert "⚠️" in out
    # No DB write
    assert not (tmp_path / "k.db").exists()


@pytest.mark.asyncio
async def test_invoke_max_retries_propagates(tmp_path: Path):
    skill = GoalSkill()
    ctx = _ctx(tmp_path)
    await skill.invoke(
        skill.match("/goal --max-retries 5 어떤 목표"), ctx,
    )
    db = KanbanDB(ctx.settings.kanban_db_path,
                  workspaces_root=ctx.settings.kanban_workspaces_root)
    await db.migrate()
    tasks = await db.list_tasks()
    assert all(t.max_retries == 5 for t in tasks)


@pytest.mark.asyncio
async def test_invoke_max_retries_out_of_range_rejected(tmp_path: Path):
    skill = GoalSkill()
    out = await skill.invoke(
        skill.match("/goal --max-retries 99 something"),
        _ctx(tmp_path),
    )
    assert "⚠️" in out
    assert not (tmp_path / "k.db").exists()


@pytest.mark.asyncio
async def test_invoke_max_retries_non_int_rejected(tmp_path: Path):
    skill = GoalSkill()
    out = await skill.invoke(
        skill.match("/goal --max-retries abc something"),
        _ctx(tmp_path),
    )
    assert "⚠️" in out


@pytest.mark.asyncio
async def test_invoke_workspace_missing_value_rejected(tmp_path: Path):
    skill = GoalSkill()
    out = await skill.invoke(
        skill.match("/goal --workspace"), _ctx(tmp_path),
    )
    assert "⚠️" in out


@pytest.mark.asyncio
async def test_invoke_flags_anywhere_in_message(tmp_path: Path):
    """Flag handling shouldn't depend on flag position vs goal text."""
    skill = GoalSkill()
    ctx = _ctx(tmp_path)
    out = await skill.invoke(
        skill.match("/goal --dry-run --max-retries 4 빌드 자동화"),
        ctx,
    )
    assert "dry-run" in out
    assert "max_retries: 4" in out


@pytest.mark.asyncio
async def test_invoke_only_dry_run_flag_no_text_returns_usage(tmp_path: Path):
    skill = GoalSkill()
    out = await skill.invoke(
        skill.match("/goal --dry-run"), _ctx(tmp_path),
    )
    # Only the flag, no goal text — should print usage.
    assert "/goal" in out
    assert "dry-run" not in out.lower() or "<자연어 목표>" in out
