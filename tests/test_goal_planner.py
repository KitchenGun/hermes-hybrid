"""Unit tests for the deterministic goal planner (P3)."""
from __future__ import annotations

import pytest

from src.orchestrator.goal_planner import (
    GoalPlannerError,
    GoalTask,
    plan_goal,
)


def test_empty_goal_text_rejected():
    with pytest.raises(GoalPlannerError):
        plan_goal(goal_text="")
    with pytest.raises(GoalPlannerError):
        plan_goal(goal_text="   ")


def test_default_returns_six_tasks():
    plan = plan_goal(goal_text="인스타 자동화 파이프라인 만들기")
    assert len(plan.tasks) == 6
    assert plan.goal_title == "인스타 자동화 파이프라인 만들기"


def test_task_count_in_3_to_8_inclusive():
    plan_lo = plan_goal(goal_text="x", max_tasks=3)
    assert len(plan_lo.tasks) == 3
    plan_hi = plan_goal(goal_text="x", max_tasks=8)
    # blueprint only has 6 stages, so 8 caps at 6 — that's intentional
    # (we don't fabricate stages). Either 6 or 8 is acceptable; assert
    # the behaviour we actually want.
    assert len(plan_hi.tasks) == 6


def test_max_tasks_below_3_rejected():
    with pytest.raises(GoalPlannerError):
        plan_goal(goal_text="x", max_tasks=2)


def test_max_tasks_above_8_rejected():
    with pytest.raises(GoalPlannerError):
        plan_goal(goal_text="x", max_tasks=9)


def test_max_retries_below_zero_rejected():
    with pytest.raises(GoalPlannerError):
        plan_goal(goal_text="x", max_retries=-1)


def test_max_retries_above_10_rejected():
    with pytest.raises(GoalPlannerError):
        plan_goal(goal_text="x", max_retries=11)


def test_max_retries_propagated_to_tasks():
    plan = plan_goal(goal_text="x", max_retries=5)
    assert all(t.max_retries == 5 for t in plan.tasks)


def test_each_task_has_acceptance_criteria():
    plan = plan_goal(goal_text="x")
    for t in plan.tasks:
        assert isinstance(t.acceptance_criteria, list)
        assert len(t.acceptance_criteria) >= 1


def test_each_task_has_required_fields():
    plan = plan_goal(goal_text="x")
    for t in plan.tasks:
        assert isinstance(t, GoalTask)
        assert t.title
        assert t.description
        assert t.priority >= 1
        assert t.suggested_profile
        assert t.workspace == "scratch"
        assert t.max_retries == 3


def test_default_workspace_is_scratch():
    plan = plan_goal(goal_text="x")
    assert all(t.workspace == "scratch" for t in plan.tasks)


def test_explicit_workspace_propagates():
    plan = plan_goal(
        goal_text="x", workspace="dir:/abs/path",
    )
    assert all(t.workspace == "dir:/abs/path" for t in plan.tasks)


def test_default_profile_overrides_per_stage_profile():
    plan = plan_goal(goal_text="x", default_profile="solo")
    assert all(t.suggested_profile == "solo" for t in plan.tasks)


def test_goal_text_appears_in_first_task_title_abbreviated():
    plan = plan_goal(goal_text="인스타 자동화 파이프라인 만들기")
    # short form (≤50 chars) is woven in; check it's there
    assert "인스타" in plan.tasks[0].title
    # title shape: "[1/6] <short> — 현황 분석"
    assert "[1/6]" in plan.tasks[0].title
    assert "—" in plan.tasks[0].title


def test_long_goal_text_is_abbreviated_in_title():
    long_text = "x" * 200
    plan = plan_goal(goal_text=long_text)
    # the abbreviation should keep titles bounded
    assert all(len(t.title) < 200 for t in plan.tasks)


def test_priority_descending_order_respected():
    plan = plan_goal(goal_text="x")
    # Stages 1-2 priority 3, stages 3-5 priority 2, stage 6 priority 1
    priorities = [t.priority for t in plan.tasks]
    assert priorities == [3, 3, 2, 2, 2, 1]


def test_dangerous_stage_includes_approval_warning():
    plan = plan_goal(goal_text="x")
    # Stage 4 ("핵심 변경 구현") is the one that mentions 위험 / 사용자 승인
    impl = plan.tasks[3]
    assert "사용자 승인" in impl.description


def test_goal_title_carries_original_text():
    plan = plan_goal(goal_text="some really long goal that exceeds 50 chars by quite a lot")
    # goal_title is the FULL original (not abbreviated); only titles are
    # abbreviated.
    assert plan.goal_title == "some really long goal that exceeds 50 chars by quite a lot"
