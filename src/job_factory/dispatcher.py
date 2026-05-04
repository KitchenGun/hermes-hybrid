"""JobFactoryDispatcher — top-level orchestrator for v2 routing.

Pulled-together flow (Plan v2 §"라우팅 흐름 (재정의)"):

  1. Classify the message → JobType.
  2. Look up policy for that JobType.
  3. Loop up to ``max_attempts`` times among LOCAL adapters:
       a. Selector picks a model (epsilon-greedy bandit).
       b. Adapter generates.
       c. Validator scores 0–100 (async, multi-axis).
       d. ScoreMatrix update with the score (online learning).
       e. If score ≥ quality_threshold → run the action via Runner →
          return.
       f. Else continue loop (different model next iteration thanks to
          tie-break + exploration).
  4. If still failing after ``max_attempts``, **escalate via the same
     epsilon-greedy bandit** but with the candidate set restricted to
     cloud arms allowed by ``JobType`` and approved by ``CloudPolicy``.
     ``selection_reason`` becomes ``"escalation"``.
  5. Each cloud step also goes through ``CloudPolicy.evaluate`` first;
     denials skip the arm, ``needs_approval`` short-circuits the run
     so the orchestrator can pop a Discord button (Phase 7).
  6. Cloud step's score is pushed to the ScoreMatrix so cloud models
     become legitimate arms in the bandit too.

This dispatcher is **state-light** — it reads from registries, the
matrix, and the selector; it doesn't own any persistent state of its
own. The CloudPolicy *does* hold in-process counters; share one
instance across concurrent dispatchers in the same process.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from src.job_factory.classifier import JobClassification, JobClassifier
from src.job_factory.policy import CloudPolicy, PolicyVerdict
from src.job_factory.registry import (
    JobType,
    JobTypeRegistry,
    ModelEntry,
    ModelRegistry,
)
from src.job_factory.runner import ActionRunner, ToolResult
from src.job_factory.score_matrix import ScoreMatrix
from src.job_factory.selector import EpsilonGreedySelector, Selection
from src.llm.adapters.base import (
    AdapterRequest,
    AdapterResponse,
    ChatMessage,
    LLMAdapter,
)

log = logging.getLogger(__name__)

DispatchOutcome = Literal[
    "ok",                # response delivered (may include action result)
    "exhausted",         # tried everything, all sub-threshold
    "no_local_models",   # no local arms available + cloud disallowed
    "denied_cloud",      # cloud cap reached / job disallowed
    "needs_approval",    # cloud step gated on user approval
    "fatal",             # validator/runtime error we can't recover from
]


# ---- DTOs ------------------------------------------------------------------


@dataclass
class StepRecord:
    """One attempt within a dispatch — for the ledger / observability."""

    provider: str
    model: str
    matrix_key: str
    selection_reason: str
    score: float
    passed: bool
    response_text: str = ""
    tool_result: ToolResult | None = None
    error: str = ""


@dataclass
class ApprovalRequest:
    """Captured when CloudPolicy returns ``needs_approval``.

    The orchestrator (Phase 7) takes this and renders a Discord button
    via the existing HITL infrastructure. After user response, the
    orchestrator either re-dispatches with policy override or returns
    a degraded message.
    """

    matrix_key: str
    provider: str
    model: str
    reason: str
    estimated_cost_usd: float
    triggered_rule: str


@dataclass
class DispatchResult:
    """The dispatcher's verdict for one user turn."""

    outcome: DispatchOutcome
    job_type: str
    classification: JobClassification
    steps: list[StepRecord] = field(default_factory=list)
    final_text: str = ""
    final_tool_result: ToolResult | None = None
    approval_request: ApprovalRequest | None = None

    @property
    def best_step(self) -> StepRecord | None:
        if not self.steps:
            return None
        for s in reversed(self.steps):
            if s.passed:
                return s
        return self.steps[-1]


# ---- Validator surface ----------------------------------------------------


# Phase 6: validator is async and returns (score 0-100, baseline_ok).
# Use ``make_dispatcher_validator(CompositeValidator(...))`` from
# ``src.job_factory.validator`` to construct one for production.
ValidatorFn = Callable[
    [JobType, AdapterResponse],
    Awaitable[tuple[float, bool]],
]


async def _default_validator(
    job: JobType, response: AdapterResponse,
) -> tuple[float, bool]:
    """Trivial fallback: any non-empty response → 70/pass.

    This lets the dispatcher run before a real validator is wired up
    (e.g., in tests). For production, use the CompositeValidator.
    """
    text = (response.text or "").strip()
    if not text:
        return 0.0, False
    return 70.0, True


# ---- JobFactoryDispatcher -------------------------------------------------


