"""Tests for ABReportJob (src/jobs/ab_report.py).

Phase 21 (2026-05-07).

Locks down:
  * Welch's t-test sign + monotonicity (treatment higher → t > 0)
  * verdict label rules ("no_signal" / "treatment_better" / "control_better")
  * Report file is created and contains arm sample sizes
  * experiment_name filter excludes other experiments' rows
  * Empty / single-arm input does not crash
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core.experience_logger import ExperienceLogger, ExperienceRecord
from src.jobs.ab_report import ABReportJob, welch_t_test


def _write_records(
    log_root: Path,
    rows: list[dict],
) -> ExperienceLogger:
    """Write rows directly into the date-sharded JSONL so query() picks them up."""
    log_root.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()
    path = log_root / f"{today}.jsonl"
    lines: list[str] = []
    for r in rows:
        rec = ExperienceRecord(**r)
        lines.append(rec.model_dump_json(exclude_defaults=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ExperienceLogger(log_root, enabled=True)


def _row(arm: str, score: float, *, ts: str | None = None, name: str = "memory_inject") -> dict:
    return {
        "ts": ts or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "task_id": f"t-{arm}-{score:.2f}-{ts or 'now'}",
        "session_id": "s",
        "user_id": "u",
        "experiment_arm": arm,
        "experiment_name": name,
        "self_score": score,
        "latency_ms": 100,
    }


def test_welch_t_returns_positive_when_first_sample_is_higher():
    s_high = [0.9, 0.85, 0.92, 0.88, 0.91]
    s_low = [0.5, 0.45, 0.55, 0.52, 0.48]
    t, df, p = welch_t_test(s_high, s_low)
    assert t > 5.0, f"big difference should yield large t, got {t:.2f}"
    assert p < 0.05


def test_welch_t_returns_negative_when_first_sample_is_lower():
    s_low = [0.4, 0.42, 0.38, 0.41, 0.39]
    s_high = [0.8, 0.82, 0.78, 0.81, 0.79]
    t, _, _ = welch_t_test(s_low, s_high)
    assert t < 0.0


def test_welch_t_returns_zero_when_samples_identical():
    s = [0.7, 0.7, 0.7, 0.7, 0.7]
    t, _, _ = welch_t_test(s, s)
    assert t == 0.0


def test_welch_t_handles_small_samples():
    """n < 2 must return safe zero/neutral instead of crashing."""
    t, df, p = welch_t_test([], [])
    assert t == 0.0 and p == 1.0
    t2, _, p2 = welch_t_test([0.5], [0.6])
    assert t2 == 0.0 and p2 == 1.0


def test_report_creates_markdown_file_with_sample_sizes(tmp_path):
    log_root = tmp_path / "exp"
    rows = [
        _row("control", 0.5),
        _row("control", 0.55),
        _row("treatment", 0.8),
        _row("treatment", 0.85),
        _row("treatment_no_hits", 0.5),
    ]
    logger = _write_records(log_root, rows)

    out_dir = tmp_path / "ab"
    job = ABReportJob(logger, out_dir, window_days=1)
    result = job.run()

    assert result.ok
    assert result.output_path is not None
    assert result.output_path.exists()
    md = result.output_path.read_text(encoding="utf-8")
    assert "control: 2" in md
    assert "treatment: 2" in md
    assert "treatment_no_hits: 1" in md


def test_report_metrics_carry_sample_counts(tmp_path):
    log_root = tmp_path / "exp"
    rows = [
        _row("control", 0.4),
        _row("control", 0.4),
        _row("control", 0.4),
        _row("treatment", 0.9),
        _row("treatment", 0.9),
        _row("treatment", 0.9),
    ]
    logger = _write_records(log_root, rows)

    out_dir = tmp_path / "ab"
    job = ABReportJob(logger, out_dir, window_days=1)
    result = job.run()

    assert result.metrics["control_n"] == 3
    assert result.metrics["treatment_n"] == 3
    assert result.metrics["score_t"] > 0  # treatment higher
    assert result.metrics["verdict"] == "treatment_better"


def test_experiment_name_filter_isolates_concurrent_experiments(tmp_path):
    log_root = tmp_path / "exp"
    rows = [
        _row("control", 0.4, name="memory_inject"),
        _row("treatment", 0.9, name="memory_inject"),
        _row("control", 0.9, name="other_experiment"),     # noise
        _row("treatment", 0.4, name="other_experiment"),   # noise
    ]
    logger = _write_records(log_root, rows)

    out_dir = tmp_path / "ab"
    job = ABReportJob(
        logger, out_dir, window_days=1, experiment_name="memory_inject",
    )
    result = job.run()

    assert result.metrics["control_n"] == 1
    assert result.metrics["treatment_n"] == 1


def test_empty_log_does_not_crash(tmp_path):
    log_root = tmp_path / "exp"
    log_root.mkdir(parents=True)
    logger = ExperienceLogger(log_root, enabled=True)

    out_dir = tmp_path / "ab"
    job = ABReportJob(logger, out_dir, window_days=1)
    result = job.run()

    assert result.ok
    assert result.metrics["control_n"] == 0
    assert result.metrics["treatment_n"] == 0
    assert result.metrics["verdict"] == "no_signal"
