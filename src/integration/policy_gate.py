"""Policy Gate — diagram-aligned policy decisions on a task.

Wraps three orthogonal concerns the diagram lumps together:
  * **safety**: ``requires_confirmation`` (HITL gate) per profile job
  * **budget**: daily cloud token cap, per-user in-flight cap
  * **tier**: validator decision (retry / tier-up / final-failure) on
    LLM outputs

The Hermes Master Orchestrator calls :meth:`pre_dispatch` before any
LLM call to check budget / safety, and :meth:`post_validate` on the
result to decide retry / escalate. Existing implementations
(:class:`Validator`, :class:`ProfileLoader`, repo budget tracking) are
delegated to — the Policy Gate is the orchestrator-facing single
contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from src.config import Settings
from src.orchestrator.profile_loader import ProfileLoader
from src.state import TaskState
from src.validator import ValidationResult, Validator


PolicyAction = Literal[
    "allow",
    "deny_budget",
    "deny_allowlist",
    "needs_confirmation",
]


@dataclass
class PolicyDecision:
    action: PolicyAction
    reason: str = ""
    requires_confirmation: bool = False
    profile_id: str | None = None
    job_name: str | None = None


class PolicyGate:
    """Single entry for safety / budget / tier policy."""

    def __init__(
        self,
        settings: Settings,
        *,
        repo: Any = None,
        validator: Validator | None = None,
        profile_loader: ProfileLoader | None = None,
    ):
        self.settings = settings
        self.repo = repo
        self.validator = validator if validator is not None else Validator(settings)
        self.profile_loader = (
            profile_loader
            if profile_loader is not None
            else ProfileLoader(settings.profiles_dir)
        )

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
          3. requires_confirmation on the matched profile job (HITL)
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

        # 3. HITL — does this profile job declare requires_confirmation?
        if profile_id and job_name and self.settings.hitl_enabled:
            try:
                needs = self.profile_loader.requires_confirmation(
                    profile_id, job_name
                )
            except Exception:  # noqa: BLE001
                needs = False
            if needs:
                return PolicyDecision(
                    action="needs_confirmation",
                    reason="profile job declares safety.requires_confirmation",
                    requires_confirmation=True,
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
