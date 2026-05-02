"""Tests for src/job_factory/validator.py."""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from src.job_factory.registry import JobType
from src.job_factory.validator import (
    CompositeValidator,
    LLMJudgeValidator,
    LengthValidator,
    StructuralValidator,
    ValidationResult,
    default_rubric,
    make_dispatcher_validator,
)
from src.llm.adapters.base import (
    AdapterRequest,
    AdapterResponse,
    ChatMessage,
)


def _resp(text: str, **kw) -> AdapterResponse:
    defaults = dict(
        provider="test", model="test-model",
        duration_ms=10, completion_tokens=5,
    )
    defaults.update(kw)
    return AdapterResponse(text=text, **defaults)


def _job(name: str = "simple_chat", **kw) -> JobType:
    return JobType(name=name, **kw)


# ---- LengthValidator ------------------------------------------------------


@pytest.mark.asyncio
async def test_length_empty_baseline_fail():
    v = LengthValidator()
    score, ok, reason = await v.evaluate(job=_job(), response=_resp(""))
    assert score == 0
    assert ok is False
    assert "empty" in reason


@pytest.mark.asyncio
async def test_length_short_scaled():
    v = LengthValidator(min_chars=10)
    # 5 chars / min 10 → 50%.
    score, ok, _ = await v.evaluate(job=_job(), response=_resp("hello"))
    assert ok is True  # short is penalized, not blocked
    assert score == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_length_sweet_spot_full_marks():
    v = LengthValidator(min_chars=5, soft_max=4000)
    score, ok, _ = await v.evaluate(
        job=_job(), response=_resp("a" * 1000),
    )
    assert score == 100
    assert ok is True


@pytest.mark.asyncio
async def test_length_verbose_scaled_to_50():
    v = LengthValidator(min_chars=5, soft_max=100, hard_max=200)
    # Halfway: (150 - 100) / (200 - 100) = 0.5 → 100 - 50*0.5 = 75
    score, _, _ = await v.evaluate(
        job=_job(), response=_resp("a" * 150),
    )
    assert score == pytest.approx(75.0)


@pytest.mark.asyncio
async def test_length_too_long_floor_30():
    v = LengthValidator(min_chars=5, soft_max=100, hard_max=200)
    score, ok, reason = await v.evaluate(
        job=_job(), response=_resp("a" * 5000),
    )
    assert score == 30
    assert ok is True  # very long ≠ baseline fail
    assert "too long" in reason


def test_length_invalid_thresholds_raise():
    with pytest.raises(ValueError):
        LengthValidator(min_chars=100, soft_max=50, hard_max=200)
    with pytest.raises(ValueError):
        LengthValidator(min_chars=10, soft_max=200, hard_max=100)


# ---- StructuralValidator --------------------------------------------------


@pytest.mark.asyncio
async def test_structural_no_required_full_marks_for_valid_json():
    v = StructuralValidator()
    score, ok, _ = await v.evaluate(
        job=_job(), response=_resp('{"a": 1}'),
    )
    assert score == 100
    assert ok is True


@pytest.mark.asyncio
async def test_structural_invalid_json_baseline_fail():
    v = StructuralValidator()
    score, ok, reason = await v.evaluate(
        job=_job(), response=_resp("not json"),
    )
    assert score == 0
    assert ok is False
    assert "JSONDecodeError" in reason


@pytest.mark.asyncio
async def test_structural_strips_code_fences():
    v = StructuralValidator()
    score, ok, _ = await v.evaluate(
        job=_job(),
        response=_resp('```json\n{"x": 1}\n```'),
    )
    assert score == 100
    assert ok is True


@pytest.mark.asyncio
async def test_structural_partial_required_fields_baseline_fail():
    v = StructuralValidator(
        required_fields=("Date", "Activity", "Category", "Mood"),
    )
    score, ok, reason = await v.evaluate(
        job=_job(),
        # 2 of 4 fields present.
        response=_resp(json.dumps({"Date": "2026-04-30", "Activity": "x"})),
    )
    assert score == pytest.approx(50.0)
    assert ok is False                       # missing fields = baseline fail
    assert "missing required fields" in reason


