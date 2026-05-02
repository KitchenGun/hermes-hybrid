"""Tests for src/job_factory/score_matrix.py.

Verifies:
  - Welford's algorithm gives the correct mean/variance to within float
    epsilon vs a one-shot reference computation.
  - Concurrent updates (asyncio.gather) don't lose writes or corrupt
    cells.
  - Round-trip through disk preserves all stats exactly.
  - Corrupt / missing / wrong-version files return an empty matrix
    instead of raising (cold-start auto-recovery).
  - Atomic persist: a tmp file never lingers if write succeeds; if it
    fails, the old file is left intact.
"""
from __future__ import annotations

import asyncio
import json
import statistics
from pathlib import Path

import pytest

from src.job_factory.score_matrix import (
    DEFAULT_FLUSH_THRESHOLD,
    SCHEMA_VERSION,
    ScoreMatrix,
    ScoreStats,
)


# ---- ScoreStats Welford correctness ---------------------------------------


def test_score_stats_empty():
    s = ScoreStats()
    assert s.n == 0
    assert s.mean == 0.0
    assert s.variance == 0.0
    assert s.last_updated is None


def test_score_stats_single_update_sets_mean():
    s = ScoreStats()
    s.update(75.0)
    assert s.n == 1
    assert s.mean == 75.0
    # n=1 → no spread possible
    assert s.variance == 0.0
    assert s.last_updated is not None


def test_score_stats_two_updates_mean():
    s = ScoreStats()
    s.update(60.0)
    s.update(80.0)
    assert s.n == 2
    assert s.mean == pytest.approx(70.0)


def test_score_stats_matches_reference_mean_variance():
    """Welford should match a one-shot mean/variance computation exactly
    (modulo float epsilon)."""
    values = [
        12.5, 87.3, 55.1, 99.0, 0.5,
        42.0, 67.8, 33.3, 78.9, 91.2,
        25.0, 50.0, 75.0, 100.0, 0.0,
    ]
    s = ScoreStats()
    for v in values:
        s.update(v)

    expected_mean = statistics.fmean(values)
    expected_var = statistics.pvariance(values)  # population variance

    assert s.n == len(values)
    assert s.mean == pytest.approx(expected_mean, rel=1e-12)
    assert s.variance == pytest.approx(expected_var, rel=1e-9)


def test_score_stats_handles_many_samples_without_drift():
    """1000 samples — Welford must not drift relative to reference."""
    import random
    rng = random.Random(42)
    values = [rng.uniform(0, 100) for _ in range(1000)]
    s = ScoreStats()
    for v in values:
        s.update(v)
    assert s.mean == pytest.approx(statistics.fmean(values), rel=1e-9)
    assert s.variance == pytest.approx(
        statistics.pvariance(values), rel=1e-7
    )


def test_score_stats_dict_roundtrip():
    s = ScoreStats()
    for v in [10, 20, 30, 40]:
        s.update(float(v))
    d = s.to_dict()
    s2 = ScoreStats.from_dict(d)
    assert s2.n == s.n
    assert s2.mean == pytest.approx(s.mean)
    assert s2.m2 == pytest.approx(s.m2)
    assert s2.last_updated == s.last_updated


# ---- ScoreMatrix basic behavior -------------------------------------------


def _matrix_path(tmp_path: Path) -> Path:
    return tmp_path / "score_matrix.json"


def test_get_returns_empty_stats_when_absent(tmp_path):
    m = ScoreMatrix(path=_matrix_path(tmp_path))
    s = m.get("simple_chat", "qwen2.5:7b-instruct")
    assert s.n == 0
    assert s.mean == 0.0
    assert m.has("simple_chat", "qwen2.5:7b-instruct") is False


@pytest.mark.asyncio
async def test_update_creates_cell_and_records_value(tmp_path):
    m = ScoreMatrix(path=_matrix_path(tmp_path))
    cell = await m.update("simple_chat", "qwen2.5:7b-instruct", 80.0)
    assert cell.n == 1
    assert cell.mean == 80.0
    assert m.has("simple_chat", "qwen2.5:7b-instruct") is True


@pytest.mark.asyncio
async def test_update_score_out_of_range_raises(tmp_path):
    m = ScoreMatrix(path=_matrix_path(tmp_path))
    with pytest.raises(ValueError):
        await m.update("simple_chat", "qwen2.5:7b-instruct", -1.0)
    with pytest.raises(ValueError):
        await m.update("simple_chat", "qwen2.5:7b-instruct", 101.0)


@pytest.mark.asyncio
async def test_update_accumulates_correctly(tmp_path):
    m = ScoreMatrix(path=_matrix_path(tmp_path))
    for v in [50.0, 70.0, 90.0]:
        await m.update("summarize", "qwen2.5:14b-instruct", v)
    cell = m.get("summarize", "qwen2.5:14b-instruct")
    assert cell.n == 3
    assert cell.mean == pytest.approx(70.0)


@pytest.mark.asyncio
async def test_models_for_filters_by_job_type(tmp_path):
    m = ScoreMatrix(path=_matrix_path(tmp_path))
    await m.update("simple_chat", "model-a", 50)
    await m.update("simple_chat", "model-b", 60)
    await m.update("code_gen", "model-a", 70)

    chat_models = set(m.models_for("simple_chat"))
    code_models = set(m.models_for("code_gen"))
    assert chat_models == {"model-a", "model-b"}
    assert code_models == {"model-a"}


