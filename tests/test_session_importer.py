"""Tests for hermes session JSON → ExperienceLog importer (Phase 2).

Locks down:
  * session_to_record produces a valid ExperienceRecord from a synthetic
    session JSON (modelUsage, messages with tool_calls, etc.)
  * import_sessions is idempotent — re-running doesn't duplicate
  * sessions sub-path that contains ``profiles/{profile}`` extracts
    profile_id correctly
  * malformed JSON is skipped (errors counter increments, no crash)
  * outcome inference from tool error markers
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.core import ExperienceLogger, import_sessions, session_to_record


_SAMPLE_SESSION = {
    "session_id": "abc-123",
    "ended_at": "2026-05-06T05:00:00Z",
    "provider": "ollama",
    "model": "qwen2.5:14b-instruct",
    "modelUsage": [
        {
            "model": "qwen2.5:14b-instruct",
            "turns": 2,
            "prompt_tokens": 312,
            "completion_tokens": 89,
            "cost_usd": 0.0,
        }
    ],
    "turns_used": 2,
    "skills_invoked": ["google_calendar"],
    "total_cost_usd": 0.0,
    "messages": [
        {"role": "user", "content": "오늘 일정 알려줘"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "t1", "function": {"name": "list_events", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "name": "list_events", "content": "[]"},
        {"role": "assistant", "content": "오늘은 일정이 없어요."},
    ],
}


def test_session_to_record_extracts_core_fields():
    rec = session_to_record(
        _SAMPLE_SESSION,
        profile_id="calendar_ops",
        job_id="morning_briefing",
        trigger_type="cron",
        trigger_source="0 8 * * *",
    )
    assert rec.task_id == "abc-123"
    assert rec.session_id == "abc-123"
    assert rec.profile == "calendar_ops"
    assert rec.job_id == "morning_briefing"
    assert rec.trigger_type == "cron"
    assert rec.trigger_source == "0 8 * * *"
    assert rec.model_provider == "ollama"
    assert rec.model_name == "qwen2.5:14b-instruct"
    assert rec.prompt_tokens == 312
    assert rec.completion_tokens == 89
    assert rec.hermes_turns == 2
    assert rec.skill_ids == ["google_calendar"]
    assert rec.outcome == "succeeded"
    assert rec.handled_by == "hermes-session:cron"


def test_session_to_record_privacy_no_raw_text():
    """user / assistant content must NOT be stored verbatim."""
    rec = session_to_record(
        _SAMPLE_SESSION,
        profile_id="calendar_ops",
        job_id=None,
        trigger_type="cron",
        trigger_source=None,
    )
    blob = rec.model_dump_json()
    assert "오늘 일정 알려줘" not in blob
    assert "오늘은 일정이 없어요" not in blob
    assert rec.input_text_length == len("오늘 일정 알려줘")
    assert rec.response_length == len("오늘은 일정이 없어요.")


def test_session_to_record_failed_when_empty_response():
    session = {
        "session_id": "fail-1",
        "messages": [{"role": "user", "content": "hi"}],
    }
    rec = session_to_record(
        session, profile_id=None, job_id=None,
        trigger_type="cron", trigger_source=None,
    )
    assert rec.outcome == "failed"
    assert rec.status == "failed"


def test_session_to_record_degraded_when_tool_error():
    session = {
        "session_id": "deg-1",
        "provider": "ollama",
        "messages": [
            {"role": "user", "content": "fetch x"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "t1", "function": {"name": "fetch", "arguments": "{}"}}],
            },
            {"role": "tool", "name": "fetch", "content": "Error: connection refused"},
            {"role": "assistant", "content": "I couldn't fetch it."},
        ],
    }
    rec = session_to_record(
        session, profile_id=None, job_id=None,
        trigger_type="cron", trigger_source=None,
    )
    assert rec.outcome == "degraded"
    assert any(not tc.get("ok", True) for tc in rec.tool_calls)


def test_import_sessions_writes_jsonl(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "session_abc-123.json").write_text(
        json.dumps(_SAMPLE_SESSION), encoding="utf-8"
    )

    log_root = tmp_path / "exp"
    logger = ExperienceLogger(log_root, enabled=True)

    metrics = import_sessions(
        sessions, logger, state_path=tmp_path / ".processed.json"
    )
    assert metrics["imported"] == 1
    assert metrics["skipped"] == 0
    assert metrics["errors"] == 0

    # JSONL written
    files = list(log_root.glob("*.jsonl"))
    assert len(files) == 1
    line = files[0].read_text(encoding="utf-8").strip()
    parsed = json.loads(line)
    assert parsed["task_id"] == "abc-123"


def test_import_sessions_dedups_on_rerun(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "session_abc-123.json").write_text(
        json.dumps(_SAMPLE_SESSION), encoding="utf-8"
    )
    log_root = tmp_path / "exp"
    state_path = tmp_path / ".processed.json"

    logger = ExperienceLogger(log_root, enabled=True)
    first = import_sessions(sessions, logger, state_path=state_path)
    second = import_sessions(sessions, logger, state_path=state_path)

    assert first["imported"] == 1
    assert second["imported"] == 0
    assert second["skipped"] == 1
    # Only one line in the JSONL
    files = list(log_root.glob("*.jsonl"))
    assert len(files) == 1
    assert len(files[0].read_text(encoding="utf-8").splitlines()) == 1


def test_import_sessions_extracts_profile_from_path(tmp_path: Path):
    nested = tmp_path / "profiles" / "kk_job" / "sessions"
    nested.mkdir(parents=True)
    (nested / "session_xyz.json").write_text(
        json.dumps(_SAMPLE_SESSION), encoding="utf-8"
    )
    log_root = tmp_path / "exp"
    logger = ExperienceLogger(log_root, enabled=True)
    import_sessions(nested, logger, state_path=tmp_path / ".processed.json")

    line = list(log_root.glob("*.jsonl"))[0].read_text("utf-8").strip()
    parsed = json.loads(line)
    assert parsed["profile"] == "kk_job"


def test_import_sessions_skips_malformed_files(tmp_path: Path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "session_good.json").write_text(
        json.dumps(_SAMPLE_SESSION), encoding="utf-8"
    )
    (sessions / "session_bad.json").write_text("{ not valid json", encoding="utf-8")

    log_root = tmp_path / "exp"
    logger = ExperienceLogger(log_root, enabled=True)
    metrics = import_sessions(
        sessions, logger, state_path=tmp_path / ".processed.json"
    )
    assert metrics["imported"] == 1
    assert metrics["errors"] == 1


def test_import_sessions_missing_dir_returns_zero(tmp_path: Path):
    logger = ExperienceLogger(tmp_path / "exp", enabled=True)
    metrics = import_sessions(
        tmp_path / "does_not_exist",
        logger,
        state_path=tmp_path / ".p.json",
    )
    assert metrics["imported"] == 0
    assert "reason" in metrics
