"""Tests for src/job_factory/bench/runner.py.

Uses fake LLMAdapters returning canned responses to exercise the runner
end-to-end without any network/subprocess. Verifies:
  - Per-prompt failures don't abort the whole run.
  - Dimension scores aggregate correctly.
  - JobTypeWeights → per-job_type score conversion.
  - ScoreMatrix integration (optional update).
  - Adapter-level exceptions are caught and surfaced as BenchOutcome
    with passed=False.
  - GPU concurrency cap is honored.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from src.job_factory.bench.runner import BenchRunner, _aggregate_dimension
from src.job_factory.bench.types import (
    BenchOutcome,
    BenchPrompt,
    Dimension,
    JobTypeWeights,
)
from src.job_factory.score_matrix import ScoreMatrix
from src.llm.adapters.base import (
    AdapterRequest,
    AdapterResponse,
    ChatMessage,
    LLMAdapter,
)


# ---- Fake adapter ---------------------------------------------------------


@dataclass
class _FakeAdapter:
    """Return canned responses keyed by the user message text.

    If a message isn't in ``responses``, returns ``default_text``.
    Optionally raises a configured exception for one specific message.
    """

    provider_name: str = "fake"
    model_name: str = "fake-model"
    default_text: str = '{"Date": "2026-04-30", "Activity": "x", "Category": "Other"}'
    responses: dict[str, str] = field(default_factory=dict)
    exception_on: dict[str, Exception] = field(default_factory=dict)
    completion_tokens: int = 10
    duration_ms: int = 100
    calls: list[AdapterRequest] = field(default_factory=list)

    @property
    def provider(self) -> str:
        return self.provider_name

    @property
    def model(self) -> str:
        return self.model_name

    async def generate(self, request: AdapterRequest) -> AdapterResponse:
        self.calls.append(request)
        text_in = request.messages[-1].content
        if text_in in self.exception_on:
            raise self.exception_on[text_in]
        text_out = self.responses.get(text_in, self.default_text)
        return AdapterResponse(
            text=text_out,
            provider=self.provider_name,
            model=self.model_name,
            prompt_tokens=5,
            completion_tokens=self.completion_tokens,
            duration_ms=self.duration_ms,
            raw=None,
        )


# ---- Setup helpers --------------------------------------------------------


def _structural_dim(prompts: list[BenchPrompt]) -> Dimension:
    return Dimension(
        name="json",
        weight=0.2,
        judge="structural",
        prompts=prompts,
    )


def _label_match_dim(prompts: list[BenchPrompt]) -> Dimension:
    return Dimension(
        name="routing",
        weight=0.1,
        judge="label_match",
        prompts=prompts,
    )


# ---- BenchRunner basic ----------------------------------------------------


@pytest.mark.asyncio
async def test_run_single_model_single_dim_single_prompt(tmp_path):
    adapter = _FakeAdapter(
        responses={"give json": '{"x": 1}'},
    )
    dim = _structural_dim([BenchPrompt(id="p1", prompt="give json")])
    runner = BenchRunner(
        adapters={"m1": adapter},
        dimensions=[dim],
    )
    report = await runner.run()
    assert "m1" in report.results
    mres = report.results["m1"]
    assert mres.dimensions["json"].n == 1
    assert mres.dimensions["json"].n_passed == 1
    assert mres.dimensions["json"].mean_score == 100.0


@pytest.mark.asyncio
async def test_run_aggregates_pass_and_fail(tmp_path):
    adapter = _FakeAdapter(responses={
        "ok": '{"a": 1}',          # passes structural
        "bad": "not json",         # fails
    })
    dim = _structural_dim([
        BenchPrompt(id="p_ok", prompt="ok"),
        BenchPrompt(id="p_bad", prompt="bad"),
    ])
    runner = BenchRunner(adapters={"m1": adapter}, dimensions=[dim])
    report = await runner.run()
    score = report.results["m1"].dimensions["json"]
    assert score.n == 2
    assert score.n_passed == 1
    assert score.mean_score == pytest.approx(50.0)
    assert score.failure_rate == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_run_records_per_prompt_results():
    adapter = _FakeAdapter(responses={
        "p1": '{"a": 1}',
        "p2": "broken",
    })
    dim = _structural_dim([
        BenchPrompt(id="p1", prompt="p1"),
        BenchPrompt(id="p2", prompt="p2"),
    ])
    runner = BenchRunner(adapters={"m1": adapter}, dimensions=[dim])
    report = await runner.run()
    assert len(report.prompt_results) == 2
    by_id = {pr.prompt_id: pr.outcome for pr in report.prompt_results}
    assert by_id["p1"].passed is True
    assert by_id["p2"].passed is False


# ---- Adapter exception handling -------------------------------------------


@pytest.mark.asyncio
async def test_adapter_raising_does_not_abort_run():
    adapter = _FakeAdapter(
        responses={"good": '{"a": 1}'},
        exception_on={"bad": RuntimeError("simulated failure")},
    )
    dim = _structural_dim([
        BenchPrompt(id="p_bad", prompt="bad"),
        BenchPrompt(id="p_good", prompt="good"),
    ])
    runner = BenchRunner(adapters={"m1": adapter}, dimensions=[dim])
    report = await runner.run()
    # Both prompts get results — the exception is recorded as a fail.
    assert len(report.prompt_results) == 2
    by_id = {pr.prompt_id: pr.outcome for pr in report.prompt_results}
    assert by_id["p_bad"].passed is False
    assert "simulated failure" in by_id["p_bad"].error
    assert by_id["p_good"].passed is True


@pytest.mark.asyncio
async def test_unknown_target_model_silently_skipped():
    adapter = _FakeAdapter()
    runner = BenchRunner(
        adapters={"m1": adapter},
        dimensions=[_structural_dim([BenchPrompt(id="p", prompt="hi")])],
    )
    report = await runner.run(target_models=["m1", "doesnt_exist"])
    assert "m1" in report.results
    assert "doesnt_exist" not in report.results
    assert report.target_models == ["m1"]


# ---- JobTypeWeights conversion -------------------------------------------


@pytest.mark.asyncio
async def test_job_type_score_combines_dimensions():
    """schedule_logging weights: json=0.7, routing=0.3.
    Both dimensions score 100 → combined = 100. One scores 80 →
    weighted average."""
    adapter = _FakeAdapter(
        # json prompt gets a JSON response (100), routing prompt gets the
        # exact label expected (100).
        responses={
            "json prompt": '{"a": 1}',
            "routing prompt": "schedule_logging",
        },
    )
    dims = [
        _structural_dim([BenchPrompt(id="j1", prompt="json prompt")]),
        _label_match_dim([BenchPrompt(
            id="r1", prompt="routing prompt", expected="schedule_logging",
        )]),
    ]
    weights = {
        "schedule_logging": JobTypeWeights(
            job_type="schedule_logging",
            weights={"json": 0.7, "routing": 0.3},
        ),
    }
    runner = BenchRunner(
        adapters={"m1": adapter},
        dimensions=dims,
        job_type_weights=weights,
    )
    report = await runner.run()
    assert report.results["m1"].job_type_scores["schedule_logging"] == \
        pytest.approx(100.0)


@pytest.mark.asyncio
async def test_job_type_score_weighted_average():
    """json=100, routing=0 → 0.7*100 + 0.3*0 = 70."""
    adapter = _FakeAdapter(responses={
        "json prompt": '{"a": 1}',                  # structural pass → 100
        "routing prompt": "wrong_label",            # label mismatch → 0
    })
    dims = [
        _structural_dim([BenchPrompt(id="j1", prompt="json prompt")]),
        _label_match_dim([BenchPrompt(
            id="r1", prompt="routing prompt", expected="schedule_logging",
        )]),
    ]
    weights = {"schedule_logging": JobTypeWeights(
        job_type="schedule_logging",
        weights={"json": 0.7, "routing": 0.3},
    )}
    runner = BenchRunner(
        adapters={"m1": adapter},
        dimensions=dims,
        job_type_weights=weights,
    )
    report = await runner.run()
    assert report.results["m1"].job_type_scores["schedule_logging"] == \
        pytest.approx(70.0)


@pytest.mark.asyncio
async def test_job_type_score_skips_when_no_dim_matches():
    """JobType references a dimension not in this run → score absent
    (rather than misleading 0.0)."""
    adapter = _FakeAdapter(responses={"x": '{"a": 1}'})
    dims = [_structural_dim([BenchPrompt(id="j1", prompt="x")])]
    weights = {"only_speed": JobTypeWeights(
        job_type="only_speed",
        weights={"speed": 1.0},     # speed dim not in run
    )}
    runner = BenchRunner(
        adapters={"m1": adapter},
        dimensions=dims,
        job_type_weights=weights,
    )
    report = await runner.run()
    assert "only_speed" not in report.results["m1"].job_type_scores


# ---- ScoreMatrix integration ----------------------------------------------


@pytest.mark.asyncio
async def test_score_matrix_updated_with_job_type_scores(tmp_path):
    matrix = ScoreMatrix(path=tmp_path / "matrix.json", flush_threshold=10_000)
    adapter = _FakeAdapter(responses={"x": '{"a": 1}'})
    dims = [_structural_dim([BenchPrompt(id="j1", prompt="x")])]
    weights = {"schedule_logging": JobTypeWeights(
        job_type="schedule_logging",
        weights={"json": 1.0},
    )}
    runner = BenchRunner(
        adapters={"m1": adapter},
        dimensions=dims,
        job_type_weights=weights,
        score_matrix=matrix,
    )
    await runner.run()
    cell = matrix.get("schedule_logging", "m1")
    assert cell.n == 1
    assert cell.mean == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_score_matrix_not_updated_when_none():
    """Default score_matrix=None — runner mustn't touch any matrix."""
    adapter = _FakeAdapter(responses={"x": '{"a": 1}'})
    runner = BenchRunner(
        adapters={"m1": adapter},
        dimensions=[_structural_dim([BenchPrompt(id="j1", prompt="x")])],
        job_type_weights={"j": JobTypeWeights(
            job_type="j", weights={"json": 1.0},
        )},
        # score_matrix not passed
    )
    # Should complete without touching anything external.
    report = await runner.run()
    assert "j" in report.results["m1"].job_type_scores


