"""Tests for src/job_factory/bench/scheduler.py.

Uses a stubbed Ollama discovery and a recording bench callback to keep
the tests hermetic. Verifies:

  - Startup detects unbenched models from ScoreMatrix and triggers a bench.
  - The watch loop spots new models added between polls and benches them.
  - Models removed from Ollama don't trigger any bench (just a log).
  - Stop() cancels both the watch loop and any in-flight bench.
  - A bench callback that raises doesn't kill the scheduler.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from src.job_factory.bench.scheduler import BenchScheduler
from src.job_factory.score_matrix import ScoreMatrix


# ---- Helpers --------------------------------------------------------------


class _RecordingBench:
    """Records calls to the bench callback. Optionally simulates work
    via ``await asyncio.sleep(work_s)``."""

    def __init__(self, work_s: float = 0.0):
        self.calls: list[list[str]] = []
        self.work_s = work_s
        self.started = asyncio.Event()
        self.finished = asyncio.Event()

    async def __call__(self, models: list[str]) -> None:
        self.calls.append(list(models))
        self.started.set()
        if self.work_s:
            await asyncio.sleep(self.work_s)
        self.finished.set()


def _matrix(tmp_path: Path) -> ScoreMatrix:
    return ScoreMatrix(path=tmp_path / "m.json", flush_threshold=10_000)


def _patch_ollama(monkeypatch, scheduler: BenchScheduler, models: list[str]):
    """Replace _discover_installed_models on this scheduler instance."""

    async def fake_discover():
        return list(models)

    monkeypatch.setattr(scheduler, "_discover_installed_models", fake_discover)


# ---- Startup behavior -----------------------------------------------------


@pytest.mark.asyncio
async def test_startup_benches_all_when_matrix_empty(tmp_path, monkeypatch):
    matrix = _matrix(tmp_path)
    bench = _RecordingBench()
    scheduler = BenchScheduler(
        score_matrix=matrix,
        ollama_base_url="http://localhost:11434",
        bench_callback=bench,
        poll_interval_s=3600,  # don't trigger watch loop during test
        debounce_s=0,
    )
    _patch_ollama(monkeypatch, scheduler, ["m1", "m2", "m3"])

    await scheduler.start()
    # Wait briefly for the spawned bench task to start.
    await asyncio.wait_for(bench.started.wait(), timeout=2.0)
    await asyncio.wait_for(bench.finished.wait(), timeout=2.0)
    await scheduler.stop()

    # _find_unbenched returns a set, so order isn't guaranteed.
    assert len(bench.calls) == 1
    assert set(bench.calls[0]) == {"m1", "m2", "m3"}


@pytest.mark.asyncio
async def test_startup_skips_models_already_benched(tmp_path, monkeypatch):
    matrix = _matrix(tmp_path)
    # Seed matrix with an observation for m1 → m1 is "benched".
    await matrix.update("simple_chat", "m1", 80.0)

    bench = _RecordingBench()
    scheduler = BenchScheduler(
        score_matrix=matrix,
        ollama_base_url="http://localhost:11434",
        bench_callback=bench,
        poll_interval_s=3600,
        debounce_s=0,
    )
    _patch_ollama(monkeypatch, scheduler, ["m1", "m2"])

    await scheduler.start()
    await asyncio.wait_for(bench.started.wait(), timeout=2.0)
    await asyncio.wait_for(bench.finished.wait(), timeout=2.0)
    await scheduler.stop()

    # m1 already benched → only m2 in the call.
    assert len(bench.calls) == 1
    assert bench.calls[0] == ["m2"]


@pytest.mark.asyncio
async def test_startup_no_bench_when_all_already_benched(
    tmp_path, monkeypatch
):
    matrix = _matrix(tmp_path)
    await matrix.update("simple_chat", "m1", 80.0)
    await matrix.update("simple_chat", "m2", 70.0)

    bench = _RecordingBench()
    scheduler = BenchScheduler(
        score_matrix=matrix,
        ollama_base_url="http://localhost:11434",
        bench_callback=bench,
        poll_interval_s=3600,
        debounce_s=0,
    )
    _patch_ollama(monkeypatch, scheduler, ["m1", "m2"])

    await scheduler.start()
    # Give it a moment to confirm no bench fires.
    await asyncio.sleep(0.1)
    await scheduler.stop()

    assert bench.calls == []


# ---- Watch loop -----------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_loop_picks_up_new_models(tmp_path, monkeypatch):
    matrix = _matrix(tmp_path)
    bench = _RecordingBench()

    # Start with a single model, all benched. Watch loop discovers a
    # new model on the next poll.
    await matrix.update("simple_chat", "m1", 80.0)

    discover_results = [
        ["m1"],            # initial (no new)
        ["m1", "m2"],      # m2 appeared
    ]
    discover_idx = {"i": 0}

    async def fake_discover():
        i = discover_idx["i"]
        discover_idx["i"] = min(i + 1, len(discover_results) - 1)
        return list(discover_results[i])

    scheduler = BenchScheduler(
        score_matrix=matrix,
        ollama_base_url="http://localhost:11434",
        bench_callback=bench,
        poll_interval_s=0,    # fire watch loop ASAP
        debounce_s=0,
    )
    monkeypatch.setattr(scheduler, "_discover_installed_models", fake_discover)

    await scheduler.start()
    # Wait for second poll to see m2 → bench triggered.
    await asyncio.wait_for(bench.started.wait(), timeout=3.0)
    await asyncio.wait_for(bench.finished.wait(), timeout=3.0)
    await scheduler.stop()

    assert any("m2" in c for c in bench.calls)


@pytest.mark.asyncio
async def test_watch_loop_ignores_removed_models(tmp_path, monkeypatch):
    """A model removed from Ollama shouldn't trigger any bench."""
    matrix = _matrix(tmp_path)
    await matrix.update("simple_chat", "m1", 80.0)
    await matrix.update("simple_chat", "m2", 70.0)

    bench = _RecordingBench()
    discover_results = [
        ["m1", "m2"],
        ["m1"],   # m2 removed
    ]
    idx = {"i": 0}

    async def fake_discover():
        i = idx["i"]
        idx["i"] = min(i + 1, len(discover_results) - 1)
        return list(discover_results[i])

    scheduler = BenchScheduler(
        score_matrix=matrix,
        ollama_base_url="http://localhost:11434",
        bench_callback=bench,
        poll_interval_s=0,
        debounce_s=0,
    )
    monkeypatch.setattr(scheduler, "_discover_installed_models", fake_discover)

    await scheduler.start()
    # Let the watch loop poll a couple of times.
    await asyncio.sleep(0.2)
    await scheduler.stop()

    assert bench.calls == []  # no new models ever appeared


