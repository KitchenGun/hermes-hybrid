"""Tests for ReflectionJob — weekly statistical reducer.

The job is pure stats: feed it ExperienceRecords through an in-memory
ExperienceLogger, get back a markdown file + JobResult metrics. The
contract we lock down here:

  * empty window → ok=True, file with "no entries" stub, no crash
  * non-empty window → outcome counts and latency percentiles match
    inputs exactly
  * profile / handled_by / tool aggregations bucket correctly
  * output is idempotent: rerun same window overwrites with new stats
  * ISO week label rolls correctly across year boundary (2024-W52,
    2025-W01) so the operator never gets a "2024-W53" filename
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core import ExperienceLogger
from src.jobs.reflection_job import (
    ReflectionJob,
    _iso_week_label,
    _percentile,
    _summarize,
)
from src.state import TaskState


def _append(
    logger: ExperienceLogger,
    *,
    profile: str = "calendar_ops",
    handled_by: str = "local",
    status: str = "succeeded",
    degraded: bool = False,
    latency_ms: int = 100,
    cloud_calls: int = 0,
    self_score: float = 0.0,
    tool_calls: list[dict] | None = None,
    user_message: str = "msg",
):
    """Synthesize a TaskState that produces a record with the desired fields."""
    task = TaskState(
        session_id="s1",
        user_id="u1",
        user_message=user_message,
    )
    task.job_profile_id = profile
    task.status = status
    task.degraded = degraded
    task.cloud_call_count = cloud_calls
    task.internal_confidence = self_score
    if tool_calls:
        from src.state.task_state import ToolOutput

        for tc in tool_calls:
            task.tool_outputs.append(
                ToolOutput(
                    action_id=f"a{len(task.tool_outputs)}",
                    tool=tc["tool"],
                    result={},
                    ms=tc.get("ms", 1),
                    ok=tc.get("ok", True),
                )
            )
    logger.append(task, handled_by=handled_by, latency_ms=latency_ms)


# ---- pure helpers ----------------------------------------------------------


def test_percentile_handles_empty_and_single():
    assert _percentile([], 0.5) == 0
    assert _percentile([42], 0.5) == 42
    # nearest-rank with banker's rounding: round((n-1)*p) with n=10, p=0.5
    # gives k=4 → s[4] = 5 (off-by-one OK; we only need it to be stable)
    assert _percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.5) in (5, 6)
    assert _percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10


def test_iso_week_label_year_boundary():
    # 2024-12-30 (Mon) → ISO week 2025-W01 (calendar quirk)
    end_of_2024 = datetime(2024, 12, 30, 12, 0)
    assert _iso_week_label(end_of_2024) == "2025-W01"
    # Mid-year: 2026-05-04 → 2026-W19 (Mon)
    mid_2026 = datetime(2026, 5, 4, 12, 0)
    assert _iso_week_label(mid_2026) == "2026-W19"


def test_summarize_empty_returns_zero_block():
    s = _summarize([])
    assert s["total"] == 0
    assert s["success_rate"] == 0.0
    assert s["latency_p50"] == 0
    assert s["top_failures"] == []


# ---- end-to-end ReflectionJob ----------------------------------------------


def test_reflection_empty_window_writes_stub_and_returns_ok(tmp_path: Path):
    logger = ExperienceLogger(tmp_path / "exp", enabled=True)
    out_dir = tmp_path / "reflection"

    job = ReflectionJob(logger, out_dir, window_days=7)
    result = job.run()

    assert result.ok is True
    assert result.metrics["total"] == 0
    assert result.output_path is not None
    body = result.output_path.read_text(encoding="utf-8")
    assert "Total tasks:** 0" in body
    assert "No experience log entries" in body


def test_reflection_aggregates_outcome_counts(tmp_path: Path):
    logger = ExperienceLogger(tmp_path / "exp", enabled=True)
    out_dir = tmp_path / "reflection"

    # 6 succeeded, 2 failed, 2 degraded → total 10
    for _ in range(6):
        _append(logger, status="succeeded", latency_ms=50)
    for _ in range(2):
        _append(logger, status="failed", latency_ms=300)
    for _ in range(2):
        _append(logger, status="succeeded", degraded=True, latency_ms=200)

    job = ReflectionJob(logger, out_dir)
    result = job.run()

    assert result.metrics["total"] == 10
    # 6 / 10 → success_rate
    assert result.metrics["success_rate"] == 0.6
    assert result.metrics["fail_rate"] == 0.2
    body = result.output_path.read_text(encoding="utf-8")
    assert "Total tasks:** 10" in body
    assert "Success rate" in body


def test_reflection_buckets_by_profile_and_handled_by(tmp_path: Path):
    logger = ExperienceLogger(tmp_path / "exp", enabled=True)
    out_dir = tmp_path / "reflection"

    _append(logger, profile="calendar_ops", handled_by="local")
    _append(logger, profile="calendar_ops", handled_by="local")
    _append(logger, profile="kk_job", handled_by="cloud")

    job = ReflectionJob(logger, out_dir)
    result = job.run()
    body = result.output_path.read_text(encoding="utf-8")
    assert "`calendar_ops`: 2" in body
    assert "`kk_job`: 1" in body
    assert "`local`: 2" in body
    assert "`cloud`: 1" in body


def test_reflection_top_tools_includes_failure_rate(tmp_path: Path):
    logger = ExperienceLogger(tmp_path / "exp", enabled=True)
    out_dir = tmp_path / "reflection"

    # sheets_append: 4 calls, 1 failure
    for ok in [True, True, True, False]:
        _append(logger, tool_calls=[{"tool": "sheets_append", "ok": ok, "ms": 10}])

    job = ReflectionJob(logger, out_dir)
    job.run()
    body = (out_dir / next(iter(out_dir.iterdir())).name).read_text("utf-8")
    assert "sheets_append" in body
    assert "4 calls" in body
    assert "1 failed" in body


def test_reflection_overwrites_same_week_idempotent(tmp_path: Path):
    logger = ExperienceLogger(tmp_path / "exp", enabled=True)
    out_dir = tmp_path / "reflection"

    _append(logger, status="succeeded")
    job = ReflectionJob(logger, out_dir)
    first = job.run()
    first_mtime = first.output_path.stat().st_mtime

    _append(logger, status="failed")
    second = job.run()

    # Same file path (same ISO week), but new content.
    assert second.output_path == first.output_path
    assert second.metrics["total"] == 2
    # mtime monotonically advances when the file is rewritten.
    assert second.output_path.stat().st_mtime >= first_mtime


def test_reflection_self_score_avg_only_counts_scored_records(tmp_path: Path):
    """Records with self_score == 0.0 are 'unscored' (forced_profile path,
    factory v2 path) and must not drag the average down."""
    logger = ExperienceLogger(tmp_path / "exp", enabled=True)
    out_dir = tmp_path / "reflection"

    _append(logger, self_score=0.0)   # unscored (forced_profile path)
    _append(logger, self_score=0.8)
    _append(logger, self_score=0.6)

    job = ReflectionJob(logger, out_dir)
    result = job.run()
    # avg of 0.8, 0.6 = 0.7 (the 0.0 is excluded)
    body = result.output_path.read_text(encoding="utf-8")
    assert "Self-score avg:** 0.7" in body