# ---- GPU concurrency ------------------------------------------------------


@pytest.mark.asyncio
async def test_gpu_concurrency_serializes_models():
    """With concurrency=1, a slow first model must finish before the
    second starts."""
    timings: list[tuple[str, str, float]] = []  # (model, event, time)

    class _Tracking:
        def __init__(self, name, delay):
            self.name = name
            self.delay = delay

        @property
        def provider(self):
            return "fake"

        @property
        def model(self):
            return self.name

        async def generate(self, request):
            t0 = asyncio.get_event_loop().time()
            timings.append((self.name, "start", t0))
            await asyncio.sleep(self.delay)
            t1 = asyncio.get_event_loop().time()
            timings.append((self.name, "end", t1))
            return AdapterResponse(
                text='{"a": 1}', provider="fake", model=self.name,
                duration_ms=int(self.delay * 1000),
                completion_tokens=10,
            )

    adapters = {
        "slow": _Tracking("slow", delay=0.2),
        "fast": _Tracking("fast", delay=0.05),
    }
    dim = _structural_dim([BenchPrompt(id="p", prompt="hi")])
    runner = BenchRunner(
        adapters=adapters,
        dimensions=[dim],
        gpu_concurrency=1,
    )
    await runner.run()

    # With concurrency=1, one model fully completes before the other starts.
    starts = {m: t for (m, evt, t) in timings if evt == "start"}
    ends = {m: t for (m, evt, t) in timings if evt == "end"}
    # Either slow_end <= fast_start, or fast_end <= slow_start (depends on
    # asyncio.gather ordering).
    serialized = (
        ends["slow"] <= starts["fast"] + 1e-3
        or ends["fast"] <= starts["slow"] + 1e-3
    )
    assert serialized, f"models ran concurrently: {timings}"


