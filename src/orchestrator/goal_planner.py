"""Goal planner — deterministic goal → Kanban task decomposition (P3).

LLM-free fallback so the ``/goal`` mini command always produces something
predictable and testable. The hardcoded 6-stage blueprint covers the
common shape of "build / fix / extend feature X" workflows; the goal
text is woven into each task title and body so the result feels
specific even without an LLM.

Future iterations may swap ``plan_goal`` for an LLM-backed planner;
the public surface (``plan_goal`` returning ``GoalPlan``) stays the
same so callers (``GoalSkill``) don't change.

Safety:
  * dangerous categories (auto execution, secret rotation, schema
    rewrite) are flagged in description with "사용자 승인 필요" so the
    downstream worker reads it before acting.
  * ``max_retries`` is clamped to [0, 10].
  * ``max_tasks`` is clamped to [3, 8].
  * ``workspace`` is **not** parsed here — that's done by the caller
    (``GoalSkill``) using :func:`src.core.kanban.workspace.parse_workspace_spec`
    so absolute-path validation lives in one place.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class GoalPlannerError(ValueError):
    """Raised on invalid goal_text or out-of-range bounds."""


@dataclass(frozen=True)
class GoalTask:
    title: str
    description: str
    priority: int
    suggested_profile: str
    acceptance_criteria: list[str]
    workspace: str  # "scratch" or "dir:<abs>"
    max_retries: int


@dataclass(frozen=True)
class GoalPlan:
    goal_title: str
    tasks: list[GoalTask] = field(default_factory=list)


# Stage blueprint: (stage_label, description_seed, priority, profile, AC list).
# Priority follows Kanban dispatcher semantics — higher = sooner.
_BLUEPRINT: list[tuple[str, str, int, str, list[str]]] = [
    (
        "현황 분석",
        "관련 코드 / 문서 / 요구사항을 파악하고 핵심 제약을 정리한다.",
        3,
        "researcher",
        ["분석 대상 명시", "발견된 제약 ≥ 3개 정리"],
    ),
    (
        "설계안 작성",
        "분석 결과를 바탕으로 설계안 1~2 개를 명시하고 trade-off 를 적는다.",
        3,
        "architect",
        ["설계안 ≥ 1개 제시", "trade-off 명시"],
    ),
    (
        "구현 계획 수립",
        "어떤 파일을 어떤 순서로 변경할지 step-by-step 계획을 작성한다.",
        2,
        "planner",
        ["변경 파일 목록", "단계별 순서"],
    ),
    (
        "핵심 변경 구현",
        "계획대로 코드를 작성한다. 위험 / 자동 실행 / 시크릿 변경은 사용자 승인 필요.",
        2,
        "coder",
        ["주요 코드 변경 적용", "관련 단위 테스트 추가"],
    ),
    (
        "테스트 및 검증",
        "단위 / 회귀 / smoke 테스트로 회귀 0 을 확인한다.",
        2,
        "tester",
        ["pytest 통과", "회귀 미검출 확인"],
    ),
    (
        "결과 보고 및 문서화",
        "변경 요약 + 사용 예시 + 후속 추천 + 운영 포인트를 문서화한다.",
        1,
        "writer",
        ["변경 요약", "운영 가이드 ≥ 1 항목"],
    ),
]


def _abbrev(s: str, limit: int) -> str:
    flat = " ".join(s.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def plan_goal(
    *,
    goal_text: str,
    workspace: str | None = None,
    default_profile: str | None = None,
    max_tasks: int = 6,
    max_retries: int = 3,
) -> GoalPlan:
    """Decompose ``goal_text`` into 3-8 deterministic Kanban tasks.

    Args:
        goal_text: free-form natural-language goal. Required.
        workspace: workspace spec ('scratch' or 'dir:<abs>'). Defaults to
            'scratch'. NOT parsed here — caller validates.
        default_profile: when set, overrides the per-stage profile (rare
            override; use only when one specialist owns the whole goal).
        max_tasks: clamp [3, 8]. Defaults to 6 (full blueprint).
        max_retries: per-task retry budget. Clamp [0, 10]. Defaults to 3.

    Raises:
        GoalPlannerError: on empty goal_text or out-of-range bounds.
    """
    text = (goal_text or "").strip()
    if not text:
        raise GoalPlannerError("goal_text is required")
    if not (0 <= max_retries <= 10):
        raise GoalPlannerError("max_retries must be in [0, 10]")
    if not (3 <= max_tasks <= 8):
        raise GoalPlannerError("max_tasks must be in [3, 8]")

    ws = workspace or "scratch"
    selected = _BLUEPRINT[:max_tasks]
    short = _abbrev(text, 50)
    tasks: list[GoalTask] = []
    for idx, (stage, desc, pri, profile, ac) in enumerate(selected, 1):
        title = f"[{idx}/{len(selected)}] {short} — {stage}"
        body_lines = [
            f"{stage}: {desc}",
            "",
            f"원본 목표: {text}",
        ]
        # Surface explicit warning for stages that may touch the user's
        # filesystem / external services.
        if "위험" in desc or "사용자 승인" in desc:
            body_lines.append("")
            body_lines.append("⚠️ 사용자 승인 필요한 위험 작업이 포함될 수 있음.")
        tasks.append(
            GoalTask(
                title=title,
                description="\n".join(body_lines),
                priority=pri,
                suggested_profile=default_profile or profile,
                acceptance_criteria=list(ac),
                workspace=ws,
                max_retries=max_retries,
            )
        )
    return GoalPlan(goal_title=text, tasks=tasks)


__all__ = ["GoalPlan", "GoalPlannerError", "GoalTask", "plan_goal"]
