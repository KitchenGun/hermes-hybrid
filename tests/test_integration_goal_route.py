"""IntentRouter routing integration test for /goal (P3).

Verifies that:
  1. ``/goal ...`` is short-circuited as a slash skill (handled_by="skill:goal").
  2. Existing slash skills (kanban, memo, hybrid-status) still match unchanged.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Settings
from src.integration.intent_router import IntentRouter


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        kanban_db_path=tmp_path / "k.db",
        kanban_workspaces_root=tmp_path / "ws",
        state_db_path=tmp_path / "state.db",
    )


@pytest.mark.asyncio
async def test_goal_routes_to_goal_skill(tmp_path: Path):
    router = IntentRouter(_settings(tmp_path))
    result = await router.route(
        user_message="/goal 인스타 자동화 파이프라인 만들기",
        user_id="42", session_id="s1",
    )
    assert result.short_circuited is True
    assert result.handled_by == "skill:goal"
    assert result.slash_skill == "goal"


@pytest.mark.asyncio
async def test_goal_with_flags_still_routes(tmp_path: Path):
    router = IntentRouter(_settings(tmp_path))
    result = await router.route(
        user_message="/goal --dry-run --workspace dir:/abs/x 빌드 안정화",
        user_id="42", session_id="s1",
    )
    assert result.handled_by == "skill:goal"


@pytest.mark.asyncio
async def test_existing_slash_skills_still_match(tmp_path: Path):
    """Adding GoalSkill must not break the prior set."""
    router = IntentRouter(_settings(tmp_path))

    r_kanban = await router.route(
        user_message="/kanban list", user_id="42", session_id="s1",
    )
    assert r_kanban.handled_by == "skill:kanban"

    r_memo = await router.route(
        user_message="/memo list", user_id="42", session_id="s1",
    )
    assert r_memo.handled_by == "skill:hybrid-memo"


@pytest.mark.asyncio
async def test_freeform_text_falls_through_to_master(tmp_path: Path):
    router = IntentRouter(_settings(tmp_path))
    result = await router.route(
        user_message="안녕 오늘 어떻게 도와줄래?",
        user_id="42", session_id="s1",
    )
    assert result.short_circuited is False
    assert result.handled_by is None


@pytest.mark.asyncio
async def test_lookalike_does_not_match_goal(tmp_path: Path):
    router = IntentRouter(_settings(tmp_path))
    # Embedded /goal should not match — must start the message.
    result = await router.route(
        user_message="이건 그냥 /goal 같은 단어가 들어간 문장",
        user_id="42", session_id="s1",
    )
    assert result.handled_by is None
