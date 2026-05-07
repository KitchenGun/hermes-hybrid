"""Experiment Runner — Phase 21 (2026-05-07).

A/B 분기 결정론적 도구. task_id hash 기반으로 control/treatment arm 할당.

설계:
  * 결정론 — 같은 task_id + 같은 experiment_name 은 항상 같은 arm.
    재시도가 같은 arm 으로 돌아가야 측정 가설이 깨지지 않는다.
  * 동률 분포 — sha256 의 첫 8 hex 를 100 으로 나눈 bucket 사용.
    ratio 0.5 면 bucket < 50 시 treatment.
  * Stateless — 외부 저장 X. settings 만 읽음.
  * Privacy — task_id 는 UUID4 라 user 식별 불가.

Phase 17 와 묶여서 Week 1 도입. memory_inject 의 행동 변화 효과 측정 후
Phase 18 SKILL auto-promote 평가, Phase 19 timer 도입 회귀 감지, Phase 20
feedback loop 효과 측정에도 재사용.
"""
from __future__ import annotations

import hashlib
from typing import Literal

Arm = Literal["control", "treatment"]


class ExperimentRunner:
    """Deterministic A/B arm assigner.

    Usage::

        runner = ExperimentRunner(name="memory_inject", treatment_ratio=0.5)
        arm = runner.assign(task_id)  # "control" or "treatment"
    """

    def __init__(
        self,
        *,
        name: str,
        treatment_ratio: float,
        enabled: bool = True,
    ) -> None:
        self.name = name
        # Clamp into [0.0, 1.0]; out-of-range values would silently break
        # the bucket math otherwise.
        self.treatment_ratio = max(0.0, min(1.0, treatment_ratio))
        self.enabled = enabled

    def assign(self, task_id: str) -> Arm:
        """Return the arm for this task_id.

        Disabled runner always returns ``"control"`` so caller code paths
        remain identical regardless of the toggle.
        """
        if not self.enabled or self.treatment_ratio <= 0.0:
            return "control"
        if self.treatment_ratio >= 1.0:
            return "treatment"

        h = hashlib.sha256(
            f"{self.name}:{task_id}".encode("utf-8")
        ).hexdigest()[:8]
        bucket = int(h, 16) % 100
        threshold = int(self.treatment_ratio * 100)
        return "treatment" if bucket < threshold else "control"


__all__ = ["ExperimentRunner", "Arm"]
