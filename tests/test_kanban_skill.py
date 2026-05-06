"""Integration tests for KanbanSkill (Phase 2-A, Nous verb set)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.config import Settings
from src.skills.base import SkillContext
from src.skills.kanban_skill import KanbanSkill


def _ctx(tmp_path: Path) -> SkillContext:
    settings = Settings(
        kanban_db_path=tmp_path / "k.db",
        kanban_workspaces_root=tmp_path / "ws",
    )
    return SkillContext(
        settings=settings, repo=None, memory=None,
        user_id="user1", session_id="sess1",
    )


def _id_from_create_output(text: str) -> str:
    m = re.search(r"created `([^`]+)`", text)
    assert m, f"no id in: {text!r}"
    return m.group(1)


def test_match_list_basic():
    skill = KanbanSkill()
    m = skill.match("/kanban list")
    assert m is not None and m.args["verb"] == "list"


def test_match_create_with_args():
    skill = KanbanSkill()
    m = skill.match("/kanban create devops echo hello")
    assert m is not None
    assert m.args["verb"] == "create"
    assert "devops echo hello" in m.args["rest"]


def test_match_unknown_returns_none():
    assert KanbanSkill().match("/notkanban list") is None


def test_match_case_insensitive_verb():
    m = KanbanSkill().match("/Kanban LIST")
    assert m is not None and m.args["verb"] == "list"


@pytest.mark.asyncio
async def test_invoke_list_empty(tmp_path: Path):
    skill = KanbanSkill()
    ctx = _ctx(tmp_path)
    out = await skill.invoke(skill.match("/kanban list"), ctx)
    assert "task 없음" in out


@pytest.mark.asyncio
async def test_invoke_create_then_list(tmp_path: Path):
    skill = KanbanSkill()
    ctx = _ctx(tmp_path)
    out = await skill.invoke(
        skill.match("/kanban create devops echo hello"), ctx
    )
    assert "✅ created" in out
    out2 = await skill.invoke(skill.match("/kanban list"), ctx)
    assert "Kanban — 1 open" in out2


@pytest.mark.asyncio
async def test_invoke_create_with_flags(tmp_path: Path):
    skill = KanbanSkill()
    ctx = _ctx(tmp_path)
    out = await skill.invoke(
        skill.match(
            "/kanban create dev refactor x --priority 5 --tenant biz"
        ),
        ctx,
    )
    tid = _id_from_create_output(out)
    show = await skill.invoke(skill.match(f"/kanban show {tid}"), ctx)
    assert "tenant: `biz`" in show
    assert "priority: 5" in show


@pytest.mark.asyncio
async def test_invoke_show_includes_status(tmp_path: Path):
    skill = KanbanSkill()
    ctx = _ctx(tmp_path)
    out = await skill.invoke(
        skill.match("/kanban create dev hi"), ctx
    )
    tid = _id_from_create_output(out)
    detail = await skill.invoke(skill.match(f"/kanban show {tid}"), ctx)
    assert "hi" in detail
    assert "status: **ready**" in detail


@pytest.mark.asyncio
async def test_invoke_complete_marks_done(tmp_path: Path):
    skill = KanbanSkill()
    ctx = _ctx(tmp_path)
    out = await skill.invoke(skill.match("/kanban create dev hi"), ctx)
    tid = _id_from_create_output(out)
    out2 = await skill.invoke(
        skill.match(f"/kanban complete {tid} done!"), ctx
    )
    assert "done" in out2


@pytest.mark.asyncio
async def test_invoke_block_then_unblock(tmp_path: Path):
    skill = KanbanSkill()
    ctx = _ctx(tmp_path)
    out = await skill.invoke(skill.match("/kanban create dev hi"), ctx)
    tid = _id_from_create_output(out)
    await skill.invoke(
        skill.match(f"/kanban block {tid} need decision"), ctx
    )
    out2 = await skill.invoke(skill.match(f"/kanban unblock {tid}"), ctx)
    assert "ready" in out2


@pytest.mark.asyncio
async def test_invoke_link_creates_dependency(tmp_path: Path):
    skill = KanbanSkill()
    ctx = _ctx(tmp_path)
    a_out = await skill.invoke(skill.match("/kanban create dev a"), ctx)
    b_out = await skill.invoke(skill.match("/kanban create dev b"), ctx)
    a_id = _id_from_create_output(a_out)
    b_id = _id_from_create_output(b_out)
    out = await skill.invoke(
        skill.match(f"/kanban link {a_id} {b_id}"), ctx
    )
    assert "linked" in out


@pytest.mark.asyncio
async def test_invoke_unknown_id_returns_friendly_error(tmp_path: Path):
    skill = KanbanSkill()
    ctx = _ctx(tmp_path)
    out = await skill.invoke(skill.match("/kanban show t_nope"), ctx)
    assert "못 찾음" in out


@pytest.mark.asyncio
async def test_invoke_archive(tmp_path: Path):
    skill = KanbanSkill()
    ctx = _ctx(tmp_path)
    out = await skill.invoke(skill.match("/kanban create dev x"), ctx)
    tid = _id_from_create_output(out)
    out2 = await skill.invoke(skill.match(f"/kanban archive {tid}"), ctx)
    assert "archived" in out2


@pytest.mark.asyncio
async def test_invoke_comment_appends(tmp_path: Path):
    skill = KanbanSkill()
    ctx = _ctx(tmp_path)
    out = await skill.invoke(skill.match("/kanban create dev x"), ctx)
    tid = _id_from_create_output(out)
    out2 = await skill.invoke(
        skill.match(f"/kanban comment {tid} this is feedback"), ctx
    )
    assert "commented" in out2
    show = await skill.invoke(skill.match(f"/kanban show {tid}"), ctx)
    assert "this is feedback" in show
