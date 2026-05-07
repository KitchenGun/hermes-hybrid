"""Tests for ExperimentRunner (src/core/experiment_runner.py).

Phase 21 (2026-05-07).

Locks down:
  * Determinism — same (name, task_id) always yields the same arm
  * 50/50 split — large-n distribution is within ±5% of expected
  * Boundary ratios — 0.0 → all control, 1.0 → all treatment
  * Disabled runner → all control regardless of ratio
  * Different experiment names → uncorrelated arm assignment
"""
from __future__ import annotations

import uuid

from src.core.experiment_runner import ExperimentRunner


def test_assign_is_deterministic():
    runner = ExperimentRunner(name="exp", treatment_ratio=0.5)
    tid = "fixed-task-id-123"
    a1 = runner.assign(tid)
    a2 = runner.assign(tid)
    a3 = runner.assign(tid)
    assert a1 == a2 == a3


def test_ratio_zero_is_all_control():
    runner = ExperimentRunner(name="exp", treatment_ratio=0.0)
    for _ in range(200):
        assert runner.assign(str(uuid.uuid4())) == "control"


def test_ratio_one_is_all_treatment():
    runner = ExperimentRunner(name="exp", treatment_ratio=1.0)
    for _ in range(200):
        assert runner.assign(str(uuid.uuid4())) == "treatment"


def test_disabled_runner_is_all_control():
    """Phase 21 토글 OFF 시 분기 코드가 항상 control 만 반환."""
    runner = ExperimentRunner(
        name="exp", treatment_ratio=0.5, enabled=False,
    )
    for _ in range(200):
        assert runner.assign(str(uuid.uuid4())) == "control"


def test_ratio_clamps_out_of_range_inputs():
    """음수/1 초과 ratio 는 silently clamp."""
    r1 = ExperimentRunner(name="exp", treatment_ratio=-0.5)
    r2 = ExperimentRunner(name="exp", treatment_ratio=1.5)
    assert r1.treatment_ratio == 0.0
    assert r2.treatment_ratio == 1.0


def test_distribution_is_close_to_target_ratio():
    """10000 random task_id 에서 50% ratio 가 50% ± 5% 안에 들어와야."""
    runner = ExperimentRunner(name="exp", treatment_ratio=0.5)
    n = 10000
    treatment_count = sum(
        1 for _ in range(n)
        if runner.assign(str(uuid.uuid4())) == "treatment"
    )
    pct = treatment_count / n
    # ±5% tolerance — sha256 pseudo-uniformity over 10k draws is tight.
    assert 0.45 <= pct <= 0.55, f"expected ~50%, got {pct:.3f}"


def test_distribution_at_25_percent():
    runner = ExperimentRunner(name="exp", treatment_ratio=0.25)
    n = 10000
    treatment_count = sum(
        1 for _ in range(n)
        if runner.assign(str(uuid.uuid4())) == "treatment"
    )
    pct = treatment_count / n
    assert 0.22 <= pct <= 0.28, f"expected ~25%, got {pct:.3f}"


def test_different_experiment_names_yield_uncorrelated_arms():
    """동일 task_id 라도 name 다르면 다른 bucket — 동시 실험 간섭 방지."""
    r_a = ExperimentRunner(name="exp_a", treatment_ratio=0.5)
    r_b = ExperimentRunner(name="exp_b", treatment_ratio=0.5)
    same_arm_count = 0
    n = 5000
    for _ in range(n):
        tid = str(uuid.uuid4())
        if r_a.assign(tid) == r_b.assign(tid):
            same_arm_count += 1
    pct = same_arm_count / n
    # Expected ~50% if independent. Tolerate ±5%.
    assert 0.45 <= pct <= 0.55, (
        f"different experiment names should be independent, got {pct:.3f}"
    )