# ---- Aggregation helper ---------------------------------------------------


def test_aggregate_dimension_no_outcomes():
    score = _aggregate_dimension([])
    assert score.n == 0
    assert score.mean_score == 0.0


def test_aggregate_dimension_mean_includes_failures():
    """Mean score includes fails (so models that fail a lot drop in
    mean — that's the signal we want)."""
    outcomes = [
        BenchOutcome(score=100, passed=True, latency_ms=100),
        BenchOutcome(score=0, passed=False, latency_ms=200),
    ]
    score = _aggregate_dimension(outcomes)
    assert score.n == 2
    assert score.n_passed == 1
    assert score.mean_score == 50.0


def test_aggregate_dimension_latency_uses_passed_only():
    """Failed prompts don't contribute to latency mean (a fast failure
    shouldn't make latency look better than it is)."""
    outcomes = [
        BenchOutcome(score=100, passed=True, latency_ms=200,
                     tokens_per_sec=25.0),
        BenchOutcome(score=0, passed=False, latency_ms=10),
    ]
    score = _aggregate_dimension(outcomes)
    assert score.mean_latency_ms == 200.0
    assert score.mean_tokens_per_sec == 25.0


# ---- Validation -----------------------------------------------------------


def test_invalid_gpu_concurrency_raises():
    with pytest.raises(ValueError):
        BenchRunner(
            adapters={}, dimensions=[],
            gpu_concurrency=0,
        )