@pytest.mark.asyncio
async def test_structural_array_uses_first_element():
    v = StructuralValidator(required_fields=("Date",))
    score, ok, _ = await v.evaluate(
        job=_job(),
        response=_resp(json.dumps([
            {"Date": "2026-04-30"},
            {"Date": "2026-05-01"},
        ])),
    )
    assert score == 100
    assert ok is True


@pytest.mark.asyncio
async def test_structural_non_dict_array_element():
    v = StructuralValidator(required_fields=("x",))
    score, ok, reason = await v.evaluate(
        job=_job(), response=_resp(json.dumps(["just", "strings"])),
    )
    assert score == 0
    assert ok is False
    assert "expected dict" in reason


# ---- LLMJudgeValidator ----------------------------------------------------


@dataclass
class _StubJudgeAdapter:
    response_text: str = "SCORE: 80\nREASON: looks good"
    raise_exc: Exception | None = None
    last_request: AdapterRequest | None = None

    @property
    def provider(self) -> str:
        return "stub_judge"

    @property
    def model(self) -> str:
        return "stub-model"

    async def generate(self, request: AdapterRequest) -> AdapterResponse:
        self.last_request = request
        if self.raise_exc:
            raise self.raise_exc
        return AdapterResponse(
            text=self.response_text,
            provider="stub_judge", model="stub-model",
            duration_ms=10, completion_tokens=5,
        )


@pytest.mark.asyncio
async def test_llm_judge_parses_score_line():
    judge = _StubJudgeAdapter("SCORE: 85\nREASON: nice")
    v = LLMJudgeValidator(judge, default_rubric)
    score, ok, _ = await v.evaluate(
        job=_job(), response=_resp("good answer"),
    )
    assert score == 85
    assert ok is True


@pytest.mark.asyncio
async def test_llm_judge_low_score_fails_baseline():
    judge = _StubJudgeAdapter("SCORE: 25\nREASON: bad")
    v = LLMJudgeValidator(judge, default_rubric)
    score, ok, _ = await v.evaluate(
        job=_job(), response=_resp("text"),
    )
    assert score == 25
    assert ok is False  # judge < 40 = baseline fail


@pytest.mark.asyncio
async def test_llm_judge_clamps_above_100():
    judge = _StubJudgeAdapter("SCORE: 150\nREASON: amazing")
    v = LLMJudgeValidator(judge, default_rubric)
    score, _, _ = await v.evaluate(
        job=_job(), response=_resp("text"),
    )
    assert score == 100


@pytest.mark.asyncio
async def test_llm_judge_unparseable_falls_back_to_50():
    judge = _StubJudgeAdapter("nothing structured here")
    v = LLMJudgeValidator(judge, default_rubric)
    score, ok, reason = await v.evaluate(
        job=_job(), response=_resp("text"),
    )
    assert score == 50
    assert ok is True   # neutral pass — don't penalize for judge issue
    assert "unparseable" in reason


@pytest.mark.asyncio
async def test_llm_judge_exception_falls_back_to_50():
    judge = _StubJudgeAdapter(raise_exc=RuntimeError("API down"))
    v = LLMJudgeValidator(judge, default_rubric)
    score, ok, reason = await v.evaluate(
        job=_job(), response=_resp("text"),
    )
    assert score == 50
    assert ok is True
    assert "judge unavailable" in reason


@pytest.mark.asyncio
async def test_llm_judge_empty_response():
    judge = _StubJudgeAdapter("SCORE: 90\nREASON: ok")
    v = LLMJudgeValidator(judge, default_rubric)
    score, ok, reason = await v.evaluate(
        job=_job(), response=_resp(""),
    )
    assert score == 0
    assert ok is False
    assert "empty" in reason


@pytest.mark.asyncio
async def test_llm_judge_passes_rubric_to_judge():
    """Verify the judge prompt actually includes the per-job rubric."""
    judge = _StubJudgeAdapter("SCORE: 70\nREASON: ok")

    def custom_rubric(j: JobType) -> str:
        return f"CUSTOM RUBRIC FOR {j.name.upper()}"

    v = LLMJudgeValidator(judge, custom_rubric)
    await v.evaluate(
        job=_job("schedule_logging"),
        response=_resp("activity log entry"),
    )
    sent = judge.last_request
    assert sent is not None
    payload = sent.messages[0].content
    assert "CUSTOM RUBRIC FOR SCHEDULE_LOGGING" in payload
    assert "activity log entry" in payload


