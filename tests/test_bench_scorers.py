"""Tests for src/job_factory/bench/scorers.py.

Each scorer is exercised against fabricated AdapterResponse objects so
the tests are hermetic (no LLM calls, no subprocess for non-execution
scorers). The ExecutionScorer test uses real pytest in a tempdir
because that's the only way to verify it actually runs code; we keep
the unit-test file tiny.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from src.job_factory.bench.scorers import (
    ExecutionScorer,
    LabelMatchScorer,
    LatencyScorer,
    LLMJudgeScorer,
    StructuralScorer,
    make_scorer,
)
from src.job_factory.bench.types import BenchPrompt
from src.llm.adapters.base import (
    AdapterRequest,
    AdapterResponse,
    ChatMessage,
)


def _resp(text: str, **kw) -> AdapterResponse:
    """Build an AdapterResponse with sensible defaults for tests."""
    defaults = {
        "provider": "test",
        "model": "test-model",
        "duration_ms": 100,
        "prompt_tokens": 10,
        "completion_tokens": 10,
    }
    defaults.update(kw)
    return AdapterResponse(text=text, **defaults)


# ---- StructuralScorer -----------------------------------------------------


@pytest.mark.asyncio
async def test_structural_valid_json_no_required_fields_full_marks():
    s = StructuralScorer()
    p = BenchPrompt(id="p1", prompt="output JSON")
    r = _resp('{"x": 1}')
    out = await s.score(prompt=p, response=r)
    assert out.score == 100
    assert out.passed is True


@pytest.mark.asyncio
async def test_structural_invalid_json_zero():
    s = StructuralScorer()
    p = BenchPrompt(id="p1", prompt="output JSON")
    r = _resp("not json {")
    out = await s.score(prompt=p, response=r)
    assert out.score == 0
    assert out.passed is False
    assert "JSONDecodeError" in out.error


@pytest.mark.asyncio
async def test_structural_strips_code_fences():
    s = StructuralScorer()
    p = BenchPrompt(id="p1", prompt="output JSON")
    r = _resp('```json\n{"x": 1}\n```')
    out = await s.score(prompt=p, response=r)
    assert out.score == 100


@pytest.mark.asyncio
async def test_structural_partial_required_fields():
    s = StructuralScorer()
    p = BenchPrompt(
        id="p1",
        prompt="x",
        required_fields=("Date", "Activity", "Category", "Focus Score"),
    )
    # 2 of 4 fields present.
    r = _resp(json.dumps({"Date": "2026-04-30", "Activity": "test"}))
    out = await s.score(prompt=p, response=r)
    assert out.score == 50.0
    assert out.passed is False


@pytest.mark.asyncio
async def test_structural_array_uses_first_element():
    s = StructuralScorer()
    p = BenchPrompt(id="p1", prompt="x", required_fields=("Date", "Activity"))
    r = _resp(json.dumps([
        {"Date": "2026-04-30", "Activity": "운동"},
        {"Date": "2026-04-30", "Activity": "코딩"},
    ]))
    out = await s.score(prompt=p, response=r)
    assert out.score == 100


# ---- ExecutionScorer ------------------------------------------------------


@pytest.mark.asyncio
async def test_execution_passing_solution(tmp_path):
    # Tiny unit test: solution must export a function ``add`` that adds 2.
    test_file = tmp_path / "test_add.py"
    test_file.write_text(textwrap.dedent("""
        from solution import add
        def test_basic():
            assert add(2, 3) == 5
            assert add(0, 0) == 0
    """).strip())

    s = ExecutionScorer()
    p = BenchPrompt(
        id="p1", prompt="implement add(a,b)",
        unit_test=str(test_file),
    )
    r = _resp("```python\ndef add(a, b):\n    return a + b\n```")
    out = await s.score(prompt=p, response=r)
    assert out.passed is True
    assert out.score == 100


@pytest.mark.asyncio
async def test_execution_failing_solution(tmp_path):
    test_file = tmp_path / "test_add.py"
    test_file.write_text(
        "from solution import add\ndef test_basic(): assert add(2, 3) == 5"
    )

    s = ExecutionScorer()
    p = BenchPrompt(
        id="p1", prompt="implement add",
        unit_test=str(test_file),
    )
    # Wrong implementation.
    r = _resp("```python\ndef add(a, b):\n    return a - b\n```")
    out = await s.score(prompt=p, response=r)
    assert out.passed is False
    assert out.score == 0
    assert "pytest failed" in out.error


@pytest.mark.asyncio
async def test_execution_missing_unit_test_returns_zero():
    s = ExecutionScorer()
    p = BenchPrompt(id="p1", prompt="x", unit_test="/no/such/path.py")
    r = _resp("```python\nprint('hi')\n```")
    out = await s.score(prompt=p, response=r)
    assert out.passed is False
    assert "not found" in out.error


@pytest.mark.asyncio
async def test_execution_handles_no_code_block():
    """Falls back to whole response as code if no ``` fence."""
    # Use a unit_test that won't import — expects failure but should not raise.
    s = ExecutionScorer()
    p = BenchPrompt(id="p1", prompt="x", unit_test="")
    r = _resp("plain text reply")
    out = await s.score(prompt=p, response=r)
    assert out.passed is False
    # When unit_test is empty, scorer reports the configuration error.
    assert "unit_test" in out.error.lower()


# ---- LLMJudgeScorer -------------------------------------------------------


class _FakeJudgeAdapter:
    """LLMAdapter that returns a canned judgement."""

    def __init__(self, response_text: str):
        self._text = response_text

    @property
    def provider(self) -> str:
        return "fake_judge"

    @property
    def model(self) -> str:
        return "fake-judge-model"

    async def generate(self, request: AdapterRequest) -> AdapterResponse:
        return _resp(self._text, provider="fake_judge", model="fake-judge-model")


@pytest.mark.asyncio
async def test_llm_judge_parses_score_line():
    judge = _FakeJudgeAdapter("SCORE: 85\nREASON: looks good")
    s = LLMJudgeScorer(judge)
    p = BenchPrompt(id="p1", prompt="요약하라", rubric="자연스러운 한국어")
    r = _resp("이것은 요약입니다.")
    out = await s.score(prompt=p, response=r)
    assert out.score == 85
    assert out.passed is True


@pytest.mark.asyncio
async def test_llm_judge_clamps_score():
    judge = _FakeJudgeAdapter("SCORE: 999\nREASON: somehow over")
    s = LLMJudgeScorer(judge)
    p = BenchPrompt(id="p1", prompt="x", rubric="x")
    r = _resp("x")
    out = await s.score(prompt=p, response=r)
    assert out.score == 100  # clamped


@pytest.mark.asyncio
async def test_llm_judge_low_score_marks_not_passed():
    judge = _FakeJudgeAdapter("SCORE: 30\nREASON: bad")
    s = LLMJudgeScorer(judge)
    p = BenchPrompt(id="p1", prompt="x", rubric="x")
    r = _resp("x")
    out = await s.score(prompt=p, response=r)
    assert out.score == 30
    assert out.passed is False


@pytest.mark.asyncio
async def test_llm_judge_unparseable_response():
    judge = _FakeJudgeAdapter("nothing structured here")
    s = LLMJudgeScorer(judge)
    p = BenchPrompt(id="p1", prompt="x", rubric="x")
    r = _resp("x")
    out = await s.score(prompt=p, response=r)
    assert out.score == 0
    assert out.passed is False
    assert "unparseable" in out.error


# ---- LabelMatchScorer -----------------------------------------------------


@pytest.mark.asyncio
async def test_label_match_exact():
    s = LabelMatchScorer()
    p = BenchPrompt(id="p1", prompt="classify", expected="schedule_logging")
    r = _resp("schedule_logging")
    out = await s.score(prompt=p, response=r)
    assert out.score == 100
    assert out.passed is True


@pytest.mark.asyncio
async def test_label_match_case_insensitive_and_punctuation():
    s = LabelMatchScorer()
    p = BenchPrompt(id="p1", prompt="classify", expected="schedule_logging")
    r = _resp("Schedule_Logging.")
    out = await s.score(prompt=p, response=r)
    assert out.score == 100


@pytest.mark.asyncio
async def test_label_match_substring_mode():
    s = LabelMatchScorer(allow_substring=True)
    p = BenchPrompt(id="p1", prompt="classify", expected="schedule_logging")
    r = _resp("the answer is schedule_logging here")
    out = await s.score(prompt=p, response=r)
    assert out.score == 100


@pytest.mark.asyncio
async def test_label_match_mismatch():
    s = LabelMatchScorer()
    p = BenchPrompt(id="p1", prompt="classify", expected="schedule_logging")
    r = _resp("simple_chat")
    out = await s.score(prompt=p, response=r)
    assert out.score == 0
    assert out.passed is False


# ---- LatencyScorer --------------------------------------------------------


@pytest.mark.asyncio
async def test_latency_at_target_full_marks():
    s = LatencyScorer(target_tokens_per_sec=30.0)
    # 30 tokens in 1000ms = 30 tps → full marks.
    r = _resp("hi", duration_ms=1000, completion_tokens=30)
    p = BenchPrompt(id="p1", prompt="x")
    out = await s.score(prompt=p, response=r)
    assert out.score == 100
    assert out.passed is True
    assert out.tokens_per_sec == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_latency_below_target_scaled():
    s = LatencyScorer(target_tokens_per_sec=30.0)
    # 15 tps — half target → score 50.
    r = _resp("hi", duration_ms=1000, completion_tokens=15)
    p = BenchPrompt(id="p1", prompt="x")
    out = await s.score(prompt=p, response=r)
    assert out.score == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_latency_clamped_at_100():
    s = LatencyScorer(target_tokens_per_sec=30.0)
    r = _resp("hi", duration_ms=100, completion_tokens=300)  # 3000 tps
    p = BenchPrompt(id="p1", prompt="x")
    out = await s.score(prompt=p, response=r)
    assert out.score == 100  # clamped


@pytest.mark.asyncio
async def test_latency_zero_duration_returns_error():
    s = LatencyScorer()
    r = _resp("hi", duration_ms=0, completion_tokens=10)
    p = BenchPrompt(id="p1", prompt="x")
    out = await s.score(prompt=p, response=r)
    assert out.passed is False
    assert "missing" in out.error


# ---- make_scorer factory --------------------------------------------------


def test_make_scorer_returns_correct_types():
    assert isinstance(make_scorer("structural"), StructuralScorer)
    assert isinstance(make_scorer("execution"), ExecutionScorer)
    assert isinstance(make_scorer("label_match"), LabelMatchScorer)
    assert isinstance(make_scorer("latency"), LatencyScorer)


def test_make_scorer_llm_judge_requires_adapter():
    with pytest.raises(ValueError, match="judge_adapter"):
        make_scorer("llm_judge")


def test_make_scorer_unknown_judge_raises():
    with pytest.raises(ValueError, match="unknown judge"):
        make_scorer("nope")
