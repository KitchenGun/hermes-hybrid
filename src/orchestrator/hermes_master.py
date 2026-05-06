"""HermesMasterOrchestrator — diagram-aligned single entry point.

All-via-master design (Phase plan 2026-05-06):

    Discord/Telegram message
        ↓
    IntentRouter — short-circuits RuleLayer / slash skills + parse @handles
        ↓ (else)
    PolicyGate — allowlist / budget
        ↓ (allow)
    JobInventory.agent_by_handle() — @handle SKILL.md lookup
        ↓
    OpenCodeAdapter — opencode CLI / gpt-5.5  ← THE single LLM call
                       (system prompt + agent snippets injected if @handles found)
        ↓
    Critic — self_score + ExperienceLog stamp
        ↓
    Response back to gateway

Phase 9 (2026-05-06): IntentRouter 가 ``@coder`` / ``@reviewer`` 같은
mention 을 감지하면, master 가 해당 sub-agent SKILL.md frontmatter
(role / when_to_use / not_for / inputs / outputs) 를 system prompt 에
inject. 이로써 master 가 sub-agent 의 행동 가이드를 따르도록 유도.
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
            agent_handles=list(getattr(intent, "agent_handles", []) or []),
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

        # Branch 4a — Phase 10: parallel agent fan-out (opt-in).
        if (
            self.settings.master_parallel_agents
            and len(task.agent_handles) >= 2
        ):
            return await self._dispatch_parallel_agents(task, intent, t0)

        # Branch 4b — master LLM dispatch (the diagram's heart)
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

    # ---- Phase 10: parallel agent fan-out ----------------------------

    async def _dispatch_parallel_agents(
        self,
        task: TaskState,
        intent: Any,
        t0: float,
    ) -> "MasterResult":
        """Fan out one opencode call per ``@handle`` in parallel.

        Triggered only when ``settings.master_parallel_agents=True`` AND
        ``len(task.agent_handles) >= 2``. Single-handle messages still go
        through the normal master path so single-shot prompts stay simple
        and the experience log row matches the original Phase 9 shape.
        """
        from src.core.delegation import (
            OpenCodeAgentDelegator,
            SubAgentRequest,
            aggregate_responses,
        )

        delegator = OpenCodeAgentDelegator(
            self.opencode,
            self.job_inventory._agent_registry(),  # noqa: SLF001 — same instance
            max_concurrency=self.settings.master_parallel_max_concurrency,
        )

        requests = [
            SubAgentRequest(
                agent_handle=handle,
                user_message=task.user_message,
                parent_task_id=task.task_id,
                parent_session_id=task.session_id,
                context={
                    "trigger_type": task.trigger_type,
                    "trigger_source": task.trigger_source or "",
                },
            )
            for handle in task.agent_handles
        ]

        log.info(
            "master.parallel_dispatch_start",
            handles=task.agent_handles,
            max_concurrency=self.settings.master_parallel_max_concurrency,
        )
        results = await delegator.delegate_many(requests)

        # Stamp aggregate token cost + tools onto the task so the
        # ExperienceLog reflects N opencode calls accurately.
        any_failed = False
        for r in results:
            task.record_model_output(
                tier="C1",
                text=r.response,
                model_name="gpt-5.5",
                prompt_tokens=r.prompt_tokens,
                completion_tokens=r.completion_tokens,
                substage=f"parallel:{r.request.agent_handle}",
            )
            if not r.success:
                any_failed = True
                task.record_error(
                    "tool_error",
                    f"{r.request.agent_handle}: {r.error}",
                )

        task.model_provider = "opencode"
        task.model_name = "gpt-5.5"
        aggregated = aggregate_responses(results)
        task.final_response = aggregated

        if all(r.success for r in results):
            task.status = "succeeded"
            handled_by = "master:parallel"
        elif any(r.success for r in results):
            task.status = "succeeded"
            task.degraded = True
            handled_by = "master:parallel_partial"
        else:
            task.status = "failed"
            task.degraded = True
            handled_by = "master:parallel_failed"

        log.info(
            "master.parallel_dispatch_end",
            handled_by=handled_by,
            agents=len(results),
            successes=sum(1 for r in results if r.success),
        )
        self._finalize(task, handled_by=handled_by, t0=t0)
        return MasterResult(
            task=task,
            response=aggregated,
            handled_by=handled_by,
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
        """Stitch a system prompt + agent snippets + user message.

        Phase 9: ``intent.agent_handles`` 에 발견된 sub-agent 핸들이 있으면
        각각의 SKILL.md frontmatter 를 짧은 snippet 으로 변환해 system
        prompt 에 inject. master 가 해당 agent 의 책임/경계/입출력을 따라
        응답하도록 유도.

        snippet 포맷:
            ## Active sub-agent: @coder (role: write_new_code)
            description: 신규 모듈/기능을 작성하는 sub-agent.
            when_to_use:
              - 새 모듈/기능
              - greenfield 작성
            not_for:
              - 외과적 수정 (→ @editor)
            inputs: [...]
            outputs: [...]
        """
        parts: list[str] = [_SYSTEM_PROMPT]

        handles = list(getattr(intent, "agent_handles", []) or [])
        if handles:
            for handle in handles:
                snippet = self._agent_snippet(handle)
                if snippet:
                    parts.append(snippet)
            log.info(
                "master.agent_injected",
                handles=handles,
                injected=sum(
                    1 for h in handles if self.job_inventory.agent_by_handle(h)
                ),
            )

        parts.append("## User\n" + task.user_message)
        return "\n\n".join(parts)

    def _agent_snippet(self, handle: str) -> str:
        """Compose a compact prompt snippet from an agent's SKILL.md frontmatter.

        Returns empty string if the handle doesn't resolve in AgentRegistry —
        IntentRouter already filters unknown handles, but this is the second
        line of defense.
        """
        entry = self.job_inventory.agent_by_handle(handle)
        if entry is None:
            return ""

        lines: list[str] = [
            f"## Active sub-agent: {entry.handle} (role: {entry.role or '—'})",
        ]
        if entry.description:
            lines.append(f"description: {entry.description}")
        if entry.when_to_use:
            lines.append("when_to_use:")
            lines.extend(f"  - {item}" for item in entry.when_to_use)
        if entry.not_for:
            lines.append("not_for:")
            lines.extend(f"  - {item}" for item in entry.not_for)
        if entry.inputs:
            lines.append(f"inputs: {', '.join(entry.inputs)}")
        if entry.outputs:
            lines.append(f"outputs: {', '.join(entry.outputs)}")
        if entry.primary_tools:
            lines.append(
                f"primary_tools: {', '.join(entry.primary_tools)}"
            )
        return "\n".join(lines)

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