# ---- CompositeValidator ---------------------------------------------------


@pytest.mark.asyncio
async def test_composite_combines_axes_with_weights():
    """length=100 (weight 0.3), structural=50 (weight 0.7) → 65."""
    composite = CompositeValidator(
        axes=[LengthValidator(min_chars=1), StructuralValidator(
            required_fields=("a", "b"),
        )],
        weights={"length": 0.3, "structural": 0.7},
    )
    # JSON with 1/2 required fields.
    response = _resp(json.dumps({"a": 1}))
    result = await composite.evaluate(job=_job(), response=response)
    # Length: ~100 (text is long enough).
    # Structural: 50 but baseline-fail → composite 0.
    assert result.passed_baseline is False
    assert result.score == 0  # baseline-fail floors composite
    assert result.breakdown["length"] == 100
    assert result.breakdown["structural"] == 50


@pytest.mark.asyncio
async def test_composite_baseline_pass_returns_arithmetic_average():
    """When all axes pass baseline, composite is the weighted average."""
    composite = CompositeValidator(
        axes=[LengthValidator(min_chars=1)],
        weights={"length": 1.0},
    )
    result = await composite.evaluate(
        job=_job(), response=_resp("a normal length response"),
    )
    assert result.passed_baseline is True
    assert result.score == 100


@pytest.mark.asyncio
async def test_composite_per_job_overrides():
    """schedule_logging applies length+structural; simple_chat applies
    length only."""
    composite = CompositeValidator(
        axes=[LengthValidator(min_chars=1), StructuralValidator()],
        weights={"length": 1.0, "structural": 0.0},        # default
        per_job_overrides={
            "schedule_logging": {"length": 0.4, "structural": 0.6},
        },
    )
    # For simple_chat: only length matters → text passes.
    r1 = await composite.evaluate(
        job=_job("simple_chat"), response=_resp("hi there"),
    )
    assert r1.score == 100
    # For schedule_logging: structural matters too — non-JSON fails.
    r2 = await composite.evaluate(
        job=_job("schedule_logging"), response=_resp("hi there"),
    )
    assert r2.passed_baseline is False
    assert r2.score == 0


@pytest.mark.asyncio
async def test_composite_no_axes_returns_neutral():
    """No axes / zero weights → neutral 50 (don't punish, don't pass)."""
    composite = CompositeValidator(
        axes=[LengthValidator()],
        weights={"length": 0.0},
    )
    result = await composite.evaluate(
        job=_job(), response=_resp("hello"),
    )
    assert result.score == 50
    assert result.passed_baseline is True


@pytest.mark.asyncio
async def test_composite_axis_exception_recorded_as_zero():
    """An axis that raises → that axis scores 0 and baseline-fails, but
    composite still returns (it's not a hard crash)."""
    class _Exploding:
        name = "explody"
        async def evaluate(self, *, job, response):
            raise RuntimeError("axis bug")

    composite = CompositeValidator(
        axes=[_Exploding(), LengthValidator()],
        weights={"explody": 0.5, "length": 0.5},
    )
    result = await composite.evaluate(
        job=_job(), response=_resp("hi"),
    )
    assert result.breakdown["explody"] == 0
    assert result.passed_baseline is False
    assert "axis error" in result.reason


# ---- make_dispatcher_validator -------------------------------------------


@pytest.mark.asyncio
async def test_make_dispatcher_validator_returns_score_and_baseline():
    composite = CompositeValidator(
        axes=[LengthValidator(min_chars=1)],
        weights={"length": 1.0},
    )
    fn = make_dispatcher_validator(composite)
    score, ok = await fn(_job(), _resp("hello there"))
    assert score == 100
    assert ok is True


@pytest.mark.asyncio
async def test_make_dispatcher_validator_baseline_fail_returns_zero():
    composite = CompositeValidator(
        axes=[LengthValidator()],
        weights={"length": 1.0},
    )
    fn = make_dispatcher_validator(composite)
    score, ok = await fn(_job(), _resp(""))
    assert score == 0
    assert ok is False
