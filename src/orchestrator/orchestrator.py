"""Orchestrator — diagram-aligned thin wrapper.

After the all-via-master migration (2026-05-06) the legacy ollama / Claude
CLI tier ladder + JobFactory v1/v2 + Router lives in :mod:`hermes_master`
+ :mod:`integration` packages. ``Orchestrator`` is now the public entry
that the gateway (Discord, Telegram) talks to:

  * ``handle`` delegates to :class:`HermesMasterOrchestrator`
  * ``replay`` re-runs a prior task by id
  * ``get_status`` reads a TaskState back
  * HITL surface (``requires_confirmation`` / ``enter_confirmation_gate``
    / ``record_confirmation_message`` / ``resume_after_confirmation`` /
    ``list_pending_confirmations`` / ``build_preview``) — confirmation
    flow lives here because it spans persistence + Discord rendering,
    not the master path.

The class is intentionally lightweight; everything dispatch-related is
delegated. ``master_enabled=False`` is a transient compatibility flag
for unit tests that don't want the master path; production always has
it ON. Once the test surface is fully ported (Phase 7) the flag goes
away.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src.config import Settings
from src.core import Critic, ExperienceLogger
from src.integration import IntentRouter
from src.memory import InMemoryMemory, MemoryBackend
from src.obs import bind_task_id, get_logger
from src.orchestrator.profile_loader import JobMeta, ProfileLoader
from src.skills import SkillRegistry, default_registry
from src.state import ConfirmationContext, Repository, TaskState
from src.validator import Validator

log = get_logger(__name__)


@dataclass
class OrchestratorResult:
    task: TaskState
    response: str
    handled_by: str


class Orchestrator:
    """Public entry. Delegates dispatch to HermesMasterOrchestrator
    when ``master_enabled=True``; otherwise returns a degraded notice
    (the legacy ollama/Claude CLI tier ladder was removed in commit-4)."""

    def __init__(
        self,
        settings: Settings,
        repo: Repository | None = None,
        *,
        skills: SkillRegistry | None = None,
        memory: MemoryBackend | None = None,
        experience_logger: ExperienceLogger | None = None,
    ):
        self.settings = settings
        self.repo = repo
        self.skills: SkillRegistry = (
            skills if skills is not None else default_registry(settings)
        )
        self.memory: MemoryBackend = (
            memory if memory is not None else InMemoryMemory()
        )
        self.experience_logger: ExperienceLogger = (
            experience_logger
            if experience_logger is not None
            else ExperienceLogger(
                settings.experience_log_root,
                enabled=settings.experience_log_enabled,
            )
        )
        # HITL relies on a profile yaml index — kept as a runtime
        # component because gateway code asks ``requires_confirmation``
        # at button-click time without going through the master.
        self.profile_loader = ProfileLoader(settings.profiles_dir)
        # Critic / Validator stay accessible to the gateway in case it
        # wants to score a manually-constructed response (e.g. confirm
        # gate replays).
        self.validator = Validator(settings)
        self.critic = Critic(self.validator)
        # IntentRouter handles RuleLayer + slash skill short-circuits
        # *without* touching the master LLM. Useful when master_enabled
        # is False (unit tests) — slash commands like /memo, /hybrid-*
        # still work.
        self.intent_router = IntentRouter(settings, skills=self.skills)

        # Per-user in-flight gate (R13).
        self._user_locks: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(settings.per_user_in_flight_max)
        )

        # Master is lazy-built on first delegation so unit tests that
        # never call ``handle`` don't pay the construction cost.
        self._hermes_master: Any = None

    # ---- public entry ----

    async def handle(
        self,
        user_message: str,
        *,
        user_id: str,
        session_id: str | None = None,
        history: list[dict[str, str]] | None = None,
        heavy: bool = False,
        forced_profile: str | None = None,
    ) -> OrchestratorResult:
        async with self._user_locks[user_id]:
            # 1. Try short-circuits via IntentRouter — RuleLayer hit and
            #    slash skill matches don't need the master LLM at all,
            #    so they work even when master_enabled is False.
            short = await self._try_short_circuit(
                user_message,
                user_id=user_id,
                session_id=session_id,
                forced_profile=forced_profile,
                heavy=heavy,
            )
            if short is not None:
                return short

            # 2. Master path — only if enabled.
            if self.settings.master_enabled:
                return await self._delegate_to_master(
                    user_message,
                    user_id=user_id,
                    session_id=session_id,
                    history=history,
                    heavy=heavy,
                    forced_profile=forced_profile,
                )

            # 3. Master disabled — surface a clear hint rather than a
            #    silent failure. Legacy dispatch is gone (commit-4).
            task = TaskState(
                session_id=session_id or "no-session",
                user_id=user_id,
                user_message=user_message,
                heavy=heavy,
                forced_profile=forced_profile,
            )
            task.status = "failed"
            task.degraded = True
            task.final_response = (
                "⚠️ master_enabled=False — 기존 dispatch 가 제거된 환경에서는 "
                "응답이 불가능합니다. .env 에 ``MASTER_ENABLED=true`` 설정 후 "
                "opencode CLI 인증 (1회) 하세요."
            )
            return OrchestratorResult(
                task=task,
                response=task.final_response,
                handled_by="master:disabled",
            )

    async def _try_short_circuit(
        self,
        user_message: str,
        *,
        user_id: str,
        session_id: str | None,
        forced_profile: str | None,
        heavy: bool,
    ) -> OrchestratorResult | None:
        """RuleLayer / slash skill short-circuit before master dispatch.

        Returns None when nothing short-circuited — caller proceeds to
        master path (or the disabled fallback).
        """
        intent = await self.intent_router.route(
            user_message=user_message,
            user_id=user_id,
            session_id=session_id or "",
            forced_profile=forced_profile,
            heavy=heavy,
            memory=self.memory,
            repo=self.repo,
            orchestrator=self,
        )

        if intent.handled_by == "rule" and intent.response is not None:
            task = self._task_for_short_circuit(
                user_message, user_id=user_id, session_id=session_id,
                heavy=heavy, forced_profile=forced_profile, intent=intent,
            )
            task.status = "succeeded"
            task.final_response = intent.response
            return OrchestratorResult(
                task=task, response=intent.response, handled_by="rule",
            )

        if intent.skill_match is not None:
            skill, skill_match = intent.skill_match
            ctx = self.intent_router.build_skill_context(
                user_id=user_id,
                session_id=session_id or "",
                memory=self.memory,
                repo=self.repo,
                orchestrator=self,
            )
            handled = f"skill:{skill.name}"
            task = self._task_for_short_circuit(
                user_message, user_id=user_id, session_id=session_id,
                heavy=heavy, forced_profile=forced_profile, intent=intent,
            )
            try:
                resp = await skill.invoke(skill_match, ctx)
                task.status = "succeeded"
                task.final_response = resp
            except Exception as e:  # noqa: BLE001
                log.warning("skill.error", skill=skill.name, err=str(e))
                task.status = "failed"
                task.degraded = True
                body = str(e)[:400]
                resp = (
                    f"⚠️ skill `{skill.name}` failed: "
                    f"`{type(e).__name__}`\n```\n{body}\n```"
                )
                task.final_response = resp
            return OrchestratorResult(
                task=task, response=resp, handled_by=handled,
            )

        return None

    @staticmethod
    def _task_for_short_circuit(
        user_message: str,
        *,
        user_id: str,
        session_id: str | None,
        heavy: bool,
        forced_profile: str | None,
        intent: Any,
    ) -> TaskState:
        task = TaskState(
            session_id=session_id or "no-session",
            user_id=user_id,
            user_message=user_message,
            heavy=heavy,
            forced_profile=forced_profile,
            trigger_type=intent.trigger_type,
            trigger_source=intent.trigger_source,
            slash_skill=intent.slash_skill,
            job_id=intent.job_id,
            job_category=intent.job_category,
        )
        task.mark("created_at")
        return task

    async def _delegate_to_master(
        self,
        user_message: str,
        *,
        user_id: str,
        session_id: str | None,
        history: list[dict[str, str]] | None,
        heavy: bool,
        forced_profile: str | None,
    ) -> OrchestratorResult:
        if self._hermes_master is None:
            from src.orchestrator.hermes_master import HermesMasterOrchestrator
            self._hermes_master = HermesMasterOrchestrator(
                self.settings,
                self.repo,
                skills=self.skills,
                memory=self.memory,
                experience_logger=self.experience_logger,
            )
        result = await self._hermes_master.handle(
            user_message,
            user_id=user_id,
            session_id=session_id,
            history=history,
            heavy=heavy,
            forced_profile=forced_profile,
        )
        return OrchestratorResult(
            task=result.task,
            response=result.response,
            handled_by=result.handled_by,
        )

    async def replay(self, task_id: str) -> OrchestratorResult | None:
        """R4: re-run a previously failed task with a fresh budget."""
        if self.repo is None:
            return None
        prior = await self.repo.get_task(task_id)
        if prior is None:
            return None
        return await self.handle(
            prior.user_message,
            user_id=prior.user_id,
            session_id=prior.session_id,
            history=prior.history_window,
        )

    async def get_status(self, task_id: str) -> TaskState | None:
        if self.repo is None:
            return None
        return await self.repo.get_task(task_id)

    # ---- HITL (human-in-the-loop) ----

    def requires_confirmation(self, profile_id: str, job_name: str) -> bool:
        """Quick check — does this profile job declare
        ``safety.requires_confirmation``? Returns ``False`` if HITL is
        globally disabled or the job has no safety section."""
        if not self.settings.hitl_enabled:
            return False
        return self.profile_loader.requires_confirmation(profile_id, job_name)

    async def enter_confirmation_gate(
        self,
        task: TaskState,
        *,
        profile_id: str,
        job_name: str,
        preview_title: str,
        preview_body: str,
        pending_payload: dict[str, Any],
        preview_color: int = 0xFEE75C,
    ) -> ConfirmationContext:
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self.settings.hitl_timeout_seconds
        )
        ctx = ConfirmationContext(
            profile_id=profile_id,
            job_name=job_name,
            preview_title=preview_title,
            preview_body=preview_body,
            preview_color=preview_color,
            pending_payload=pending_payload,
            expires_at=expires_at,
        )
        task.confirmation_context = ctx
        task.status = "awaiting_confirmation"
        task.mark("awaiting_confirmation_at")
        await self._persist(task)
        log.info(
            "hitl.gate_entered",
            task_id=task.task_id,
            profile_id=profile_id,
            job_name=job_name,
            expires_at=expires_at.isoformat(),
        )
        return ctx

    async def record_confirmation_message(
        self, task_id: str, *, message_id: int, channel_id: int
    ) -> None:
        if self.repo is None:
            return
        task = await self.repo.get_task(task_id)
        if task is None or task.confirmation_context is None:
            return
        task.confirmation_context = task.confirmation_context.model_copy(
            update={
                "discord_message_id": message_id,
                "discord_channel_id": channel_id,
            }
        )
        task.touch()
        await self._persist(task)

    async def resume_after_confirmation(
        self,
        task_id: str,
        *,
        decision: str,
        actor_user_id: str,
    ) -> tuple[TaskState, bool] | None:
        if self.repo is None:
            return None
        task = await self.repo.get_task(task_id)
        if task is None or task.status != "awaiting_confirmation":
            return None
        if str(task.user_id) != str(actor_user_id):
            log.warning(
                "hitl.actor_mismatch",
                task_id=task_id,
                owner=task.user_id,
                actor=actor_user_id,
            )
            return None

        ctx = task.confirmation_context
        if ctx is not None and ctx.is_expired() and decision == "confirm":
            decision = "timeout"

        if decision == "confirm":
            task.status = "acting"
            task.mark("confirmed_at")
            await self._persist(task)
            log.info("hitl.confirmed", task_id=task_id)
            return task, True

        task.status = "failed"
        task.degraded = True
        reason = "사용자 취소" if decision == "cancel" else "확인 시간 초과"
        task.final_response = f"⚠️ {reason}으로 실행을 건너뜁니다. (task `{task_id}`)"
        task.mark("finalized_at")
        await self._persist(task)
        log.info("hitl.declined", task_id=task_id, decision=decision)
        return task, False

    async def list_pending_confirmations(self) -> list[TaskState]:
        if self.repo is None:
            return []
        return await self.repo.list_awaiting_confirmations()

    def build_preview(
        self,
        meta: JobMeta,
        pending_payload: dict[str, Any],
    ) -> tuple[str, str, int]:
        title = f"📝 {meta.job_name} 실행 확인"
        color = 0xFEE75C
        lines = [
            f"프로파일: `{meta.profile_id}`",
            f"잡: `{meta.job_name}`",
        ]
        if meta.description:
            lines.append(f"설명: {meta.description}")
        if pending_payload:
            for k, v in list(pending_payload.items())[:8]:
                text = str(v)
                if len(text) > 80:
                    text = text[:77] + "..."
                lines.append(f"• **{k}**: {text}")
        lines.append("\n[확인] / [취소]")
        return title, "\n".join(lines), color

    # ---- persistence ----

    async def _persist(self, task: TaskState) -> None:
        if self.repo is not None:
            try:
                await self.repo.save_task(task)
            except Exception as e:  # noqa: BLE001
                log.warning("repo.save_failed", err=str(e))


__all__ = ["BudgetExhausted", "Orchestrator", "OrchestratorResult"]


class BudgetExhausted(RuntimeError):
    """Retained for callers that catch it; legacy dispatch raised this
    when daily token budget tripped. The master path returns a graceful
    response instead, so this is now only triggered by external code
    that emulates the legacy behavior."""
