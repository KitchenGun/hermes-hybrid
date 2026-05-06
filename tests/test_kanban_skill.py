"""Tests for the /kanban slash skill (Phase 6 follow-up).

Locks down the surface a Discord user actually types:
  * `/kanban list` / `/kanban list <tenant>`
  * `/kanban add <tenant> <title with spaces>`
  * `/kanban view <prefix>` (8-char prefix lookup)
  * `/kanban comment <prefix> <text with spaces>`
  * `/kanban done <prefix>` / `/kanban cancel <prefix>`
  * usage hint when verb is malformed
  * prefix collision → ambiguity error
  * non-`/kanban` messages don't match
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Settings
from src.skills import KanbanSkill, SkillContext


def _make_ctx(tmp_path: Path, *, user_id: str = "u1") -> SkillContext:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        require_allowlist=False,
        ollama_enabled=False,
        kanban_store_path=tmp_path / "kanban.json",
        experience_log_enabled=False,
    )
    return SkillContext(
        settings=settings,
        repo=None,
        memory=None,  # KanbanSkill doesn't touch MemoryBackend
        user_id=user_id,
        session_id="s1",
    )


def test_match_list_no_args():
    skill = KanbanSkill()
    m = skill.match("/kanban list")
    assert m is not None
    assert m.args == {"verb": "list"}


def test_match_list_with_tenant():
    skill = KanbanSkill()
    m = skill.match("/kanban list advisor_ops")
    assert m is not None
    assert m.args["verb"] == "list"
    assert m.args["rest"] == "advisor_ops"


def test_match_does_not_collide_with_other_messages():
    skill = KanbanSkill()
    assert skill.match("kanban?") is None
    assert skill.match("/kanbanlist") is None
    assert skill.match("hello /kanban list") is None  # must start at line


@pytest.mark.asyncio
async def test_list_empty_returns_friendly_message(tmp_path):
    skill = KanbanSkill()
    ctx = _make_ctx(tmp_path)
    m = skill.match("/kanban list")
    out = await skill.invoke(m, ctx)
    assert "진행 중 task 없음" in out


@pytest.mark.asyncio
async def test_add_then_list_then_view_then_done(tmp_path):
    """End-to-end happy path through the verbs a user actually chains."""
    skill = KanbanSkill()
    ctx = _make_ctx(tmp_path)

    # Add — title with spaces
    add_msg = "/kanban add advisor_ops install ripgrep for fast search"
    out = await skill.invoke(skill.match(add_msg), ctx)
    assert "added" in out
    assert "advisor_ops" in out
    # The add response carries the 8-char id we'll reuse below.
    # Strip after the trailing backtick of `id`...
    import re
    m = re.search(r"`([0-9a-f]{8})`", out)
    assert m is not None
    short_id = m.group(1)

    # List — should now show one open task
    out = await skill.invoke(skill.match("/kanban list"), ctx)
    assert short_id in out
    assert "advisor_ops" in out

    # List with tenant filter — same task
    out = await skill.invoke(
        skill.match("/kanban list advisor_ops"), ctx
    )
    assert short_id in out

    # List with non-matching tenant filter — none
    out = await skill.invoke(skill.match("/kanban list other"), ctx)
    assert "진행 중 task 없음" in out

    # View by prefix
    out = await skill.invoke(skill.match(f"/kanban view {short_id}"), ctx)
    assert "install ripgrep" in out
    assert "advisor_ops" in out
    assert short_id in out

    # Comment — text with spaces
    out = await skill.invoke(
        skill.match(f"/kanban comment {short_id} approve, schedule next week"),
        ctx,
    )
    assert "commented" in out

    # Done
    out = await skill.invoke(skill.match(f"/kanban done {short_id}"), ctx)
    assert "done" in out

    # List again — task is now done, list shows none open
    out = await skill.invoke(skill.match("/kanban list"), ctx)
    assert "진행 중 task 없음" in out


@pytest.mark.asyncio
async def test_view_unknown_prefix_returns_error(tmp_path):
    skill = KanbanSkill()
    ctx = _make_ctx(tmp_path)
    out = await skill.invoke(skill.match("/kanban view deadbeef"), ctx)
    assert "못 찾음" in out


@pytest.mark.asyncio
async def test_view_ambiguous_prefix_lists_candidates(tmp_path):
    """Two tasks whose IDs share the same first character — view by
    that single char must surface the ambiguity rather than silently
    pick one."""
    skill = KanbanSkill()
    ctx = _make_ctx(tmp_path)

    # Force two tasks; UUIDs collide on first hex char with high
    # probability over a few attempts. Loop until we have two with the
    # same first char to make the test deterministic.
    from src.core import KanbanStore
    store = KanbanStore(ctx.settings.kanban_store_path)
    a = store.create(tenant="x", title="task-a")
    # Find a sibling whose id shares prefix with a.id[0]
    target_prefix = a.id[0]
    for _ in range(50):
        b = store.create(tenant="x", title="task-b")
        if b.id[0] == target_prefix:
            break
    else:
        pytest.skip("couldn't generate colliding UUID prefix")

    out = await skill.invoke(
        skill.match(f"/kanban view {target_prefix}"), ctx
    )
    assert "모호" in out


@pytest.mark.asyncio
async def test_add_without_title_returns_usage(tmp_path):
    skill = KanbanSkill()
    ctx = _make_ctx(tmp_path)
    out = await skill.invoke(skill.match("/kanban add advisor_ops"), ctx)
    assert "Usage" in out
    out = await skill.invoke(skill.match("/kanban add"), ctx)
    assert "Usage" in out


@pytest.mark.asyncio
async def test_comment_without_text_returns_usage(tmp_path):
    skill = KanbanSkill()
    ctx = _make_ctx(tmp_path)
    out = await skill.invoke(skill.match("/kanban comment abc12345"), ctx)
    assert "Usage" in out


@pytest.mark.asyncio
async def test_cancel_marks_task_cancelled(tmp_path):
    skill = KanbanSkill()
    ctx = _make_ctx(tmp_path)
    add_out = await skill.invoke(
        skill.match("/kanban add advisor_ops to be cancelled"), ctx
    )
    import re
    short_id = re.search(r"`([0-9a-f]{8})`", add_out).group(1)
    out = await skill.invoke(skill.match(f"/kanban cancel {short_id}"), ctx)
    assert "cancelled" in out


@pytest.mark.asyncio
async def test_kanban_skill_registered_in_default_registry(tmp_path):
    """Smoke: KanbanSkill is in default_registry — orchestrator's
    skill matcher will reach `/kanban` without extra wiring."""
    from src.skills import default_registry
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        require_allowlist=False,
        ollama_enabled=False,
        kanban_store_path=tmp_path / "kanban.json",
        experience_log_enabled=False,
    )
    reg = default_registry(settings)
    hit = reg.match("/kanban list")
    assert hit is not None
    skill, _match = hit
    assert skill.name == "kanban"
