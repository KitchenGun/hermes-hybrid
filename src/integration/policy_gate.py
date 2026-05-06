"""Policy Gate — diagram-aligned policy decisions on a task (Phase 8).

Phase 8 (2026-05-06) 후 책임 축소:
  * **safety**: profile yaml 의존이 사라져 ``requires_confirmation`` 자체
    가 없어짐. 향후 master 가 직접 사용자 확인 prompt 를 띄우는 패턴으로
    대체.
  * **budget**: 일일 cloud 토큰 cap, allowlist 검증 (defense-in-depth).
  * **tier**: validator decision (retry / tier-up / final-failure) 그대로.

Hermes Master Orchestrator 가 LLM 호출 전 ``pre_dispatch`` 를 부르고,
응답에 ``post_validate`` 를 호출. ``Validator`` 위에 단일 contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from src.config import Settings
from src.state import TaskState
from src.validator import ValidationResult, Validator


PolicyAction = Literal[
    "allow",
    "deny_budget",
    "deny_allowlist",
]


@dataclass
class PolicyDecision:
    action: PolicyAction
    reason: str = ""
    requires_confirmation: bool = False
    profile_id: str | None = None
    job_name: str | None = None


class PolicyGate:
    """Single entry for budget / allowlist / tier policy."""

    def __init__(
        self,
        settings: Settings,
        *,
        repo: Any = None,
        validator: Validator | None = None,
    ):
        self.settings = settings
        self.repo = repo
        self.validator = validator if validator is not None else Validator(settings)

    # ---- pre-dispatch -------------------------------------------------

    async def pre_dispatch(
        self,
        task: TaskState,
        *,
        profile_id: str | None = None,
        job_name: str | None = None,
    ) -> PolicyDecision:
        """Decide whether the task is allowed to proceed to LLM dispatch.

        Order:
          1. allowlist (already enforced at gateway, but defense-in-depth)
          2. daily cloud token budget
        """
        # 1. allowlist — gateway enforces but we double-check.
        if self.settings.require_allowlist and self.settings.allowed_user_ids:
            try:
                uid = int(task.user_id)
            except (TypeError, ValueError):
                uid = -1
            if uid not in self.settings.allowed_user_ids:
                return PolicyDecision(
                    action="deny_allowlist",
                    reason=f"user_id {task.user_id} not in allowlist",
                    profile_id=profile_id,
                    job_name=job_name,
                )

        # 2. daily token budget
        if self.repo is not None:
            try:
                used = await self.repo.used_tokens_today(task.user_id)
            except Exception:  # noqa: BLE001
                used = 0
            if used >= self.settings.cloud_token_budget_daily:
                return PolicyDecision(
                    action="deny_budget",
                    reason=(
                        f"daily token budget reached "
                        f"({used}/{self.settings.cloud_token_budget_daily})"
                    ),
                    profile_id=profile_id,
                    job_name=job_name,
                )

        return PolicyDecision(
            action="allow",
            profile_id=profile_id,
            job_name=job_name,
        )

    # ---- post-validate ------------------------------------------------

    def post_validate(
        self,
        task: TaskState,
        *,
        output_text: str,
        expected_schema: str | None = None,
        timed_out: bool = False,
        tool_error: bool = False,
        hermes_turns_used: int = 0,
    ) -> ValidationResult:
        """Delegate to the existing Validator. Surfaced here so callers
        only depend on PolicyGate, not on Validator directly — when
        retry policy moves to a richer model later, only this method
        changes."""
        return self.validator.validate(
            task,
            output_text=output_text,
            expected_schema=expected_schema,
            timed_out=timed_out,
            tool_error=tool_error,
            hermes_turns_used=hermes_turns_used,
        )


__all__ = ["PolicyAction", "PolicyDecision", "PolicyGate"]