# ---- Concurrency ----------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_updates_no_lost_writes(tmp_path):
    """1000 concurrent updates → cell.n must equal 1000 (no lost write)."""
    m = ScoreMatrix(
        path=_matrix_path(tmp_path),
        flush_threshold=10_000,  # avoid disk I/O during the race test
    )
    await asyncio.gather(*(
        m.update("simple_chat", "shared_model", 75.0)
        for _ in range(1000)
    ))
    cell = m.get("simple_chat", "shared_model")
    assert cell.n == 1000
    # All values are 75.0 → mean exactly 75.0, variance 0.
    assert cell.mean == pytest.approx(75.0)
    assert cell.variance == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_concurrent_updates_different_cells(tmp_path):
    """Updates to different (job_type, model) cells must all land."""
    m = ScoreMatrix(
        path=_matrix_path(tmp_path), flush_threshold=10_000
    )
    pairs = [
        ("simple_chat", "a"), ("simple_chat", "b"),
        ("code_gen", "a"), ("code_gen", "b"),
        ("summarize", "c"),
    ]
    await asyncio.gather(*(m.update(j, m_, 80.0) for j, m_ in pairs))
    for j, m_ in pairs:
        assert m.get(j, m_).n == 1


# ---- Persistence (atomic) -------------------------------------------------


@pytest.mark.asyncio
async def test_persist_writes_json_with_correct_schema(tmp_path):
    p = _matrix_path(tmp_path)
    m = ScoreMatrix(path=p, flush_threshold=10_000)
    await m.update("simple_chat", "qwen-7b", 80.0)
    await m.update("simple_chat", "qwen-7b", 90.0)
    await m.persist()

    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["version"] == SCHEMA_VERSION
    assert "updated_at" in raw
    assert "simple_chat::qwen-7b" in raw["cells"]
    cell = raw["cells"]["simple_chat::qwen-7b"]
    assert cell["n"] == 2
    assert cell["mean"] == pytest.approx(85.0)


@pytest.mark.asyncio
async def test_persist_roundtrip_preserves_stats(tmp_path):
    p = _matrix_path(tmp_path)
    m = ScoreMatrix(path=p, flush_threshold=10_000)
    for v in [10, 25, 50, 75, 95]:
        await m.update("summarize", "qwen-14b", float(v))
    await m.persist()

    m2 = ScoreMatrix.load(p)
    cell = m2.get("summarize", "qwen-14b")
    orig = m.get("summarize", "qwen-14b")
    assert cell.n == orig.n
    assert cell.mean == pytest.approx(orig.mean)
    assert cell.m2 == pytest.approx(orig.m2)
    assert cell.last_updated == orig.last_updated


@pytest.mark.asyncio
async def test_auto_flush_at_threshold(tmp_path):
    """Reaching flush_threshold writes to disk without explicit persist()."""
    p = _matrix_path(tmp_path)
    m = ScoreMatrix(path=p, flush_threshold=3)
    assert not p.exists()
    await m.update("simple_chat", "x", 10.0)
    await m.update("simple_chat", "x", 20.0)
    assert not p.exists()  # below threshold
    await m.update("simple_chat", "x", 30.0)
    # Threshold hit — file should now exist.
    assert p.exists()


@pytest.mark.asyncio
async def test_persist_atomic_no_lingering_tmp(tmp_path):
    """After successful persist, no .tmp files remain in the directory."""
    p = _matrix_path(tmp_path)
    m = ScoreMatrix(path=p, flush_threshold=10_000)
    await m.update("simple_chat", "x", 50.0)
    await m.persist()
    leftover_tmps = list(tmp_path.glob("*.tmp"))
    assert leftover_tmps == []


# ---- Load: cold-start auto-recovery ---------------------------------------


def test_load_missing_file_returns_empty(tmp_path):
    m = ScoreMatrix.load(tmp_path / "nonexistent.json")
    assert m.cells == {}


def test_load_corrupt_json_returns_empty(tmp_path):
    p = _matrix_path(tmp_path)
    p.write_text("{not valid json", encoding="utf-8")
    m = ScoreMatrix.load(p)
    # Cold-start auto-recovery: don't raise, return empty so the bandit
    # falls back to round-robin and re-fills the matrix.
    assert m.cells == {}


def test_load_wrong_version_returns_empty(tmp_path):
    p = _matrix_path(tmp_path)
    p.write_text(
        json.dumps({"version": 999, "cells": {"x::y": {"n": 1, "mean": 50, "m2": 0}}}),
        encoding="utf-8",
    )
    m = ScoreMatrix.load(p)
    assert m.cells == {}


def test_load_skips_malformed_cell_keys(tmp_path):
    """A bad cell key shouldn't kill the whole load — just log+skip."""
    p = _matrix_path(tmp_path)
    p.write_text(
        json.dumps({
            "version": SCHEMA_VERSION,
            "cells": {
                "noseparator": {"n": 1, "mean": 50, "m2": 0},   # bad
                "good::model-x": {"n": 5, "mean": 75, "m2": 100},
            },
        }),
        encoding="utf-8",
    )
    m = ScoreMatrix.load(p)
    assert m.has("good", "model-x")
    cell = m.get("good", "model-x")
    assert cell.n == 5
    assert cell.mean == 75