# ---- Stop / lifecycle -----------------------------------------------------


@pytest.mark.asyncio
async def test_stop_cancels_inflight_bench(tmp_path, monkeypatch):
    matrix = _matrix(tmp_path)
    # Long-running bench so we can cancel it mid-flight.
    bench = _RecordingBench(work_s=10.0)
    scheduler = BenchScheduler(
        score_matrix=matrix,
        ollama_base_url="http://localhost:11434",
        bench_callback=bench,
        poll_interval_s=3600,
        debounce_s=0,
    )
    _patch_ollama(monkeypatch, scheduler, ["m1"])

    await scheduler.start()
    await asyncio.wait_for(bench.started.wait(), timeout=2.0)
    # Bench has started but not finished — call stop.
    t0 = asyncio.get_event_loop().time()
    await scheduler.stop()
    t1 = asyncio.get_event_loop().time()
    # stop() should not have waited the full 10s.
    assert t1 - t0 < 5.0
    # The finished event should NOT be set (bench was cancelled).
    assert not bench.finished.is_set()


@pytest.mark.asyncio
async def test_stop_is_idempotent(tmp_path, monkeypatch):
    matrix = _matrix(tmp_path)
    bench = _RecordingBench()
    scheduler = BenchScheduler(
        score_matrix=matrix,
        ollama_base_url="http://localhost:11434",
        bench_callback=bench,
        poll_interval_s=3600,
    )
    _patch_ollama(monkeypatch, scheduler, [])

    await scheduler.start()
    await scheduler.stop()
    # Second call must not raise.
    await scheduler.stop()


@pytest.mark.asyncio
async def test_start_called_twice_warns_does_not_double_spawn(
    tmp_path, monkeypatch, caplog,
):
    matrix = _matrix(tmp_path)
    bench = _RecordingBench()
    scheduler = BenchScheduler(
        score_matrix=matrix,
        ollama_base_url="http://localhost:11434",
        bench_callback=bench,
        poll_interval_s=3600,
    )
    _patch_ollama(monkeypatch, scheduler, [])

    await scheduler.start()
    with caplog.at_level(logging.WARNING):
        await scheduler.start()
    await scheduler.stop()

    assert any("already_running" in r.getMessage() for r in caplog.records)


# ---- Bench errors don't crash scheduler -----------------------------------


@pytest.mark.asyncio
async def test_bench_callback_raising_does_not_crash_scheduler(
    tmp_path, monkeypatch,
):
    matrix = _matrix(tmp_path)

    raised_event = asyncio.Event()

    async def crashing_bench(models):
        raised_event.set()
        raise RuntimeError("simulated bench crash")

    scheduler = BenchScheduler(
        score_matrix=matrix,
        ollama_base_url="http://localhost:11434",
        bench_callback=crashing_bench,
        poll_interval_s=3600,
        debounce_s=0,
    )
    _patch_ollama(monkeypatch, scheduler, ["m1"])

    await scheduler.start()
    await asyncio.wait_for(raised_event.wait(), timeout=2.0)
    # Give the task a tick to absorb the exception.
    await asyncio.sleep(0.1)
    # Watch loop must still be alive (no exception leaked through).
    assert scheduler._watch_task is not None
    assert not scheduler._watch_task.done()
    await scheduler.stop()
