"""Tests for CuratorJob — handled_by / tool stat aggregation.

Locked-down behavior:
  * empty input → ok=True, JSON has zero handlers / tools, markdown
    has the "no records" stub
  * runs / successes / failures / failure_rate match inputs exactly
  * tool_calls are aggregated separately from handled_by buckets
  * JSON is atomic-write (no half-file from a crash mid-run)
  * the suggestion section flags failure_rate ≥ 30% with runs ≥ 5
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core import ExperienceLogger
from src.jobs.curator_job import (
    CuratorJob,
    aggregate_stats,
    render_summary_md,
)
from src.state import TaskState


def _append(
    logger: ExperienceLogger,
    *,
    handled_by: str,
    status: str = "succeeded",
    degraded: bool = False,
    tool_calls: list[dict] | None = None,
):
    task = TaskState(session_id="s", user_id="u1", user_message="m")
    task.status = status
    task.degraded = degraded
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
    logger.append(task, handled_by=handled_by, latency_ms=10)


# ---- aggregate_stats: pure function ---------------------------------------


def test_aggregate_empty_records_returns_empty_dicts():
    s = aggregate_stats([])
    assert s["by_handled_by"] == {}
    assert s["by_tool"] == {}


def test_aggregate_counts_runs_and_failures(tmp_path: Path):
    logger = ExperienceLogger(tmp_path, enabled=True)
    for _ in range(3):
        _append(logger, handled_by="skill:hybrid-memo", status="succeeded")
    _append(logger, handled_by="skill:hybrid-memo", status="failed")

    records = list(logger.query(since=datetime.now(timezone.utc) - timedelta(hours=1)))
    s = aggregate_stats(records)
    bucket = s["by_handled_by"]["skill:hybrid-memo"]
    assert bucket["runs"] == 4
    assert bucket["successes"] == 3
    assert bucket["failures"] == 1
    assert bucket["failure_rate"] == 0.25


def test_aggregate_separates_tools_from_handlers(tmp_path: Path):
    logger = ExperienceLogger(tmp_path, enabled=True)
    _append(
        logger,
        handled_by="skill:calendar",
        tool_calls=[
            {"tool": "google_calendar", "ok": True},
            {"tool": "google_calendar", "ok": False},
        ],
    )
    records = list(logger.query(since=datetime.now(timezone.utc) - timedelta(hours=1)))
    s = aggregate_stats(records)
    assert s["by_handled_by"]["skill:calendar"]["runs"] == 1
    cal = s["by_tool"]["google_calendar"]
    assert cal["calls"] == 2
    assert cal["ok"] == 1
    assert cal["failures"] == 1
    assert cal["failure_rate"] == 0.5


def test_aggregate_skips_empty_tool_names(tmp_path: Path):
    logger = ExperienceLogger(tmp_path, enabled=True)
    _append(
        logger,
        handled_by="local",
        tool_calls=[{"tool": "", "ok": True}],
    )
    records = list(logger.query(since=datetime.now(timezone.utc) - timedelta(hours=1)))
    s = aggregate_stats(records)
    assert s["by_tool"] == {}


# ---- end-to-end CuratorJob ------------------------------------------------


def test_curator_empty_window_writes_stub(tmp_path: Path):
    logger = ExperienceLogger(tmp_path / "exp", enabled=True)
    out = tmp_path / "curator"
    job = CuratorJob(logger, out, window_days=7)
    result = job.run()

    assert result.ok is True
    assert result.metrics["total"] == 0

    json_path = out / "handled_by_stats.json"
    assert json_path.exists()
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["total_records"] == 0
    assert payload["by_handled_by"] == {}

    md = result.output_path.read_text(encoding="utf-8")
    assert "No records" in md


def test_curator_writes_json_and_markdown(tmp_path: Path):
    logger = ExperienceLogger(tmp_path / "exp", enabled=True)
    out = tmp_path / "curator"

    for _ in range(8):
        _append(logger, handled_by="skill:hybrid-memo", status="succeeded")
    for _ in range(2):
        _append(logger, handled_by="skill:hybrid-memo", status="failed")
    _append(logger, handled_by="local", status="succeeded")

    job = CuratorJob(logger, out, window_days=7)
    result = job.run()
    assert result.metrics["handlers"] == 2

    payload = json.loads((out / "handled_by_stats.json").read_text("utf-8"))
    memo = payload["by_handled_by"]["skill:hybrid-memo"]
    assert memo["runs"] == 10
    assert memo["successes"] == 8
    assert memo["failures"] == 2
    assert memo["failure_rate"] == 0.2

    md = result.output_path.read_text(encoding="utf-8")
    assert "skill:hybrid-memo" in md
    assert "20.0%" in md or "20%" in md  # failure_rate display


def test_curator_flags_high_failure_rate_in_suggestions(tmp_path: Path):
    logger = ExperienceLogger(tmp_path / "exp", enabled=True)
    out = tmp_path / "curator"

    # 5 runs, 3 failures = 60% failure rate — should trigger suggestion
    for _ in range(2):
        _append(logger, handled_by="skill:flaky", status="succeeded")
    for _ in range(3):
        _append(logger, handled_by="skill:flaky", status="failed")

    job = CuratorJob(logger, out, window_days=7)
    result = job.run()
    md = result.output_path.read_text(encoding="utf-8")
    assert "skill:flaky" in md
    # The suggestion line is the only place failure_rate is mentioned
    # in the format `60% failure rate over 5 runs`.
    assert "review or consider deactivating" in md


def test_curator_no_suggestion_when_below_thresholds(tmp_path: Path):
    logger = ExperienceLogger(tmp_path / "exp", enabled=True)
    out = tmp_path / "curator"

    # Below run-count threshold: 4 runs, 4 failures still doesn't trigger
    # (we want at least 5 runs of evidence before flagging).
    for _ in range(4):
        _append(logger, handled_by="skill:scarce", status="failed")

    job = CuratorJob(logger, out, window_days=7)
    result = job.run()
    md = result.output_path.read_text(encoding="utf-8")
    assert "_No automatic flags this window._" in md


def test_render_summary_md_handles_empty_total():
    md = render_summary_md(
        generated_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
        window_days=30,
        total=0,
        stats={"by_handled_by": {}, "by_tool": {}},
    )
    assert "No records in window" in md
    assert "Curator stats" in md
