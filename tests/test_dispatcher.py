"""Integration tests for src/job_factory/dispatcher.py.

Stubs every external dependency:
  * Classifier returns a fixed label.
  * Adapters are canned (configurable text + score).
  * Validator is configurable.
  * ScoreMatrix is real (so we can assert it gets updated).
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from src.job_factory.classifier import JobClassification, JobClassifier
from src.job_factory.dispatcher import (
    DispatchResult,
    JobFactoryDispatcher,
    StepRecord,
)
from src.job_factory.registry import (
    ClassifierConfig,
    DiscoveryConfig,
    JobType,
    JobTypeRegistry,
    ModelEntry,
    ModelRegistry,
)
from src.job_factory.runner import ActionRunner, ToolRegistry
from src.job_factory.score_matrix import ScoreMatrix
from src.job_factory.selector import EpsilonGreedySelector
from src.llm.adapters.base import (
    AdapterRequest,
    AdapterResponse,
    ChatMessage,
    LLMAdapter,
)


# ---- Stubs ----------------------------------------------------------------


@dataclass
class _StubClassifier:
    """JobClassifier-shaped stub returning a fixed classification."""

    job_type: str
    method: str = "keyword"
    confidence: float = 0.9

    async def classify(self, message: str) -> JobClassification:
        return JobClassification(
            job_type=self.job_type,
            confidence=self.confidence,
            method=self.method,
        )


@dataclass
class _StubAdapter:
    """LLMAdapter-shaped stub. Returns canned text per call.

    If ``raise_exc`` is set, raises that on .generate().
    """

    provider_name: str = "ollama"
    model_name: str = "stub-model"
    response_text: str = "ok"
    raise_exc: Exception | None = None
    calls: int = 0

    @property
    def provider(self) -> str:
        return self.provider_name

    @property
    def model(self) -> str:
        return self.model_name

    async def generate(self, request: AdapterRequest) -> AdapterResponse:
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        return AdapterResponse(
            text=self.response_text,
            provider=self.provider_name,
            model=self.model_name,
            duration_ms=10,
            completion_tokens=5,
        )


def _job_type(
    name: str,
    *,
    quality_threshold: int = 60,
    max_attempts: int = 2,
    cloud_allowed: bool = False,
    claude_allowed: bool = False,
) -> JobType:
    return JobType(
        name=name,
        quality_threshold=quality_threshold,
        max_attempts=max_attempts,
        cloud_allowed=cloud_allowed,
        claude_allowed=claude_allowed,
        timeout_seconds=30,
    )


def _registry(jts: list[JobType]) -> JobTypeRegistry:
    return JobTypeRegistry(
        job_types={j.name: j for j in jts},
        classifier=ClassifierConfig(fallback_job_type=jts[0].name),
    )


def _model_registry(
    *,
    local: list[tuple[str, str]],
    cloud: list[tuple[str, str]] | None = None,
) -> ModelRegistry:
    return ModelRegistry(
        local=tuple(ModelEntry(provider=p, name=n) for p, n in local),
        cloud=tuple(
            ModelEntry(provider=p, name=n)
            for p, n in (cloud or [])
        ),
        discovery=DiscoveryConfig(),
    )


def _matrix(tmp_path: Path) -> ScoreMatrix:
    return ScoreMatrix(
        path=tmp_path / "matrix.json", flush_threshold=10_000,
    )


def _make_validator(score: float, ok: bool = True):
    """Phase 6: validator is async. Tests can await without ceremony."""
    async def v(job: JobType, resp: AdapterResponse) -> tuple[float, bool]:
        return score, ok
    return v


def _permissive_policy():
    """A CloudPolicy with all caps disabled — for tests that want to
    reach the cloud step unconditionally."""
    from src.job_factory.policy import CloudPolicy, CloudPolicyConfig
    cfg = CloudPolicyConfig(
        claude_auto_calls_per_hour=0,
        claude_auto_calls_per_day=0,
        daily_usd_cap=0,
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0,
    )
    return CloudPolicy(config=cfg)


# ---- Local-only happy path ------------------------------------------------


@pytest.mark.asyncio
async def test_first_local_passes(tmp_path):
    """First attempt scores above threshold → return immediately."""
    matrix = _matrix(tmp_path)
    job = _job_type("simple_chat", quality_threshold=60, max_attempts=2)
    jobs = _registry([job])
    models = _model_registry(local=[("ollama", "m1")])

    adapter = _StubAdapter(model_name="m1", response_text="hello back")
    selector = EpsilonGreedySelector(
        matrix, epsilon=0.0, warmup_n=0, rng=random.Random(0),
    )

    dispatcher = JobFactoryDispatcher(
        classifier=_StubClassifier("simple_chat"),
        job_registry=jobs,
        model_registry=models,
        selector=selector,
        score_matrix=matrix,
        local_adapters={"ollama/m1": adapter},
        validator=_make_validator(80.0),
    )
    result = await dispatcher.dispatch("hi")
    assert result.outcome == "ok"
    assert result.job_type == "simple_chat"
    assert len(result.steps) == 1
    assert result.steps[0].passed
    assert result.final_text == "hello back"
    # ScoreMatrix updated.
    assert matrix.get("simple_chat", "ollama/m1").mean == pytest.approx(80.0)


@pytest.mark.asyncio
async def test_first_below_threshold_retries_with_different_model(tmp_path):
    """First attempt scores below threshold → bandit picks again."""
    matrix = _matrix(tmp_path)
    job = _job_type("simple_chat", quality_threshold=60, max_attempts=2)
    jobs = _registry([job])
    models = _model_registry(local=[
        ("ollama", "weak"),
        ("ollama", "strong"),
    ])

    weak = _StubAdapter(model_name="weak", response_text="meh")
    strong = _StubAdapter(model_name="strong", response_text="great")

    # Custom validator: weak gets 30, strong gets 90.
    async def validator(job, resp):
        if resp.model == "weak":
            return 30.0, True
        return 90.0, True

    selector = EpsilonGreedySelector(
        matrix, epsilon=0.0, warmup_n=1, rng=random.Random(0),
    )
    dispatcher = JobFactoryDispatcher(
        classifier=_StubClassifier("simple_chat"),
        job_registry=jobs,
        model_registry=models,
        selector=selector,
        score_matrix=matrix,
        local_adapters={
            "ollama/weak": weak,
            "ollama/strong": strong,
        },
        validator=validator,
    )
    result = await dispatcher.dispatch("hi")
    # Should converge on a passing step within max_attempts=2.
    assert result.outcome == "ok"
    assert any(s.passed for s in result.steps)
    # Final text comes from a passing step (likely strong).
    assert result.final_text == "great"


@pytest.mark.asyncio
async def test_exhausted_returns_best_step(tmp_path):
    """All attempts below threshold + cloud disallowed → exhausted,
    final_text from best (least-bad) step."""
    matrix = _matrix(tmp_path)
    job = _job_type(
        "simple_chat",
        quality_threshold=80,
        max_attempts=2,
        cloud_allowed=False,
        claude_allowed=False,
    )
    jobs = _registry([job])
    models = _model_registry(local=[("ollama", "m1")])
    adapter = _StubAdapter(model_name="m1", response_text="meh")

    selector = EpsilonGreedySelector(
        matrix, epsilon=0.0, warmup_n=0, rng=random.Random(0),
    )
    dispatcher = JobFactoryDispatcher(
        classifier=_StubClassifier("simple_chat"),
        job_registry=jobs,
        model_registry=models,
        selector=selector,
        score_matrix=matrix,
        local_adapters={"ollama/m1": adapter},
        validator=_make_validator(40.0),  # below 80 threshold
    )
    result = await dispatcher.dispatch("hi")
    assert result.outcome == "exhausted"
    assert len(result.steps) == 2  # tried max_attempts times
    assert all(not s.passed for s in result.steps)
    assert result.final_text == "meh"


@pytest.mark.asyncio
async def test_adapter_exception_records_zero_and_continues(tmp_path):
    """An adapter raising → score 0 for that arm + try next."""
    matrix = _matrix(tmp_path)
    job = _job_type("simple_chat", quality_threshold=60, max_attempts=2)
    jobs = _registry([job])
    models = _model_registry(local=[
        ("ollama", "broken"),
        ("ollama", "working"),
    ])

    broken = _StubAdapter(
        model_name="broken",
        raise_exc=RuntimeError("simulated"),
    )
    working = _StubAdapter(model_name="working", response_text="ok")

    # Need bandit to pick broken first → seed warmup so both are cold.
    selector = EpsilonGreedySelector(
        matrix, epsilon=0.0, warmup_n=1, rng=random.Random(42),
    )
    dispatcher = JobFactoryDispatcher(
        classifier=_StubClassifier("simple_chat"),
        job_registry=jobs,
        model_registry=models,
        selector=selector,
        score_matrix=matrix,
        local_adapters={
            "ollama/broken": broken,
            "ollama/working": working,
        },
        validator=_make_validator(80.0),
    )
    result = await dispatcher.dispatch("hi")
    # Either: broken got picked, recorded 0, then working passed
    # OR: working got picked first and passed (no broken attempt).
    # Both are acceptable — the key check is that the run survived.
    assert result.outcome == "ok"
    # ScoreMatrix shows 0 for broken if it was tried.
    if matrix.has("simple_chat", "ollama/broken"):
        assert matrix.get("simple_chat", "ollama/broken").mean == 0.0


# ---- No-models edge cases -------------------------------------------------


@pytest.mark.asyncio
async def test_no_local_adapters_and_cloud_disallowed_returns_no_local(
    tmp_path,
):
    matrix = _matrix(tmp_path)
    job = _job_type(
        "simple_chat",
        cloud_allowed=False,
        claude_allowed=False,
    )
    jobs = _registry([job])
    models = _model_registry(local=[])

    selector = EpsilonGreedySelector(
        matrix, rng=random.Random(0),
    )
    dispatcher = JobFactoryDispatcher(
        classifier=_StubClassifier("simple_chat"),
        job_registry=jobs,
        model_registry=models,
        selector=selector,
        score_matrix=matrix,
        local_adapters={},
        validator=_make_validator(80.0),
    )
    result = await dispatcher.dispatch("hi")
    assert result.outcome == "no_local_models"
    assert result.steps == []


# ---- Cloud escalation -----------------------------------------------------


@pytest.mark.asyncio
async def test_cloud_escalation_on_local_exhaustion(tmp_path):
    """Local exhausted → escalate to cloud + cloud passes."""
    matrix = _matrix(tmp_path)
    job = _job_type(
        "summarize",
        quality_threshold=80,
        max_attempts=1,
        cloud_allowed=True, claude_allowed=True,
    )
    jobs = _registry([job])
    models = _model_registry(
        local=[("ollama", "weak")],
        cloud=[("claude_cli", "haiku")],
    )

    local = _StubAdapter(model_name="weak", response_text="meh")
    cloud = _StubAdapter(
        provider_name="claude_cli",
        model_name="haiku",
        response_text="great cloud answer",
    )

    async def validator(job, resp):
        if resp.provider == "claude_cli":
            return 95.0, True
        return 30.0, True

    selector = EpsilonGreedySelector(
        matrix, epsilon=0.0, warmup_n=0, rng=random.Random(0),
    )
    dispatcher = JobFactoryDispatcher(
        classifier=_StubClassifier("summarize"),
        job_registry=jobs,
        model_registry=models,
        selector=selector,
        score_matrix=matrix,
        local_adapters={"ollama/weak": local},
        cloud_adapters={"claude_cli/haiku": cloud},
        validator=validator,
        cloud_policy=_permissive_policy(),
    )
    result = await dispatcher.dispatch("summarize this")
    assert result.outcome == "ok"
    # 1 local attempt + 1 cloud attempt.
    assert len(result.steps) == 2
    # Last step is cloud and passed.
    assert result.steps[-1].provider == "claude_cli"
    assert result.steps[-1].passed
    assert result.final_text == "great cloud answer"
    # Selection reason for cloud step is the unified escalation marker
    # (Phase 6: dropped per-provider suffix — single escalation arm bandit).
    assert result.steps[-1].selection_reason == "escalation"
    # ScoreMatrix updated for cloud arm too.
    assert matrix.get("summarize", "claude_cli/haiku").n == 1


@pytest.mark.asyncio
async def test_claude_escalation_when_cloud_disabled(tmp_path):
    """cloud_allowed=False but claude_allowed=True → non-claude cloud
    blocked, claude_cli still passes (provider-specific gate logic)."""
    matrix = _matrix(tmp_path)
    job = _job_type(
        "code_generation",
        quality_threshold=80,
        max_attempts=1,
        cloud_allowed=False,
        claude_allowed=True,
    )
    jobs = _registry([job])
    models = _model_registry(
        local=[("ollama", "qwen-coder")],
        cloud=[
            ("future_cloud", "x"),       # blocked by cloud_allowed=False
            ("claude_cli", "sonnet"),    # allowed by claude_allowed=True
        ],
    )

    local = _StubAdapter(model_name="qwen-coder", response_text="weak code")
    future = _StubAdapter(
        provider_name="future_cloud",
        model_name="x",
        response_text="should not be called",
    )
    claude = _StubAdapter(
        provider_name="claude_cli",
        model_name="sonnet",
        response_text="great code",
    )

    async def validator(job, resp):
        if resp.provider == "claude_cli":
            return 90.0, True
        return 30.0, True

    selector = EpsilonGreedySelector(
        matrix, epsilon=0.0, warmup_n=0, rng=random.Random(0),
    )
    dispatcher = JobFactoryDispatcher(
        classifier=_StubClassifier("code_generation"),
        job_registry=jobs,
        model_registry=models,
        selector=selector,
        score_matrix=matrix,
        local_adapters={"ollama/qwen-coder": local},
        cloud_adapters={
            "future_cloud/x": future,
            "claude_cli/sonnet": claude,
        },
        validator=validator,
        cloud_policy=_permissive_policy(),
    )
    result = await dispatcher.dispatch("write code")
    # Local once + Claude (future_cloud blocked by cloud_allowed=False).
    assert result.outcome == "ok"
    providers = [s.provider for s in result.steps]
    assert "claude_cli" in providers
    assert "future_cloud" not in providers
    assert future.calls == 0


# ---- Runner integration ---------------------------------------------------


@pytest.mark.asyncio
async def test_runner_executes_action_when_passing(tmp_path):
    """Passing step + Runner present → action JSON is parsed and tool
    invocation appears in the result."""
    matrix = _matrix(tmp_path)
    job = _job_type("simple_chat", quality_threshold=60, max_attempts=1)
    jobs = _registry([job])
    models = _model_registry(local=[("ollama", "m1")])

    # LLM produces an action JSON with respond_to_user (always allowed).
    adapter = _StubAdapter(
        model_name="m1",
        response_text=(
            '{"action": {"tool": "respond_to_user", '
            '"args": {"text": "hi via tool"}}}'
        ),
    )

    tool_reg = ToolRegistry()
    runner = ActionRunner(tool_reg)

    selector = EpsilonGreedySelector(
        matrix, epsilon=0.0, warmup_n=0, rng=random.Random(0),
    )
    dispatcher = JobFactoryDispatcher(
        classifier=_StubClassifier("simple_chat"),
        job_registry=jobs,
        model_registry=models,
        selector=selector,
        score_matrix=matrix,
        local_adapters={"ollama/m1": adapter},
        runner=runner,
        validator=_make_validator(70.0),
    )
    result = await dispatcher.dispatch("hi")
    assert result.outcome == "ok"
    assert result.final_tool_result is not None
    assert result.final_tool_result.status == "respond_only"
    # final_text is the cleaned response (from tool args).
    assert result.final_text == "hi via tool"


# ---- Classifier fallback safety ------------------------------------------


@pytest.mark.asyncio
async def test_classifier_returning_unknown_job_falls_back(tmp_path):
    """Defensive: if the classifier somehow returns an unknown job_type,
    dispatcher uses fallback_job_type."""
    matrix = _matrix(tmp_path)
    job = _job_type("simple_chat", quality_threshold=60, max_attempts=1)
    jobs = _registry([job])
    models = _model_registry(local=[("ollama", "m1")])
    adapter = _StubAdapter(model_name="m1", response_text="ok")

    # Stub returns a job_type that doesn't exist in the registry.
    classifier = _StubClassifier("does_not_exist")

    selector = EpsilonGreedySelector(
        matrix, epsilon=0.0, warmup_n=0, rng=random.Random(0),
    )
    dispatcher = JobFactoryDispatcher(
        classifier=classifier,
        job_registry=jobs,
        model_registry=models,
        selector=selector,
        score_matrix=matrix,
        local_adapters={"ollama/m1": adapter},
        validator=_make_validator(70.0),
    )
    result = await dispatcher.dispatch("hi")
    # Fell back to simple_chat (the only registered job).
    assert result.job_type == "simple_chat"
    assert result.outcome == "ok"


# ---- Phase 6: CloudPolicy gate + cloud bandit ----------------------------


@pytest.mark.asyncio
async def test_cloud_step_denied_by_policy_returns_denied_cloud(tmp_path):
    """All cloud arms denied by CloudPolicy → result.outcome == 'denied_cloud'."""
    from src.job_factory.policy import CloudPolicy, CloudPolicyConfig
    from src.job_factory.registry import ModelEntry

    matrix = _matrix(tmp_path)
    job = _job_type(
        "summarize",
        quality_threshold=80, max_attempts=1, cloud_allowed=True, claude_allowed=True,
    )
    jobs = _registry([job])
    models = _model_registry(
        local=[("ollama", "weak")],
        cloud=[("claude_cli", "haiku")],
    )
    local = _StubAdapter(model_name="weak", response_text="meh")
    cloud = _StubAdapter(
        provider_name="claude_cli", model_name="haiku",
        response_text="should not be called",
    )

    # Policy: 1 Claude CLI call/hour, then deny.
    policy = CloudPolicy(config=CloudPolicyConfig(
        claude_auto_calls_per_hour=1,
        claude_auto_calls_per_day=0,
        daily_usd_cap=0,
        approval_estimated_tokens_above=0,
        approval_estimated_cost_above_usd=0,
    ))
    # Burn the only allowed call so the next evaluate() denies.
    policy.record_call(ModelEntry(provider="claude_cli", name="haiku"))

    selector = EpsilonGreedySelector(
        matrix, epsilon=0.0, warmup_n=0, rng=random.Random(0),
    )
    dispatcher = JobFactoryDispatcher(
        classifier=_StubClassifier("summarize"),
        job_registry=jobs,
        model_registry=models,
        selector=selector,
        score_matrix=matrix,
        local_adapters={"ollama/weak": local},
        cloud_adapters={"claude_cli/haiku": cloud},
        validator=_make_validator(30.0),  # local fails
        cloud_policy=policy,
    )
    result = await dispatcher.dispatch("summarize")
    assert result.outcome == "denied_cloud"
    # Cloud adapter must NOT have been called.
    assert cloud.calls == 0


@pytest.mark.asyncio
async def test_cloud_step_needs_approval_short_circuits(tmp_path):
    """Job with requires_user_approval=True → result.outcome ==
    'needs_approval' + ApprovalRequest populated."""
    matrix = _matrix(tmp_path)
    # JobType with requires_user_approval=True (helper doesn't expose it).
    job = JobType(
        name="heavy_project_task",
        max_attempts=1,
        quality_threshold=80,
        cloud_allowed=False,
        claude_allowed=True,
        requires_user_approval=True,
        timeout_seconds=30,
    )
    jobs = JobTypeRegistry(
        job_types={job.name: job},
        classifier=ClassifierConfig(fallback_job_type=job.name),
    )
    models = _model_registry(
        local=[("ollama", "qwen-coder")],
        cloud=[("claude_cli", "sonnet")],
    )
    local = _StubAdapter(model_name="qwen-coder", response_text="local fails")
    claude = _StubAdapter(
        provider_name="claude_cli", model_name="sonnet",
        response_text="should not be called",
    )

    selector = EpsilonGreedySelector(
        matrix, epsilon=0.0, warmup_n=0, rng=random.Random(0),
    )
    dispatcher = JobFactoryDispatcher(
        classifier=_StubClassifier("heavy_project_task"),
        job_registry=jobs,
        model_registry=models,
        selector=selector,
        score_matrix=matrix,
        local_adapters={"ollama/qwen-coder": local},
        cloud_adapters={"claude_cli/sonnet": claude},
        validator=_make_validator(30.0),
        cloud_policy=_permissive_policy(),
    )
    result = await dispatcher.dispatch("refactor everything")
    assert result.outcome == "needs_approval"
    assert result.approval_request is not None
    assert result.approval_request.provider == "claude_cli"
    assert result.approval_request.triggered_rule == "job_requires_approval"
    assert claude.calls == 0


@pytest.mark.asyncio
async def test_cloud_bandit_picks_among_eligible_arms(tmp_path):
    """When two cloud arms are both eligible, the selector picks the one
    with highest mean (exploitation)."""
    matrix = _matrix(tmp_path)
    # Pre-seed matrix: haiku → 50, sonnet → 90 → bandit picks sonnet.
    for _ in range(5):
        await matrix.update("summarize", "claude_cli/haiku", 50.0)
        await matrix.update("summarize", "claude_cli/sonnet", 90.0)

    job = _job_type(
        "summarize", quality_threshold=70, max_attempts=1,
        cloud_allowed=True, claude_allowed=True,
    )
    jobs = _registry([job])
    models = _model_registry(
        local=[("ollama", "weak")],
        cloud=[("claude_cli", "haiku"), ("claude_cli", "sonnet")],
    )
    local = _StubAdapter(model_name="weak", response_text="local fails")
    mini = _StubAdapter(
        provider_name="claude_cli", model_name="haiku",
        response_text="mini answer",
    )
    big = _StubAdapter(
        provider_name="claude_cli", model_name="sonnet",
        response_text="big answer",
    )

    async def validator(job, resp):
        if resp.model == "sonnet":
            return 95.0, True
        return 30.0, True

    selector = EpsilonGreedySelector(
        matrix, epsilon=0.0, warmup_n=1, rng=random.Random(0),
    )
    dispatcher = JobFactoryDispatcher(
        classifier=_StubClassifier("summarize"),
        job_registry=jobs,
        model_registry=models,
        selector=selector,
        score_matrix=matrix,
        local_adapters={"ollama/weak": local},
        cloud_adapters={
            "claude_cli/haiku": mini,
            "claude_cli/sonnet": big,
        },
        validator=validator,
        cloud_policy=_permissive_policy(),
    )
    result = await dispatcher.dispatch("summarize")
    assert result.outcome == "ok"
    assert big.calls == 1
    assert mini.calls == 0
    assert matrix.get("summarize", "claude_cli/sonnet").n == 6  # 5 seed + 1 new


@pytest.mark.asyncio
async def test_cloud_policy_record_call_increments_counter(tmp_path):
    """A successful cloud step bumps the policy's per-provider counter."""
    matrix = _matrix(tmp_path)
    job = _job_type(
        "summarize", quality_threshold=70, max_attempts=1,
        cloud_allowed=True, claude_allowed=True,
    )
    jobs = _registry([job])
    models = _model_registry(
        local=[("ollama", "weak")],
        cloud=[("claude_cli", "haiku")],
    )
    local = _StubAdapter(model_name="weak", response_text="meh")
    cloud = _StubAdapter(
        provider_name="claude_cli", model_name="haiku",
        response_text="cloud reply",
    )

    policy = _permissive_policy()
    assert policy.stats()["claude_hour"] == 0

    # Local fails so cloud step actually fires.
    async def validator(job, resp):
        if resp.provider == "claude_cli":
            return 95.0, True
        return 30.0, True

    selector = EpsilonGreedySelector(
        matrix, epsilon=0.0, warmup_n=0, rng=random.Random(0),
    )
    dispatcher = JobFactoryDispatcher(
        classifier=_StubClassifier("summarize"),
        job_registry=jobs,
        model_registry=models,
        selector=selector,
        score_matrix=matrix,
        local_adapters={"ollama/weak": local},
        cloud_adapters={"claude_cli/haiku": cloud},
        validator=validator,
        cloud_policy=policy,
    )
    await dispatcher.dispatch("summarize")
    assert policy.stats()["claude_hour"] == 1


@pytest.mark.asyncio
async def test_async_validator_exception_records_zero(tmp_path):
    """Validator that raises → score 0, step not passed (graceful)."""
    matrix = _matrix(tmp_path)
    job = _job_type("simple_chat", quality_threshold=60, max_attempts=1)
    jobs = _registry([job])
    models = _model_registry(local=[("ollama", "m1")])
    adapter = _StubAdapter(model_name="m1", response_text="ok")

    async def crashing_validator(job, resp):
        raise RuntimeError("validator bug")

    selector = EpsilonGreedySelector(
        matrix, epsilon=0.0, warmup_n=0, rng=random.Random(0),
    )
    dispatcher = JobFactoryDispatcher(
        classifier=_StubClassifier("simple_chat"),
        job_registry=jobs,
        model_registry=models,
        selector=selector,
        score_matrix=matrix,
        local_adapters={"ollama/m1": adapter},
        validator=crashing_validator,
    )
    result = await dispatcher.dispatch("hi")
    assert result.outcome == "exhausted"
    assert result.steps[0].score == 0
    assert not result.steps[0].passed

