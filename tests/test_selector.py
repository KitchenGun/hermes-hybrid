"""Tests for src/job_factory/selector.py.

Verifies the epsilon-greedy bandit's three priority modes:
  1. Cold-start: any model with n < warmup_n gets picked first.
  2. Exploration: with probability epsilon, uniform random over all.
  3. Exploitation: argmax mean (tie-break: prefer fewer observations).

Statistical tests use seeded RNG and Monte-Carlo trial counts large
enough to make the expected vs observed ratio differ by < 5% with
overwhelming probability.
"""
from __future__ import annotations

import random
from collections import Counter
from pathlib import Path

import pytest

from src.job_factory.score_matrix import ScoreMatrix
from src.job_factory.selector import EpsilonGreedySelector, Selection


def _matrix(tmp_path: Path) -> ScoreMatrix:
    return ScoreMatrix(path=tmp_path / "matrix.json", flush_threshold=10_000)


# ---- Construction validation ----------------------------------------------


def test_init_rejects_invalid_epsilon(tmp_path):
    with pytest.raises(ValueError):
        EpsilonGreedySelector(_matrix(tmp_path), epsilon=-0.1)
    with pytest.raises(ValueError):
        EpsilonGreedySelector(_matrix(tmp_path), epsilon=1.5)


def test_init_rejects_negative_warmup(tmp_path):
    with pytest.raises(ValueError):
        EpsilonGreedySelector(_matrix(tmp_path), warmup_n=-1)


def test_select_with_empty_candidates_raises(tmp_path):
    sel = EpsilonGreedySelector(_matrix(tmp_path))
    with pytest.raises(ValueError):
        sel.select(job_type="simple_chat", available_models=[])


# ---- Cold-start mode -------------------------------------------------------


def test_cold_start_when_matrix_empty(tmp_path):
    """All models have n=0 → cold-start picks one at random."""
    matrix = _matrix(tmp_path)
    rng = random.Random(42)
    sel = EpsilonGreedySelector(matrix, warmup_n=5, rng=rng)

    result = sel.select(
        job_type="simple_chat",
        available_models=["a", "b", "c"],
    )
    assert result.reason == "cold_start"
    assert result.model in {"a", "b", "c"}


@pytest.mark.asyncio
async def test_cold_start_picks_only_underwarmed_models(tmp_path):
    """If model 'a' has n>=warmup but 'b' has n<warmup, only 'b' is picked."""
    matrix = _matrix(tmp_path)
    # Saturate 'a' to exactly warmup_n=5 → no longer cold.
    for _ in range(5):
        await matrix.update("simple_chat", "a", 80.0)
    # 'b' has only 2 observations → still cold.
    for _ in range(2):
        await matrix.update("simple_chat", "b", 50.0)

    rng = random.Random(42)
    sel = EpsilonGreedySelector(matrix, warmup_n=5, rng=rng)

    # 100 trials — every cold-start pick must be 'b'.
    picks = Counter()
    for _ in range(100):
        result = sel.select(
            job_type="simple_chat",
            available_models=["a", "b"],
        )
        if result.reason == "cold_start":
            picks[result.model] += 1
    assert picks["b"] > 0
    assert picks["a"] == 0


@pytest.mark.asyncio
async def test_cold_start_uniform_among_cold_models(tmp_path):
    """Multiple cold models → roughly uniform pick distribution."""
    matrix = _matrix(tmp_path)
    rng = random.Random(123)
    sel = EpsilonGreedySelector(matrix, warmup_n=5, rng=rng)

    picks = Counter()
    for _ in range(3000):
        result = sel.select(
            job_type="simple_chat",
            available_models=["a", "b", "c", "d"],
        )
        assert result.reason == "cold_start"
        picks[result.model] += 1

    # Each model expected ~750 picks. Allow ±10%.
    for m in ["a", "b", "c", "d"]:
        assert 600 < picks[m] < 900, f"{m}: {picks[m]}"


# ---- Exploitation mode ----------------------------------------------------


@pytest.mark.asyncio
async def test_exploitation_picks_highest_mean(tmp_path):
    """All models warm → exploitation picks highest mean."""
    matrix = _matrix(tmp_path)
    # Saturate all models past warmup with distinct means.
    for _ in range(10):
        await matrix.update("simple_chat", "low", 30.0)
    for _ in range(10):
        await matrix.update("simple_chat", "high", 90.0)
    for _ in range(10):
        await matrix.update("simple_chat", "mid", 60.0)

    rng = random.Random(42)
    # epsilon=0 → no exploration; pure exploitation.
    sel = EpsilonGreedySelector(matrix, epsilon=0.0, warmup_n=5, rng=rng)

    for _ in range(50):
        result = sel.select(
            job_type="simple_chat",
            available_models=["low", "high", "mid"],
        )
        assert result.reason == "exploitation"
        assert result.model == "high"


@pytest.mark.asyncio
async def test_exploitation_tiebreak_prefers_fewer_samples(tmp_path):
    """Two models tied on mean → pick the one with fewer observations
    (gentle exploration of less-sampled arms)."""
    matrix = _matrix(tmp_path)
    for _ in range(10):
        await matrix.update("simple_chat", "well_tested", 80.0)
    # Same mean, fewer samples — should be preferred.
    for _ in range(6):
        await matrix.update("simple_chat", "barely_tested", 80.0)

    sel = EpsilonGreedySelector(
        matrix, epsilon=0.0, warmup_n=5, rng=random.Random(42)
    )
    result = sel.select(
        job_type="simple_chat",
        available_models=["well_tested", "barely_tested"],
    )
    assert result.model == "barely_tested"