class JobFactoryDispatcher:
    """Top-level v2 dispatcher.

    Args:
        classifier: Message → job_type.
        job_registry: Job_type policy lookup.
        model_registry: Provider/model metadata (cost, etc.).
        selector: EpsilonGreedySelector over the live ScoreMatrix.
        score_matrix: ScoreMatrix to update after each attempt.
        local_adapters: ``matrix_key → LLMAdapter`` for local models.
        cloud_adapters: ``matrix_key → LLMAdapter`` for cloud models.
            Empty dict means no escalation possible.
        runner: Translates LLM output into tool execution. Optional.
        validator: Async ``(job, response) → (score, baseline_ok)``.
            Defaults to ``_default_validator``. Phase 6 production
            wiring uses ``CompositeValidator``.
        cloud_policy: Gate for cloud calls. ``None`` ⇒ a permissive
            default policy (no caps, no approval gates) — convenient
            for tests but should never reach prod.
        system_prompt: Optional system message prepended to every turn.
    """

    def __init__(
        self,
        *,
        classifier: JobClassifier,
        job_registry: JobTypeRegistry,
        model_registry: ModelRegistry,
        selector: EpsilonGreedySelector,
        score_matrix: ScoreMatrix,
        local_adapters: dict[str, LLMAdapter],
        cloud_adapters: dict[str, LLMAdapter] | None = None,
        runner: ActionRunner | None = None,
        validator: ValidatorFn | None = None,
        cloud_policy: CloudPolicy | None = None,
        system_prompt: str | None = None,
    ):
        self._classifier = classifier
        self._jobs = job_registry
        self._models = model_registry
        self._selector = selector
        self._matrix = score_matrix
        self._local_adapters = dict(local_adapters)
        self._cloud_adapters = dict(cloud_adapters or {})
        self._runner = runner
        self._validate = validator or _default_validator
        self._policy = cloud_policy or CloudPolicy()
        self._system_prompt = system_prompt

    # ---- public API -------------------------------------------------------

    async def dispatch(self, message: str) -> DispatchResult:
        # 1. Classify.
        classification = await self._classifier.classify(message)
        job_name = classification.job_type
        if not self._jobs.has(job_name):
            log.warning(
                "dispatcher.classifier_returned_unknown",
                extra={"job_type": job_name},
            )
            job_name = self._jobs.classifier.fallback_job_type
            classification = JobClassification(
                job_type=job_name,
                confidence=0.0,
                method="fallback",
            )
        job = self._jobs.get(job_name)
        log.info(
            "dispatcher.classified",
            extra={
                "job_type": job_name,
                "method": classification.method,
                "confidence": classification.confidence,
            },
        )

        result = DispatchResult(
            outcome="exhausted",
            job_type=job_name,
            classification=classification,
        )

        # 2. Local attempts (epsilon-greedy bandit).
        local_keys = self._available_local_keys()
        if not local_keys and not self._can_escalate(job):
            result.outcome = "no_local_models"
            return result

        for _ in range(job.max_attempts):
            if not local_keys:
                break  # nothing local; fall through to escalation
            selection = self._selector.select(
                job_type=job_name,
                available_models=local_keys,
            )
            adapter = self._local_adapters[selection.model]
            step = await self._run_one_step(
                message=message,
                job=job,
                adapter=adapter,
                matrix_key=selection.model,
                selection_reason=selection.reason,
            )
            result.steps.append(step)
            if step.passed:
                await self._finalize(result, step, job)
                return result

        # 3. Escalation — cloud bandit + policy gate.
        cloud_step = await self._try_cloud_escalation(
            message=message,
            job=job,
            result=result,
        )
        if cloud_step is not None:
            result.steps.append(cloud_step)
            if cloud_step.passed:
                await self._finalize(result, cloud_step, job)
                return result

        # 4. Exhausted: best step is the response we surface.
        if result.outcome == "exhausted" and result.steps:
            best = result.best_step
            assert best is not None
            result.final_text = best.response_text
            result.final_tool_result = best.tool_result
        return result

    # ---- helpers ----------------------------------------------------------

    def _available_local_keys(self) -> list[str]:
        return [
            entry.matrix_key
            for entry in self._models.local
            if entry.matrix_key in self._local_adapters
        ]

    def _can_escalate(self, job: JobType) -> bool:
        return job.cloud_allowed or job.claude_allowed

    def _cloud_candidates_for(
        self, job: JobType,
    ) -> list[ModelEntry]:
        """Cloud entries that pass the JobType policy filter AND have
        an adapter registered. (Policy gate runs separately.)"""
        out: list[ModelEntry] = []
        for entry in self._models.cloud:
            if entry.matrix_key not in self._cloud_adapters:
                continue
            # Provider-specific gates: claude_cli uses claude_allowed
            # independently; any other (future) cloud provider uses
            # cloud_allowed. 2026-05-04: OpenAI removed; only claude_cli
            # is wired today, but the generic gate is retained for any
            # future non-claude cloud provider.
            if entry.provider == "claude_cli":
                if not job.claude_allowed:
                    continue
            elif not job.cloud_allowed:
                continue
            out.append(entry)
        return out

    async def _try_cloud_escalation(
        self,
        *,
        message: str,
        job: JobType,
        result: DispatchResult,
    ) -> StepRecord | None:
        """Run the cloud step. Returns the step if executed, ``None``
        if no cloud arm could fire (denied / no candidates / approval
        needed).

        Mutates ``result.outcome`` for terminal cases (denied_cloud,
        needs_approval) so the caller doesn't have to re-derive."""
        candidates = self._cloud_candidates_for(job)
        if not candidates:
            # Nothing to try — outcome stays "exhausted" if we got here
            # with steps, otherwise the caller will set it.
            return None

        # Apply CloudPolicy gate to each candidate. Track the first
        # ``needs_approval`` so we can surface it if all others deny.
        eligible_keys: list[str] = []
        approval_pending: tuple[ModelEntry, PolicyVerdict] | None = None
        denial_count = 0
        for entry in candidates:
            verdict = self._policy.evaluate(
                job=job, entry=entry, prompt_text=message,
            )
            if verdict.outcome == "allow":
                eligible_keys.append(entry.matrix_key)
            elif verdict.outcome == "needs_approval":
                if approval_pending is None:
                    approval_pending = (entry, verdict)
            else:
                denial_count += 1
                log.info(
                    "dispatcher.cloud_denied",
                    extra={
                        "matrix_key": entry.matrix_key,
                        "rule": verdict.triggered_rule,
                        "reason": verdict.reason,
                    },
                )

        if not eligible_keys:
            # All candidates either denied or pending approval.
            if approval_pending is not None:
                entry, v = approval_pending
                result.outcome = "needs_approval"
                result.approval_request = ApprovalRequest(
                    matrix_key=entry.matrix_key,
                    provider=entry.provider,
                    model=entry.name,
                    reason=v.reason,
                    estimated_cost_usd=v.estimated_cost_usd,
                    triggered_rule=v.triggered_rule,
                )
            elif denial_count > 0:
                result.outcome = "denied_cloud"
            return None

        # Bandit-pick from the eligible cloud arms.
        # Override the selection reason — for cloud step we always log
        # ``"escalation"`` so the ledger can split local-vs-cloud
        # behavior cleanly.
        selection = self._selector.select(
            job_type=job.name,
            available_models=eligible_keys,
        )
        adapter = self._cloud_adapters[selection.model]
        chosen_entry = self._models.find(selection.model)
        assert chosen_entry is not None  # guaranteed by candidates filter

        step = await self._run_one_step(
            message=message,
            job=job,
            adapter=adapter,
            matrix_key=selection.model,
            selection_reason="escalation",
        )

        # Record the call against the policy counters.
        self._policy.record_call(
            chosen_entry,
            actual_cost_usd=self._estimate_cost_from_step(chosen_entry, step),
        )
        return step

    @staticmethod
    def _estimate_cost_from_step(
        entry: ModelEntry, step: StepRecord,
    ) -> float | None:
        """Best-effort post-call cost estimate. ``None`` for free
        providers (returns 0 from CloudPolicy when we don't know)."""
        if entry.cost_input_per_1m == 0 and entry.cost_output_per_1m == 0:
            return None
        return None  # tokens not available on StepRecord; let policy use estimate

    async def _run_one_step(
        self,
        *,
        message: str,
        job: JobType,
        adapter: LLMAdapter,
        matrix_key: str,
        selection_reason: str,
    ) -> StepRecord:
        msgs = []
        if self._system_prompt:
            msgs.append(ChatMessage(role="system", content=self._system_prompt))
        msgs.append(ChatMessage(role="user", content=message))
        req = AdapterRequest(
            messages=msgs,
            timeout_s=float(job.timeout_seconds),
        )

        try:
            resp = await adapter.generate(req)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "dispatcher.adapter_failed",
                extra={
                    "job_type": job.name,
                    "matrix_key": matrix_key,
                    "err": str(e),
                },
            )
            await self._matrix.update(job.name, matrix_key, 0.0)
            return StepRecord(
                provider=adapter.provider,
                model=adapter.model,
                matrix_key=matrix_key,
                selection_reason=selection_reason,
                score=0.0,
                passed=False,
                error=f"{type(e).__name__}: {e}",
            )

        try:
            score, ok_baseline = await self._validate(job, resp)
        except Exception as e:  # noqa: BLE001
            log.warning("dispatcher.validator_failed", extra={"err": str(e)})
            score, ok_baseline = 0.0, False

        passed = ok_baseline and score >= job.quality_threshold
        await self._matrix.update(job.name, matrix_key, score)

        log.info(
            "dispatcher.step",
            extra={
                "job_type": job.name,
                "matrix_key": matrix_key,
                "selection_reason": selection_reason,
                "score": score,
                "passed": passed,
            },
        )

        return StepRecord(
            provider=adapter.provider,
            model=adapter.model,
            matrix_key=matrix_key,
            selection_reason=selection_reason,
            score=score,
            passed=passed,
            response_text=resp.text or "",
        )

    async def _finalize(
        self,
        result: DispatchResult,
        step: StepRecord,
        job: JobType,
    ) -> None:
        result.outcome = "ok"
        result.final_text = step.response_text

        if self._runner is None:
            return

        tool_result = await self._runner.execute(
            step.response_text,
            job_required_tools=(),
            timeout_s=float(job.timeout_seconds),
        )
        step.tool_result = tool_result
        result.final_tool_result = tool_result

        if tool_result.status == "respond_only" and tool_result.output:
            result.final_text = str(tool_result.output)
