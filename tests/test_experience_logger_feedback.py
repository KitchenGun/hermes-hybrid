"""Tests for ExperienceLogger.append_feedback + feedback_counts_by_handle
(Phase 20, 2026-05-07).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core import ExperienceLogger, ExperienceRecord


def _seed_main(log: ExperienceLogger, *, task_id: str, handles: list[str]) -> None:
    rec = ExperienceRecord(
        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        task_id=task_id,
        session_id="s",
        user_id="u",
        agent_handles=handles,
    )
    log._write_line(rec)


def test_append_feedback_writes_sidecar(tmp_path: Path):
    log = ExperienceLogger(tmp_path, enabled=True)
    ok = log.append_feedback(
        "task-1",
        feedback="negative",
        feedback_text="틀려요",
        bot_message_id=42,
    )
    assert ok is True
    sidecar = tmp_path / "feedback.jsonl"
    assert sidecar.exists()
    line = sidecar.read_text(encoding="utf-8").strip()
    parsed = json.loads(line)
    assert parsed["task_id"] == "task-1"
    assert parsed["feedback"] == "negative"
    assert parsed["feedback_text"] == "틀려요"
    assert parsed["bot_message_id"] == 42


def test_append_feedback_disabled_logger_returns_false(tmp_path: Path):
    log = ExperienceLogger(tmp_path, enabled=False)
    assert log.append_feedback("t", feedback="positive") is False
    assert not (tmp_path / "feedback.jsonl").exists()


def test_append_feedback_truncates_long_text(tmp_path: Path):
    log = ExperienceLogger(tmp_path, enabled=True)
    long_text = "x" * 500
    log.append_feedback("t", feedback="negative", feedback_text=long_text)
    line = (tmp_path / "feedback.jsonl").read_text(encoding="utf-8").strip()
    parsed = json.loads(line)
    assert len(parsed["feedback_text"]) == 160


def test_feedback_counts_by_handle_aggregates_correctly(tmp_path: Path):
    log = ExperienceLogger(tmp_path, enabled=True)
    _seed_main(log, task_id="t1", handles=["@coder"])
    _seed_main(log, task_id="t2", handles=["@coder", "@reviewer"])
    _seed_main(log, task_id="t3", handles=["@reviewer"])

    log.append_feedback("t1", feedback="negative")
    log.append_feedback("t2", feedback="negative")
    log.append_feedback("t3", feedback="positive")

    now = datetime.now(timezone.utc)
    counts = log.feedback_counts_by_handle(
        now - timedelta(hours=1), now + timedelta(hours=1),
    )
    assert counts["@coder"]["negative"] == 2
    assert counts["@coder"]["positive"] == 0
    assert counts["@reviewer"]["negative"] == 1
    assert counts["@reviewer"]["positive"] == 1


def test_feedback_counts_returns_empty_when_no_feedback_file(tmp_path: Path):
    log = ExperienceLogger(tmp_path, enabled=True)
    _seed_main(log, task_id="t1", handles=["@coder"])
    now = datetime.now(timezone.utc)
    assert log.feedback_counts_by_handle(
        now - timedelta(hours=1), now + timedelta(hours=1),
    ) == {}


def test_feedback_counts_window_filter(tmp_path: Path):
    """time-window 밖의 feedback 행은 카운트되지 않음."""
    log = ExperienceLogger(tmp_path, enabled=True)
    _seed_main(log, task_id="t1", handles=["@coder"])
    log.append_feedback("t1", feedback="negative")

    # 2 days in the future window — should be empty.
    future = datetime.now(timezone.utc) + timedelta(days=2)
    counts = log.feedback_counts_by_handle(
        future, future + timedelta(hours=1),
    )
    assert counts == {}