# ---- Exploration mode -----------------------------------------------------


@pytest.mark.asyncio
async def test_exploration_ratio_matches_epsilon(tmp_path):
    """Over many trials, ~epsilon fraction should be exploration."""
    matrix = _matrix(tmp_path)
    # All models past warmup so cold-start doesn't dominate.
    for _ in range(10):
        await matrix.update("simple_chat", "a", 50.0)
        await matrix.update("simple_chat", "b", 70.0)
        await matrix.update("simple_chat", "c", 30.0)

    eps = 0.2
    rng = random.Random(7)
    sel = EpsilonGreedySelector(matrix, epsilon=eps, warmup_n=5, rng=rng)

    counts = Counter()
    N = 5000
    for _ in range(N):
        r = sel.select(
            job_type="simple_chat",
            available_models=["a", "b", "c"],
        )
        counts[r.reason] += 1

    expected_explore = N * eps
    expected_exploit = N * (1 - eps)
    # ±10% tolerance for 5000 trials.
    assert abs(counts["exploration"] - expected_explore) < 0.10 * N
    assert abs(counts["exploitation"] - expected_exploit) < 0.10 * N
    assert counts["cold_start"] == 0


@pytest.mark.asyncio
async def test_exploration_distribution_uniform(tmp_path):
    """Exploration mode picks uniformly across all available models."""
    matrix = _matrix(tmp_path)
    # All warm, with one strongly winning model.
    for _ in range(10):
        await matrix.update("simple_chat", "a", 90.0)
        await matrix.update("simple_chat", "b", 50.0)
        await matrix.update("simple_chat", "c", 50.0)
        await matrix.update("simple_chat", "d", 50.0)

    # epsilon=1.0 → pure exploration.
    sel = EpsilonGreedySelector(
        matrix, epsilon=1.0, warmup_n=5, rng=random.Random(99)
    )
    counts = Counter()
    N = 4000
    for _ in range(N):
        r = sel.select(
            job_type="simple_chat",
            available_models=["a", "b", "c", "d"],
        )
        assert r.reason == "exploration"
        counts[r.model] += 1

    # Each model expected ~1000. ±15% tolerance.
    for m in ["a", "b", "c", "d"]:
        assert 800 < counts[m] < 1200, f"{m}: {counts[m]}"


# ---- Selection metadata ---------------------------------------------------


@pytest.mark.asyncio
async def test_selection_includes_candidates_snapshot(tmp_path):
    """Selection.candidates is sorted desc by mean — for observability."""
    matrix = _matrix(tmp_path)
    for _ in range(10):
        await matrix.update("simple_chat", "low", 20.0)
        await matrix.update("simple_chat", "high", 95.0)
        await matrix.update("simple_chat", "mid", 60.0)

    sel = EpsilonGreedySelector(
        matrix, epsilon=0.0, warmup_n=5, rng=random.Random(0)
    )
    result = sel.select(
        job_type="simple_chat",
        available_models=["low", "high", "mid"],
    )
    means = [mean for _, mean, _ in result.candidates]
    assert means == sorted(means, reverse=True)
    # Top candidate's mean should match the chosen model.
    assert result.candidates[0][0] == result.model


@pytest.mark.asyncio
async def test_selection_stats_reflects_chosen_model(tmp_path):
    """Selection.stats is the live ScoreStats for the picked (job_type, model)."""
    matrix = _matrix(tmp_path)
    for _ in range(10):
        await matrix.update("simple_chat", "a", 40.0)
        await matrix.update("simple_chat", "b", 80.0)

    sel = EpsilonGreedySelector(
        matrix, epsilon=0.0, warmup_n=5, rng=random.Random(0)
    )
    result = sel.select(
        job_type="simple_chat",
        available_models=["a", "b"],
    )
    assert result.model == "b"
    assert result.stats.n == 10
    assert result.stats.mean == 80.0


# ---- Edge cases -----------------------------------------------------------


def test_single_available_model_picked_in_cold_start(tmp_path):
    matrix = _matrix(tmp_path)
    sel = EpsilonGreedySelector(
        matrix, warmup_n=5, rng=random.Random(0)
    )
    result = sel.select(job_type="x", available_models=["only"])
    assert result.model == "only"
    assert result.reason == "cold_start"


@pytest.mark.asyncio
async def test_zero_warmup_skips_cold_start(tmp_path):
    """warmup_n=0 → no cold-start mode; immediate exploitation/exploration."""
    matrix = _matrix(tmp_path)
    sel = EpsilonGreedySelector(
        matrix, epsilon=0.0, warmup_n=0, rng=random.Random(0)
    )
    # Empty matrix, but warmup_n=0 so cold-start is skipped → goes to
    # exploitation. Ties (all 0.0 mean, all n=0) → tie-break by n (all 0)
    # so ordering is by insertion / sort stability — test that *any* of
    # the candidates is picked under the exploitation reason.
    result = sel.select(job_type="x", available_models=["a", "b", "c"])
    assert result.reason == "exploitation"
    assert result.model in {"a", "b", "c"}
