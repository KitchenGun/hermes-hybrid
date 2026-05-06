"""HermesMasterOrchestrator — diagram-aligned single entry point.

All-via-master design (Phase plan 2026-05-06):

    Discord/Telegram message
        ↓
    IntentRouter — short-circuits RuleLayer / slash skills / forced profile
        ↓ (else)
    PolicyGate — allowlist / budget
        ↓ (allow)
    JobInventory — profile + job + skill specs to compose the master prompt
        ↓
    OpenCodeAdapter — opencode CLI / gpt-5.5  ← THE single LLM call
        ↓
    Critic — self_score + ExperienceLog stamp
        ↓
    Response back to gateway

The legacy ``Orchestrator._dispatch_with_retries`` chain (ollama / Claude
CLI tier ladder + JobFactory v1/v2 dispatcher) is bypassed entirely when
the master path is taken. The legacy code stays alive in this commit
(default OFF, opt-in via ``master_enabled=True``) — Phase 4 of the plan
is the actual deletion.

Why a separate class instead of mutating Orchestrator:
  * keeps the master code path independently testable
  * Orchestrator.handle delegates to this class only when
    ``master_enabled=True``, so default test behavior is unchanged
  * enables a clean `git rm` of the legacy dispatch in the next commit
    without touching master logic
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from src.config import Settings
from src.core import Critic, ExperienceLogger
from src.integration import IntentRouter, JobInventory, PolicyGate
from src.memory import InMemoryMemory, MemoryBackend
from src.obs import bind_task_id, get_logger
from src.opencode_adapter import (
    OpenCodeAdapter,
    OpenCodeAdapterError,
    OpenCodeAuthError,
    OpenCodeTimeout,
)
from src.skills import SkillContext, SkillRegistry, default_registry
from src.state import Repository, TaskState
from src.validator import Validator

log = get_logger(__name__)


_SYSTEM_PROMPT = (
    "You are Hermes Master, a personal-agent orchestrator. You receive "
    "the user's message, a brief profile/job inventory, and any "
    "relevant memos. Decide what the user needs and reply directly. "
    "Be concise, use Korean when the user does. If the user asks for "
    "code, produce runnable code."
)


class HermesMasterOrchestrator:
    """Single-entry orchestrator backed by ``opencode`` master LLM."""

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
        self.memory: MemoryBackend = (
            memory if memory is not None else InMemoryMemory()
        )

        self.skills: SkillRegistry = (
            skills if skills is not None else default_registry(settings)
        )
        self.intent_router = IntentRouter(settings, skills=self.skills)
        self.policy_gate = PolicyGate(
            settings,
            repo=repo,
            validator=Validator(settings),
        )
        self.job_inventory = JobInventory(
            repo_root=getattr(settings, "_repo_root", None),
        )
        self.opencode = OpenCodeAdapter(settings)
        self.critic = Critic(self.policy_gate.validator)
        self.experience_logger: ExperienceLogger = (
            experience_logger
            if experience_logger is not None
            else ExperienceLogger(
                settings.experience_log_root,
                enabled=settings.experience_log_enabled,
            )
        )

    # ---- public entry ------------------------------------------------

    async def handle(
        self,
        user_message: str,
        *,
        user_id: str,
        session_id: str | None = None,
        history: list[dict[str, str]] | None = None,
        heavy: bool = False,
        forced_profile: str | None = None,
    ) -> "MasterResult":
        """Drive a single user request through the diagram pipeline."""
        session_id = session_id or str(uuid.uuid4())
        t0 = time.perf_counter()

        history_window = list(history or [])
        memory_inject_count = await self._maybe_inject_memory(
            user_id, user_message, history_window
        )

        intent = await self.intent_router.route(
            user_message=user_message,
            user_id=user_id,
            session_id=session_id,
            forced_profile=forced_profile,
            heavy=heavy,
            memory=self.memory,
            repo=self.repo,
            orchestrator=self,
        )

        # Build the persistent task — every branch ends with
        # _log_task_end so the ExperienceLog gets a single row per
        # request regardless of which short-circuit fired.
        task = TaskState(
            session_id=session_id,
            user_id=user_id,
            user_message=user_message,
            history_window=history_window,
            heavy=heavy,
            forced_profile=forced_profile,
            trigger_type=intent.trigger_type,
            trigger_source=intent.trigger_source,
            memory_inject_count=memory_inject_count,
            slash_skill=intent.slash_skill,
            job_id=intent.job_id,
            job_category=intent.job_category,
            skill_ids=list(intent.skill_ids),
        )
        task.mark("created_at")

        bind_task_id(task.task_id)
        log.info(
            "master.start",
            handled_by_intent=intent.handled_by,
            forced_profile=forced_profile,
            heavy=heavy,
        )

        # Branch 1 — RuleLayer hit
        if intent.handled_by == "rule" and intent.response is not None:
            task.status = "succeeded"
            task.final_response = intent.response
            self._finalize(task, handled_by="rule", t0=t0)
            return MasterResult(task=task, response=intent.response, handled_by="rule")

        # Branch 2 — slash skill hit
        if intent.skill_match is not None:
            skill, skill_match = intent.skill_match
            ctx = self.intent_router.build_skill_context(
                user_id=user_id,
                session_id=session_id,
                memory=self.memory,
                repo=self.repo,
                orchestrator=self,
            )
            try:
                resp = await skill.invoke(skill_match, ctx)
                task.status = "succeeded"
                task.final_response = resp
            except Exception as e:  # noqa: BLE001
                log.warning("master.skill_failed", skill=skill.name, err=str(e))
                task.status = "failed"
                task.degraded = True
                body = str(e)[:400]
                resp = (
                    f"⚠️ skill `{skill.name}` failed: "
                    f"`{type(e).__name__}`\n```\n{body}\n```"
                )
                task.final_response = resp
            handled_by = f"skill:{skill.name}"
            self._finalize(task, handled_by=handled_by, t0=t0)
            return MasterResult(task=task, response=resp, handled_by=handled_by)

        # Branch 3 — policy gate
        decision = await self.policy_gate.pre_dispatch(
            task,
            profile_id=intent.profile_id,
            job_name=intent.job_id,
        )
        if decision.action == "deny_allowlist":
            return self._reject(
                task, t0,
                handled_by="deny:allowlist",
                response="⚠️ 사용 권한이 없는 사용자입니다.",
            )
        if decision.action == "deny_budget":
            return self._reject(
                task, t0,
                handled_by="deny:budget",
                response=f"⚠️ {decision.reason}",
            )

        # Branch 4 — master LLM dispatch (the diagram's heart)
        return await self._dispatch_master(task, intent, t0)

    # ---- master dispatch ---------------------------------------------

    async def _dispatch_master(
        self,
        task: TaskState,
        intent: Any,
        t0: float,
    ) -> "MasterResult":
        prompt = self._compose_prompt(task, intent)
        try:
            result = await self.opencode.run(
                prompt=prompt,
                history=task.history_window,
            )
        except OpenCodeAuthError as e:
            task.record_error("tool_error", f"opencode auth: {e}")
            task.status = "failed"
            task.degraded = True
            task.final_response = (
                "⚠️ opencode 인증/할당량 오류. WSL 에서 `opencode auth login` "
                "후 봇 재시작."
            )
            self._finalize(task, handled_by="master:auth_error", t0=t0)
            return MasterResult(
                task=task, response=task.final_response,
                handled_by="master:auth_error",
            )
        except OpenCodeTimeout as e:
            task.record_error("timeout", f"opencode timeout: {e}")
            task.status = "failed"
            task.degraded = True
            task.final_response = "⚠️ master 응답 시간 초과."
            self._finalize(task, handled_by="master:timeout", t0=t0)
            return MasterResult(
                task=task, response=task.final_response,
                handled_by="master:timeout",
            )
        except OpenCodeAdapterError as e:
            task.record_error("tool_error", f"opencode error: {e}")
            task.status = "failed"
            task.degraded = True
            task.final_response = f"⚠️ master 호출 실패: `{type(e).__name__}`"
            self._finalize(task, handled_by="master:error", t0=t0)
            return MasterResult(
                task=task, response=task.final_response,
                handled_by="master:error",
            )

        # Stamp model context onto the task before validation.
        task.model_provider = "opencode"
        task.model_name = result.model_name
        task.record_model_output(
            tier="C1",
            text=result.text,
            model_name=result.model_name,
            prompt_tokens=result.input_tokens,
            completion_tokens=result.output_tokens,
            substage="master",
        )

        verdict = self.critic.evaluate(
            task,
            output_text=result.text,
            timed_out=False,
            tool_error=False,
        )

        if verdict.decision == "pass" or verdict.decision == "retry_same_tier":
            # In Phase 3 we don't actually retry — the master path is
            # single-shot. Phase 5b adds tool-call iteration; until then
            # we accept whatever the master returned and let Critic's
            # self_score signal quality.
            task.status = "succeeded"
            task.final_response = result.text
            self._finalize(task, handled_by="master:opencode", t0=t0)
            return MasterResult(
                task=task, response=result.text,
                handled_by="master:opencode",
            )

        # final_failure / tier_up etc. — single-shot for now.
        task.status = "failed"
        task.degraded = True
        task.final_response = (
            result.text
            or f"⚠️ master 응답 검증 실패 ({verdict.reason})."
        )
        self._finalize(task, handled_by="master:degraded", t0=t0)
        return MasterResult(
            task=task,
            response=task.final_response,
            handled_by="master:degraded",
        )

    # ---- helpers ------------------------------------------------------

    async def _maybe_inject_memory(
        self,
        user_id: str,
        user_message: str,
        history_window: list[dict[str, str]],
    ) -> int:
        if not (
            self.settings.memory_inject_enabled
            and user_message.strip()
        ):
            return 0
        try:
            hits = await self.memory.search(
                user_id, user_message,
                k=self.settings.memory_inject_top_k,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("master.memory_search_failed", err=str(e))
            hits = []
        if not hits:
            return 0
        bullets = "\n".join(f"- {m.text}" for m in hits)
        history_window.insert(
            0,
            {
                "role": "system",
                "content": "관련 사용자 메모 (참고만):\n" + bullets,
            },
        )
        log.info(
            "master.memory_injected", user_id=user_id, hits=len(hits)
        )
        return len(hits)

    def _compose_prompt(
        self, task: TaskState, intent: Any
    ) -> str:
        """Stitch a system prompt + user message.

        Phase 8 후 profile/job context 가 사라졌으므로 system prompt 만.
        향후 master 가 IntentRouter 결과의 ``@coder`` 같은 mention 을
        인식하면 해당 agent 의 SKILL.md 를 inject 하는 wiring 이 추가될
        예정 (Phase 9).
        """
        return _SYSTEM_PROMPT + "\n\n## User\n" + task.user_message

    def _reject(
        self,
        task: TaskState,
        t0: float,
        *,
        handled_by: str,
        response: str,
        degraded: bool = False,
    ) -> "MasterResult":
        task.status = "failed"
        task.degraded = degraded
        task.final_response = response
        self._finalize(task, handled_by=handled_by, t0=t0)
        return MasterResult(task=task, response=response, handled_by=handled_by)

    def _finalize(
        self, task: TaskState, *, handled_by: str, t0: float
    ) -> None:
        task.mark("finalized_at")
        latency_ms = int((time.perf_counter() - t0) * 1000)
        log.info(
            "master.task_end",
            handled_by=handled_by,
            status=task.status,
            degraded=task.degraded,
            latency_ms=latency_ms,
            self_score=task.internal_confidence,
        )
        try:
            self.experience_logger.append(
                task, handled_by=handled_by, latency_ms=latency_ms
            )
        except Exception as e:  # noqa: BLE001
            log.warning("master.experience_log_failed", err=str(e))


# ---- result type -----------------------------------------------------


class MasterResult:
    """Lightweight result so callers don't depend on
    Orchestrator.OrchestratorResult."""

    def __init__(
        self,
        *,
        task: TaskState,
        response: str,
        handled_by: str,
    ):
        self.task = task
        self.response = response
        self.handled_by = handled_by


__all__ = ["HermesMasterOrchestrator", "MasterResult"]
