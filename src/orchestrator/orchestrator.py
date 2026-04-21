"""Orchestrator — routing + policy only. Does NOT execute tools.

Key invariants (risk-fixed):
  - Claude (C2) is reached ONLY via explicit user opt-in (`!heavy ...`).
    Automatic validator-driven escalation stops at C1 (R2, R9 + heavy path).
  - When Ollama is disabled, local/worker use GPT-4o-mini / GPT-4o
    SURROGATES — never Claude — with a strict token cap. (R3)
  - Per-session Claude-call budget is enforced here, not silently ignored.
  - Daily per-user cloud-token budget is enforced via Repository.
  - same_tier_retries is reset on tier switch (R8).
  - Per-user in-flight requests capped by semaphore (R13).
  - TaskState is persisted so /retry and /status actually work (R4).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass

from src.claude_adapter import (
    ClaudeCodeAdapter,
    ClaudeCodeAdapterError,
    ClaudeCodeAuthError,
    ClaudeCodeResumeFailed,
    ClaudeCodeTimeout,
)
from src.config import Settings
from src.hermes_adapter import (
    HermesAdapter,
    HermesAdapterError,
    HermesAuthError,
    HermesTimeout,
)
from src.llm import (
    AnthropicClient,
    LLMAuthError,
    LLMConnectionError,
    LLMRateLimitError,
    LLMTimeoutError,
    OllamaClient,
    OpenAIClient,
)
from src.obs import bind_task_id, get_logger
from src.memory import InMemoryMemory, MemoryBackend
from src.orchestrator.bump import compress_for_bump
from src.orchestrator.heavy_session import HeavySessionRegistry
from src.router import Router, RouterDecision, RuleLayer, RuleMatch
from src.skills import SkillContext, SkillRegistry, default_registry
from src.state import Repository, TaskState, Tier
from src.validator import Validator

log = get_logger(__name__)

_SYSTEM_PROMPT = (
    "You are a concise assistant inside a Discord bot. "
    "Answer directly. If the user asks for code, produce runnable code."
)


@dataclass
class OrchestratorResult:
    task: TaskState
    response: str
    # "rule" | "local" | "worker" | "local-surrogate" | "worker-surrogate"
    # | "cloud-gpt" | "claude-max" | "claude-auth" | "claude-timeout"
    # | "claude-error" | "hermes-auth" | "llm-auth" | "budget"
    handled_by: str


class BudgetExhausted(RuntimeError):
    pass


class Orchestrator:
    def __init__(
        self,
        settings: Settings,
        repo: Repository | None = None,
        *,
        skills: SkillRegistry | None = None,
        memory: MemoryBackend | None = None,
    ):
        self.settings = settings
        self.rules = RuleLayer()
        self.router = Router(settings)
        self.validator = Validator(settings)
        self.hermes = HermesAdapter(settings)
        self.claude_code = ClaudeCodeAdapter(settings)
        # Separate Claude CLI instance for C1 (Haiku) with its own semaphore
        # so it doesn't serialize behind C2/heavy's concurrency cap of 1.
        # Only actually invoked when ``c1_backend == "claude_cli"``; idle
        # otherwise (cheap to hold — no subprocess at construction time).
        self.claude_code_c1 = ClaudeCodeAdapter(
            settings, concurrency=settings.c1_claude_code_concurrency
        )
        # FIX#4: per-user heavy-path session reuse (10-min window).
        self.heavy_sessions = HeavySessionRegistry()
        # Phase 2: skill surface + memory backend. Both injectable for tests;
        # default wiring matches the production topology.
        self.skills: SkillRegistry = skills if skills is not None else default_registry()
        self.memory: MemoryBackend = memory if memory is not None else InMemoryMemory()
        self.repo = repo  # may be None for CLI/tests

        # Lazy clients
        self._openai_main: OpenAIClient | None = None
        self._openai_surrogate_local: OpenAIClient | None = None
        self._openai_surrogate_worker: OpenAIClient | None = None
        self._anthropic: AnthropicClient | None = None
        self._ollama_local: OllamaClient | None = None
        self._ollama_worker: OllamaClient | None = None

        # R13: per-user in-flight gate
        self._user_locks: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(settings.per_user_in_flight_max)
        )

        # Phase 3: scratch — reset per attempt, carries Hermes turns_used
        # into the validator so `trust_hermes_reflection` can short-circuit.
        self._last_hermes_turns: int = 0

    # ---- public entry ----

    async def handle(
        self,
        user_message: str,
        *,
        user_id: str,
        session_id: str | None = None,
        history: list[dict[str, str]] | None = None,
        heavy: bool = False,
    ) -> OrchestratorResult:
        session_id = session_id or str(uuid.uuid4())
        task = TaskState(
            session_id=session_id,
            user_id=user_id,
            user_message=user_message,
            history_window=history or [],
            retry_budget=self.settings.retry_budget_default,
            token_budget_remaining=self.settings.cloud_token_budget_session,
            heavy=heavy,
        )
        task.mark("created_at")

        async with self._user_locks[user_id]:
            return await self._handle_locked(task)

    async def replay(self, task_id: str) -> OrchestratorResult | None:
        """R4: Re-run a previously failed task with a fresh retry budget."""
        if self.repo is None:
            return None
        prior = await self.repo.get_task(task_id)
        if prior is None:
            return None
        # Build a fresh state from the prior user_message but new task_id
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

    # ---- core ----

    async def _handle_locked(self, task: TaskState) -> OrchestratorResult:
        with bind_task_id(task.task_id, task.user_id):
            t0 = time.perf_counter()
            log.info("task.start", message=task.user_message[:120], heavy=task.heavy)

            # Opt-in heavy path: skip rule layer + router, go directly to
            # Claude Code CLI. Daily token budget still applies as a safety
            # net, even though Max OAuth usage isn't metered in tokens.
            if task.heavy:
                return await self._handle_heavy(task, t0)

            # 1. Rule layer
            match = self.rules.match(task.user_message)
            if match is not None:
                resp = await self._handle_rule(match, task)
                task.status = "succeeded"
                task.final_response = resp
                task.mark("finalized_at")
                await self._persist(task)
                self._log_task_end(task, "rule", t0)
                return OrchestratorResult(task=task, response=resp, handled_by="rule")

            # 1.5. Skill surface (Phase 2). Skills own their slash commands
            # end-to-end — no router, no LLM, no cloud budget. A skill hit
            # short-circuits before token accounting, so `/memo list` etc.
            # stay free and deterministic.
            skill_hit = self.skills.match(task.user_message)
            if skill_hit is not None:
                skill, skill_match = skill_hit
                handled = f"skill:{skill.name}"
                ctx = SkillContext(
                    settings=self.settings,
                    repo=self.repo,
                    memory=self.memory,
                    user_id=task.user_id,
                    session_id=task.session_id,
                    orchestrator=self,
                )
                try:
                    resp = await skill.invoke(skill_match, ctx)
                    task.status = "succeeded"
                    task.final_response = resp
                except Exception as e:  # noqa: BLE001
                    log.warning("skill.error", skill=skill.name, err=str(e))
                    task.status = "failed"
                    task.degraded = True
                    task.final_response = (
                        f"⚠️ skill `{skill.name}` failed: `{type(e).__name__}`"
                    )
                    resp = task.final_response
                task.mark("finalized_at")
                await self._persist(task)
                self._log_task_end(task, handled, t0)
                return OrchestratorResult(task=task, response=resp, handled_by=handled)

            # 2. Daily budget check (R4)
            if self.repo is not None:
                used = await self.repo.used_tokens_today(task.user_id)
                if used >= self.settings.cloud_token_budget_daily:
                    task.status = "failed"
                    task.degraded = True
                    task.final_response = (
                        f"⚠️ Daily cloud token budget reached ({used}/"
                        f"{self.settings.cloud_token_budget_daily})."
                    )
                    task.mark("finalized_at")
                    await self._persist(task)
                    self._log_task_end(task, "budget", t0)
                    return OrchestratorResult(task=task, response=task.final_response, handled_by="rule")

            # 3. Router
            decision = await self.router.decide(
                task.user_message,
                history_window=task.history_window,
            )
            task.route = decision.route
            task.router_confidence = decision.confidence
            task.router_reason = decision.reason
            task.requires_planning = decision.requires_planning
            log.info("router.decision", **decision.to_dict())

            # 4. Dispatch
            task.switch_tier(self._initial_tier(decision))
            task.status = "acting"
            task.mark("first_act_at")
            handled_by = await self._dispatch_with_retries(task)

            task.mark("finalized_at")
            await self._persist(task)

            # Daily token ledger update
            if self.repo is not None:
                cloud_tokens = sum(
                    mo.prompt_tokens + mo.completion_tokens
                    for mo in task.model_outputs
                    if mo.tier in ("C1", "C2")
                )
                if cloud_tokens > 0:
                    await self.repo.add_tokens(task.user_id, cloud_tokens)

            self._log_task_end(task, handled_by, t0)
            return OrchestratorResult(task=task, response=task.final_response, handled_by=handled_by)

    async def _dispatch_with_retries(self, task: TaskState) -> str:
        handled_by = "local"
        while True:
            # Phase 3: Hermes-lane methods stash turns_used on this scratch
            # attribute; validator consults it when trust_hermes_reflection
            # is on. Non-Hermes lanes leave it at 0 which is a no-op.
            self._last_hermes_turns = 0
            try:
                text, handled_by = await self._execute_once(task)
                timed_out = False
                tool_error = False
            except HermesTimeout as e:
                text = ""; timed_out = True; tool_error = False
                log.warning("hermes.timeout", err=str(e))
            except HermesAuthError as e:
                # Auth errors are non-retryable — degrade immediately.
                task.record_error("tool_error", f"hermes auth: {e}", tier=task.current_tier)
                task.status = "failed"; task.degraded = True
                task.final_response = (
                    "⚠️ Hermes authentication failed. "
                    "Check ANTHROPIC_API_KEY in ~/.hermes/.env and restart."
                )
                return "hermes-auth"
            except HermesAdapterError as e:
                text = ""; timed_out = False; tool_error = True
                log.warning("hermes.error", err=str(e))
            except ClaudeCodeAuthError as e:
                # C1-via-Claude-CLI hit Max OAuth / quota error. Non-retryable.
                task.record_error("tool_error", f"claude cli auth: {e}", tier=task.current_tier)
                task.status = "failed"; task.degraded = True
                task.final_response = (
                    "⚠️ Claude CLI (C1 Haiku) auth/quota failed. "
                    "Run `claude /login` in WSL to refresh the Max OAuth token, "
                    "or wait for the hourly Max quota to reset."
                )
                return "claude-auth"
            except ClaudeCodeTimeout as e:
                text = ""; timed_out = True; tool_error = False
                log.warning("claude_code.timeout_c1", err=str(e))
            except ClaudeCodeAdapterError as e:
                text = ""; timed_out = False; tool_error = True
                log.warning("claude_code.error_c1", err=str(e))
            except LLMTimeoutError as e:
                text = ""; timed_out = True; tool_error = False
                log.warning("llm.timeout", err=str(e))
            except LLMRateLimitError as e:
                # Rate-limit → delay then retry same tier
                log.warning("llm.rate_limit", err=str(e))
                await asyncio.sleep(2.0)
                text = ""; timed_out = True; tool_error = False
            except LLMAuthError as e:
                task.record_error("tool_error", f"llm auth: {e}", tier=task.current_tier)
                task.status = "failed"; task.degraded = True
                task.final_response = f"⚠️ Cloud auth failed ({e}). Check API keys."
                return "llm-auth"
            except (LLMConnectionError, Exception) as e:  # noqa: BLE001
                text = ""; timed_out = False; tool_error = True
                log.warning("llm.error", err=str(e))

            verdict = self.validator.validate(
                task,
                output_text=text,
                expected_schema=None,
                timed_out=timed_out,
                tool_error=tool_error,
                hermes_turns_used=self._last_hermes_turns,
            )
            log.info(
                "validator.verdict",
                decision=verdict.decision,
                reason=verdict.reason,
                tier=task.current_tier,
                route=task.route,
            )

            if verdict.decision == "pass":
                task.status = "succeeded"
                task.final_response = text
                task.bump_prefix = ""  # FIX#2: clear on success
                return handled_by

            if verdict.decision == "final_failure":
                task.status = "failed"
                task.degraded = True
                task.final_response = self._degraded_response(task, verdict.reason)
                return handled_by

            task.retry_count += 1
            task.status = "retrying"

            # FIX#2: compress the just-failed attempt into a ≤200-char
            # breadcrumb for the next call. Non-cumulative — always reflects
            # only the *last* model output, so bump length is bounded even
            # after many retries.
            payload = compress_for_bump(task, verdict)
            task.bump_prefix = payload.summary_line

            if verdict.decision == "retry_same_tier":
                task.same_tier_retries += 1
                continue

            if verdict.decision in ("tier_up", "escalate_cloud", "escalate_claude"):
                assert verdict.next_tier is not None
                log.info(
                    "tier.escalated",
                    from_tier=task.current_tier,
                    to_tier=verdict.next_tier,
                    kind=verdict.decision,
                    reason=verdict.reason,
                )
                task.switch_tier(verdict.next_tier)  # R8: resets same_tier_retries
                task.tier_up_retries += 1
                continue

            task.status = "failed"; task.degraded = True
            task.final_response = self._degraded_response(task, "unknown verdict")
            return handled_by

    # ---- per-attempt dispatch ----

    async def _execute_once(self, task: TaskState) -> tuple[str, str]:
        tier = task.current_tier
        if tier == "L2":
            return await self._run_local_tier(task, worker=False)
        if tier == "L3":
            return await self._run_local_tier(task, worker=True)
        if tier == "C1":
            return await self._run_c1(task)
        if tier == "C2":
            return await self._run_c2(task)
        raise RuntimeError(f"Unknown tier {tier}")

    async def _run_local_tier(self, task: TaskState, *, worker: bool) -> tuple[str, str]:
        """R3: Local/worker tier.

        Three paths:
          1. ``USE_HERMES_FOR_LOCAL=true`` → HermesAdapter v2 drives the
             turn with provider pinned via Router (FIX#1 + FIX#5). Phase 1
             rollout path; off by default while we're still in migration.
          2. Ollama enabled → direct Ollama 14B/32B (local-first policy).
          3. Else → GPT-4o-mini (local) / GPT-4o (worker) surrogate,
             strictly capped in tokens, never escalated to Claude.
        """
        # --- Phase 1 path: Hermes-driven ------------------------------------
        # effective_* factors in the Phase 3 USE_HERMES_EVERYWHERE master.
        if self.settings.effective_use_hermes_for_local:
            return await self._run_local_via_hermes(task, worker=worker)

        # --- Legacy paths (unchanged) ---------------------------------------
        if self.settings.ollama_enabled:
            client = self._ollama_worker_client() if worker else self._ollama_local_client()
            resp = await client.generate(
                self._messages(task),
                max_tokens=(
                    self.settings.surrogate_max_tokens_worker if worker
                    else self.settings.surrogate_max_tokens_local
                ),
            )
            task.record_model_output(
                tier=task.current_tier, text=resp.text, model_name=resp.model,
                prompt_tokens=resp.prompt_tokens, completion_tokens=resp.completion_tokens,
            )
            return resp.text, "worker" if worker else "local"

        # Surrogate path — explicitly marked as cloud but NOT via Hermes, NOT Claude.
        client = self._openai_surrogate_worker_client() if worker else self._openai_surrogate_local_client()
        cap = (
            self.settings.surrogate_max_tokens_worker if worker
            else self.settings.surrogate_max_tokens_local
        )
        resp = await client.generate(self._messages(task), max_tokens=cap)
        task.record_model_output(
            tier=task.current_tier, text=resp.text, model_name=resp.model,
            prompt_tokens=resp.prompt_tokens, completion_tokens=resp.completion_tokens,
        )
        tag = "worker-surrogate" if worker else "local-surrogate"
        return resp.text, tag

    async def _run_local_via_hermes(
        self, task: TaskState, *, worker: bool
    ) -> tuple[str, str]:
        """Phase 1 path: L2/L3 through HermesAdapter v2.

        Provider selection:
          - ``ollama_enabled=True`` → provider='ollama', model = work/worker
            model from settings (the same model the legacy path would have
            picked, so latency is comparable).
          - Else → provider='openai', model = surrogate model. Hermes still
            runs the plan/act/reflect loop but with OpenAI as the tool-call
            LLM — which is equivalent to the legacy surrogate for single-turn
            answers but lets us smoke the Hermes wiring end-to-end.

        In either case we pin the provider with ``--provider`` (enforced in
        HermesAdapter v2 via ``HermesProviderMismatch``) so claude-code is
        structurally unreachable from this lane.
        """
        if self.settings.ollama_enabled:
            model = (
                self.settings.ollama_worker_model if worker
                else self.settings.ollama_work_model
            )
            provider = "ollama"
            tag = "worker-hermes" if worker else "local-hermes"
        else:
            model = (
                self.settings.openai_model_worker_surrogate if worker
                else self.settings.openai_model_local_surrogate
            )
            provider = "openai"
            tag = "worker-hermes-surrogate" if worker else "local-hermes-surrogate"

        # Bump prefix is injected into the query the same way _messages()
        # builds the user content for legacy lanes — Hermes gets one clean
        # prompt per turn, keeping its plan/act/reflect loop focused.
        query = task.user_message
        if task.bump_prefix:
            query = f"{task.bump_prefix}\n\n{task.user_message}"

        result = await self.hermes.run(
            query,
            model=model,
            provider=provider,
            # Cap turns to keep L2/L3 snappy; Hermes' own --max-turns becomes
            # the R2 budget (HermesBudgetExceeded if it overruns).
            max_turns=min(self.settings.hermes_max_turns, 5),
        )
        self._last_hermes_turns = result.turns_used
        task.record_model_output(
            tier=task.current_tier,
            text=result.text,
            model_name=result.primary_model or result.model_name,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
        return result.text, tag

    async def _run_c1(self, task: TaskState) -> tuple[str, str]:
        """C1: planning tier.

        Three paths (first match wins):
          1. ``USE_HERMES_FOR_C1=true`` → HermesAdapter v2 with
             provider='openai' pinned. Phase 2 rollout; off by default.
          2. ``C1_BACKEND=claude_cli`` → direct Claude Code CLI with the
             Haiku model. Zero per-token cost (Max OAuth), immune to
             OpenAI TPM limits. Added after the 2026-04-21 incident where
             the 72-skill Hermes system prompt blew past the 30k TPM cap.
             Unlike C2, this lane does NOT persist sessions — C1 is a
             single-turn planner, and heavy-session reuse is reserved for
             `!heavy`.
          3. Else → direct GPT-4o (unchanged legacy path).

        Claude **can** be reached from this lane via path #2, but only
        the lightweight Haiku model and only when the operator explicitly
        flips ``C1_BACKEND=claude_cli``. Heavy Sonnet/Opus usage stays
        gated behind `!heavy` regardless.
        """
        if self.settings.effective_use_hermes_for_c1:
            return await self._run_c1_via_hermes(task)

        if self.settings.c1_backend == "claude_cli":
            return await self._run_c1_via_claude_cli(task)

        resp = await self._openai_main_client().generate(self._messages(task))
        task.record_model_output(
            tier="C1", text=resp.text, model_name=resp.model,
            prompt_tokens=resp.prompt_tokens, completion_tokens=resp.completion_tokens,
        )
        return resp.text, "cloud-gpt"

    async def _run_c1_via_claude_cli(self, task: TaskState) -> tuple[str, str]:
        """C1 through the Claude Code CLI with the Haiku model.

        Single-turn, stateless: we flatten system prompt + recent history
        + bump breadcrumb into a single stdin payload and read back the
        JSON ``result`` field. No ``--resume``, no session persistence
        (those belong to the heavy path).

        Errors flow through :class:`ClaudeCodeAuthError` /
        :class:`ClaudeCodeTimeout` / :class:`ClaudeCodeAdapterError` —
        the dispatch loop catches them and maps auth to a non-retryable
        failure, timeout to a retryable same-tier signal, and adapter
        errors to ``tool_error``.
        """
        # History here is passed through the adapter's own stdin flattener,
        # but we still need to surface the system prompt and the bump
        # breadcrumb. _SYSTEM_PROMPT goes at the front of the prompt line
        # so the Haiku model has the same "concise Discord assistant"
        # framing the OpenAI path uses via the system message.
        user_content = task.user_message
        if task.bump_prefix:
            user_content = f"{task.bump_prefix}\n\n{task.user_message}"
        prompt = f"{_SYSTEM_PROMPT}\n\n{user_content}"

        result = await self.claude_code_c1.run(
            prompt=prompt,
            history=task.history_window,
            model=self.settings.c1_claude_code_model,
            timeout_ms=self.settings.c1_claude_code_timeout_ms,
            persist_session=False,
        )
        task.record_model_output(
            tier="C1",
            text=result.text,
            model_name=result.model_name or self.settings.c1_claude_code_model,
            prompt_tokens=result.input_tokens,
            completion_tokens=result.output_tokens,
        )
        return result.text, "cloud-claude-cli"

    async def _run_c1_via_hermes(self, task: TaskState) -> tuple[str, str]:
        """Phase 2 path: C1 through HermesAdapter v2 with OpenAI pinned.

        Same structure as :meth:`_run_local_via_hermes` (Phase 1 lane), just
        with the main ``openai_model`` instead of a surrogate and a larger
        turn cap — C1 is the planning tier, so plan/act/reflect actually
        earns its cost here (unlike L2/L3 where it's mostly a smoke test).

        Provider is pinned to ``"openai"``. The adapter raises
        :class:`HermesProviderMismatch` if Hermes drifts to anything else,
        so Claude remains structurally unreachable from the auto-escalation
        ladder.
        """
        query = task.user_message
        if task.bump_prefix:
            query = f"{task.bump_prefix}\n\n{task.user_message}"

        result = await self.hermes.run(
            query,
            model=self.settings.openai_model,
            provider="openai",
            max_turns=self.settings.hermes_max_turns,
        )
        self._last_hermes_turns = result.turns_used
        task.record_model_output(
            tier="C1",
            text=result.text,
            model_name=result.primary_model or result.model_name,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
        return result.text, "cloud-gpt-hermes"

    async def _run_c2(self, task: TaskState) -> tuple[str, str]:
        """C2: Claude via Claude Code CLI (Max subscription OAuth).

        Reached ONLY via the heavy path (`!heavy ...`). Never invoked by
        validator-driven auto-escalation — the validator's tier-up map caps
        at C1 for that reason. If somehow called outside heavy, we still run
        through this path; current_tier=="C2" is the contract.

        Two paths:
          1. ``USE_HERMES_FOR_HEAVY=true`` → HermesAdapter v2 with
             provider='claude-code' pinned. Hermes owns plan/act/reflect;
             Claude is the reasoning step. Phase 2b rollout; off by default.
          2. Else → direct ClaudeCodeAdapter with FIX#4 session reuse (10-min
             TTL, fresh-fallback on resume failure). Unchanged legacy path.
        """
        if self.settings.effective_use_hermes_for_heavy:
            return await self._run_c2_via_hermes(task)

        prior_sid = self.heavy_sessions.pick(task.user_id)
        try:
            result = await self.claude_code.run(
                prompt=task.user_message,
                history=task.history_window,
                resume_session_id=prior_sid,
                persist_session=True,  # keep session alive for next heavy turn
            )
        except ClaudeCodeResumeFailed as e:
            log.warning(
                "heavy.resume_failed",
                user_id=task.user_id,
                old_session_id=prior_sid,
                reason=str(e)[:200],
            )
            self.heavy_sessions.invalidate(task.user_id, reason="resume_failed")
            # Fresh retry — no resume flag, new session.
            result = await self.claude_code.run(
                prompt=task.user_message,
                history=task.history_window,
                resume_session_id=None,
                persist_session=True,
            )

        # Remember the new session id for the next !heavy turn (if we got one).
        if result.session_id:
            self.heavy_sessions.record(task.user_id, result.session_id)

        task.record_model_output(
            tier="C2",
            text=result.text,
            model_name=result.model_name or "claude-code-max",
            prompt_tokens=result.input_tokens,
            completion_tokens=result.output_tokens,
        )
        return result.text, "claude-max"

    async def _run_c2_via_hermes(self, task: TaskState) -> tuple[str, str]:
        """Phase 2b path: heavy through HermesAdapter with claude-code pinned.

        Hermes drives plan/act/reflect with Claude as the reasoning step
        and whatever tools Hermes exposes (local files, MCP servers, etc.)
        as the action steps. Provider is pinned to ``"claude-code"`` so the
        adapter's R1 check (FIX#5 / :class:`HermesProviderMismatch`) guards
        against Hermes silently falling back to a cheaper provider on the
        heavy lane — the opposite of what we want.

        Session reuse (FIX#4) is handled via Hermes' ``--resume`` flag
        rather than ClaudeCodeAdapter's. On a Hermes-side resume failure
        we invalidate the registry and retry fresh, symmetric to the
        legacy heavy path.
        """
        prior_sid = self.heavy_sessions.pick(task.user_id)
        model = self.settings.claude_code_model  # reuse the heavy-model knob

        try:
            result = await self.hermes.run(
                task.user_message,
                model=model,
                provider="claude-code",
                resume_session=prior_sid,
                max_turns=self.settings.hermes_max_turns,
                timeout_ms=self.settings.claude_code_timeout_ms,
            )
        except HermesAdapterError as e:
            # Treat any adapter failure on a resume as a possible session
            # eviction — invalidate + retry fresh once, mirroring the
            # legacy path's ClaudeCodeResumeFailed handling.
            if prior_sid is not None:
                log.warning(
                    "heavy.hermes_resume_failed",
                    user_id=task.user_id,
                    old_session_id=prior_sid,
                    reason=str(e)[:200],
                )
                self.heavy_sessions.invalidate(task.user_id, reason="hermes_resume_failed")
                result = await self.hermes.run(
                    task.user_message,
                    model=model,
                    provider="claude-code",
                    resume_session=None,
                    max_turns=self.settings.hermes_max_turns,
                    timeout_ms=self.settings.claude_code_timeout_ms,
                )
            else:
                raise

        if result.session_id:
            self.heavy_sessions.record(task.user_id, result.session_id)

        self._last_hermes_turns = result.turns_used
        task.record_model_output(
            tier="C2",
            text=result.text,
            model_name=result.primary_model or result.model_name,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
        return result.text, "claude-max-hermes"

    async def _handle_heavy(self, task: TaskState, t0: float) -> OrchestratorResult:
        """Heavy path: direct Claude Code CLI invocation, no tiers, no retries.

        We deliberately skip the validator + retry loop here — the user
        explicitly chose this path, and retrying a failed Max call just burns
        session quota. On error we degrade and surface a clear message.
        """
        task.route = "cloud"
        task.requires_planning = True  # informational; heavy is always "plan-ish"
        task.switch_tier("C2")
        task.status = "acting"
        task.mark("first_act_at")

        try:
            text, handled_by = await self._run_c2(task)
            task.status = "succeeded"
            task.final_response = text
        except ClaudeCodeAuthError as e:
            log.warning("claude_code.auth_error", err=str(e))
            task.record_error("tool_error", f"claude auth/quota: {e}", tier="C2")
            task.status = "failed"
            task.degraded = True
            task.final_response = (
                "⚠️ Claude heavy path unavailable — Max session quota or "
                "OAuth token issue. Try again later, or run `claude /login` "
                "in WSL to refresh the token."
            )
            handled_by = "claude-auth"
        except ClaudeCodeTimeout as e:
            log.warning("claude_code.timeout", err=str(e))
            task.record_error("timeout", f"claude timeout: {e}", tier="C2")
            task.status = "failed"
            task.degraded = True
            task.final_response = (
                f"⚠️ Claude heavy path timed out after "
                f"{self.settings.claude_code_timeout_ms // 1000}s."
            )
            handled_by = "claude-timeout"
        except ClaudeCodeAdapterError as e:
            log.warning("claude_code.error", err=str(e))
            task.record_error("tool_error", f"claude error: {e}", tier="C2")
            task.status = "failed"
            task.degraded = True
            task.final_response = f"⚠️ Claude heavy path failed: {e}"
            handled_by = "claude-error"
        except HermesAuthError as e:
            # Phase 2b: heavy-via-hermes can surface Hermes-side auth errors
            # (the Hermes CLI's own OAuth / credentials path). Treat them
            # the same as claude-auth from the user's perspective — the
            # actionable fix is still "refresh the Max OAuth token".
            log.warning("hermes.auth_error_on_heavy", err=str(e))
            task.record_error("tool_error", f"hermes auth on heavy: {e}", tier="C2")
            task.status = "failed"
            task.degraded = True
            task.final_response = (
                "⚠️ Claude heavy path unavailable — Hermes couldn't "
                "authenticate to Claude Code. Try again later, or run "
                "`claude /login` in WSL to refresh the token."
            )
            handled_by = "hermes-auth"
        except HermesTimeout as e:
            log.warning("hermes.timeout_on_heavy", err=str(e))
            task.record_error("timeout", f"hermes timeout on heavy: {e}", tier="C2")
            task.status = "failed"
            task.degraded = True
            task.final_response = (
                f"⚠️ Claude heavy path (via Hermes) timed out after "
                f"{self.settings.claude_code_timeout_ms // 1000}s."
            )
            handled_by = "claude-timeout"
        except HermesAdapterError as e:
            log.warning("hermes.error_on_heavy", err=str(e))
            task.record_error("tool_error", f"hermes error on heavy: {e}", tier="C2")
            task.status = "failed"
            task.degraded = True
            task.final_response = f"⚠️ Claude heavy path (via Hermes) failed: {e}"
            handled_by = "claude-error"

        task.mark("finalized_at")
        await self._persist(task)

        if self.repo is not None:
            cloud_tokens = sum(
                mo.prompt_tokens + mo.completion_tokens
                for mo in task.model_outputs
                if mo.tier in ("C1", "C2")
            )
            if cloud_tokens > 0:
                await self.repo.add_tokens(task.user_id, cloud_tokens)

        self._log_task_end(task, handled_by, t0)
        return OrchestratorResult(task=task, response=task.final_response, handled_by=handled_by)

    # ---- rule handlers ----

    async def _handle_rule(self, match: RuleMatch, task: TaskState) -> str:
        if match.response is not None:
            return match.response
        if match.handler == "status":
            tid = match.args["task_id"]
            prior = await self.get_status(tid)
            if prior is None:
                return f"task `{tid}` not found"
            return (
                f"**Task `{tid}`**\n"
                f"status: {prior.status}\n"
                f"tier: {prior.current_tier}\n"
                f"route: {prior.route}\n"
                f"retries: {prior.retry_count}/{prior.retry_budget}\n"
                f"cloud_calls: {prior.cloud_call_count}\n"
                f"degraded: {prior.degraded}"
            )
        if match.handler == "retry":
            tid = match.args["task_id"]
            result = await self.replay(tid)
            if result is None:
                return f"cannot replay `{tid}` (not found or no repo configured)"
            return f"[replayed {tid} → {result.task.task_id}]\n\n{result.response}"
        if match.handler == "cancel":
            return f"cancel `{match.args['task_id']}`: not supported (per-turn subprocess only)"
        return "unknown rule"

    # ---- helpers ----

    def _messages(self, task: TaskState) -> list[dict[str, str]]:
        # FIX#2: when a retry is in flight, prepend the bump_prefix breadcrumb
        # so the model sees why the previous attempt was rejected. Length is
        # bounded by compress_for_bump (≤200-char preview + short reason),
        # and the prefix is cleared on a pass.
        user_content = task.user_message
        if task.bump_prefix:
            user_content = f"{task.bump_prefix}\n\n{task.user_message}"
        return (
            [{"role": "system", "content": _SYSTEM_PROMPT}]
            + task.history_window
            + [{"role": "user", "content": user_content}]
        )

    def _initial_tier(self, decision: RouterDecision) -> Tier:
        return {"local": "L2", "worker": "L3", "cloud": "C1"}[decision.route]  # type: ignore[return-value]

    def _degraded_response(self, task: TaskState, reason: str) -> str:
        return (
            "⚠️ Request could not be fully processed.\n"
            f"Reason: {reason}\n"
            f"Task: `{task.task_id}` (use `/retry {task.task_id}` to retry)"
        )

    def _log_task_end(self, task: TaskState, handled_by: str, t0: float) -> None:
        """Single summary line per request — one grep target for all runtime analysis.

        Fields intentionally mirror the 2nd-pass review criteria (latency p50/p95,
        validator flow, cloud escalation). Everything else stays in state.db.
        """
        log.info(
            "task.end",
            handled_by=handled_by,
            status=task.status,
            route=task.route,
            tier=task.current_tier,
            cloud_calls=task.cloud_call_count,
            cloud_models=list(task.cloud_model_used),
            retries=task.retry_count,
            tier_ups=task.tier_up_retries,
            same_tier_retries=task.same_tier_retries,
            degraded=task.degraded,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )

    async def _persist(self, task: TaskState) -> None:
        if self.repo is not None:
            try:
                await self.repo.save_task(task)
            except Exception as e:  # noqa: BLE001
                log.warning("repo.save_failed", err=str(e))

    # ---- lazy client builders ----

    def _openai_main_client(self) -> OpenAIClient:
        if self._openai_main is None:
            self._openai_main = OpenAIClient(
                self.settings.openai_api_key, self.settings.openai_model
            )
        return self._openai_main

    def _openai_surrogate_local_client(self) -> OpenAIClient:
        if self._openai_surrogate_local is None:
            self._openai_surrogate_local = OpenAIClient(
                self.settings.openai_api_key, self.settings.openai_model_local_surrogate
            )
        return self._openai_surrogate_local

    def _openai_surrogate_worker_client(self) -> OpenAIClient:
        if self._openai_surrogate_worker is None:
            self._openai_surrogate_worker = OpenAIClient(
                self.settings.openai_api_key, self.settings.openai_model_worker_surrogate
            )
        return self._openai_surrogate_worker

    def _anthropic_client(self) -> AnthropicClient:
        if self._anthropic is None:
            self._anthropic = AnthropicClient(
                self.settings.anthropic_api_key, self.settings.anthropic_model
            )
        return self._anthropic

    def _ollama_local_client(self) -> OllamaClient:
        if self._ollama_local is None:
            self._ollama_local = OllamaClient(
                self.settings.ollama_base_url, self.settings.ollama_work_model
            )
        return self._ollama_local

    def _ollama_worker_client(self) -> OllamaClient:
        if self._ollama_worker is None:
            self._ollama_worker = OllamaClient(
                self.settings.ollama_base_url, self.settings.ollama_worker_model
            )
        return self._ollama_worker
