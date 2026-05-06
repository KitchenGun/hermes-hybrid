"""Tests for ExperienceLogger — JSONL append + privacy-preserving projection.

The logger is the first brick of the growth loop, so the contract is
narrow but strict:
  * disabled → no file, no exception
  * enabled  → one line per call, valid JSON, parseable back to the
               same record
  * privacy  → user_message and final_response are never written verbatim
  * read     → query() filters by [since, until) and profile, skips
               malformed lines silently
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.core import ExperienceLogger, ExperienceRecord
from src.state import TaskState


def _make_task(
    *,
    user_message: str = "안녕 오늘 날씨 어때",
    user_id: str = "12345",
    profile: str | None = "calendar_ops",
    forced_profile: str | None = None,
    final_response: str = "오늘은 맑음 ☀️",
    status: str = "succeeded",
) -> TaskState:
    """Minimal TaskState shaped like a real run that reached _log_task_end."""
    task = TaskState(
        session_id="sess-1",
        user_id=user_id,
        user_message=user_message,
        forced_profile=forced_profile,
    )
    task.job_profile_id = profile
    task.status = status
    task.final_response = final_response
    return task


def test_disabled_logger_writes_nothing(tmp_path: Path):
    logger = ExperienceLogger(tmp_path, enabled=False)
    task = _make_task()
    result = logger.append(task, handled_by="rule", latency_ms=42)
    assert result is None
    assert list(tmp_path.iterdir()) == []


def test_enabled_logger_writes_one_jsonl_line(tmp_path: Path):
    logger = ExperienceLogger(tmp_path, enabled=True)
    task = _make_task()
    record = logger.append(task, handled_by="local", latency_ms=42)

    assert record is not None
    assert record.handled_by == "local"
    assert record.latency_ms == 42
    assert record.profile == "calendar_ops"
    assert record.outcome == "succeeded"

    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].suffix == ".jsonl"

    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["handled_by"] == "local"
    assert parsed["task_id"] == task.task_id


def test_two_appends_same_day_share_one_file(tmp_path: Path):
    logger = ExperienceLogger(tmp_path, enabled=True)
    logger.append(_make_task(user_message="첫번째"), handled_by="local", latency_ms=10)
    logger.append(_make_task(user_message="두번째"), handled_by="cloud", latency_ms=20)

    files = list(tmp_path.iterdir())
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_privacy_user_message_is_hashed_not_stored(tmp_path: Path):
    logger = ExperienceLogger(tmp_path, enabled=True)
    secret = "비밀스러운 일기 내용 — 절대 저장되면 안 됨"
    task = _make_task(user_message=secret, final_response="응답도 비밀")
    logger.append(task, handled_by="hermes", latency_ms=100)

    raw = list(tmp_path.glob("*.jsonl"))[0].read_text(encoding="utf-8")
    assert secret not in raw
    assert "응답도 비밀" not in raw

    parsed = json.loads(raw.strip())
    assert parsed["input_text_length"] == len(secret)
    assert len(parsed["input_text_hash"]) == 16  # 16 hex chars
    assert parsed["response_length"] == len("응답도 비밀")


def test_failed_task_outcome_is_failed(tmp_path: Path):
    logger = ExperienceLogger(tmp_path, enabled=True)
    task = _make_task(status="failed")
    record = logger.append(task, handled_by="hermes-auth", latency_ms=200)

    assert record.outcome == "failed"
    assert record.status == "failed"


def test_succeeded_but_degraded_outcome_is_degraded(tmp_path: Path):
    logger = ExperienceLogger(tmp_path, enabled=True)
    task = _make_task(status="succeeded")
    task.degraded = True
    record = logger.append(task, handled_by="local", latency_ms=10)

    assert record.outcome == "degraded"
    assert record.degraded is True


def test_query_returns_records_in_time_window(tmp_path: Path):
    logger = ExperienceLogger(tmp_path, enabled=True)
    logger.append(_make_task(user_message="a"), handled_by="local", latency_ms=1)
    logger.append(_make_task(user_message="b"), handled_by="cloud", latency_ms=2)

    now = datetime.now(timezone.utc)
    records = list(
        logger.query(since=now - timedelta(hours=1), until=now + timedelta(hours=1))
    )
    assert len(records) == 2
    handled = sorted(r.handled_by for r in records)
    assert handled == ["cloud", "local"]


def test_query_filters_by_profile(tmp_path: Path):
    logger = ExperienceLogger(tmp_path, enabled=True)
    logger.append(
        _make_task(profile="calendar_ops"), handled_by="local", latency_ms=1
    )
    logger.append(_make_task(profile="kk_job"), handled_by="cloud", latency_ms=2)

    now = datetime.now(timezone.utc)
    only_kk = list(
        logger.query(
            since=now - timedelta(hours=1),
            until=now + timedelta(hours=1),
            profile="kk_job",
        )
    )
    assert len(only_kk) == 1
    assert only_kk[0].profile == "kk_job"


def test_query_skips_malformed_lines(tmp_path: Path):
    logger = ExperienceLogger(tmp_path, enabled=True)
    logger.append(_make_task(user_message="good"), handled_by="local", latency_ms=1)

    # Inject garbage between two valid lines — query must skip it without
    # raising.
    file_ = list(tmp_path.glob("*.jsonl"))[0]
    with file_.open("a", encoding="utf-8") as f:
        f.write("not json at all\n")
        f.write('{"partial": ')
        f.write("\n")

    logger.append(_make_task(user_message="good2"), handled_by="cloud", latency_ms=2)

    now = datetime.now(timezone.utc)
    records = list(
        logger.query(since=now - timedelta(hours=1), until=now + timedelta(hours=1))
    )
    # Two valid records, malformed lines silently dropped
    assert len(records) == 2


def test_record_includes_routing_and_retry_metadata(tmp_path: Path):
    logger = ExperienceLogger(tmp_path, enabled=True)
    task = _make_task()
    task.route = "cloud"
    task.current_tier = "C1"
    task.retry_count = 2
    task.tier_up_retries = 1
    task.cloud_call_count = 1
    task.cloud_model_used = ["claude-haiku"]

    record = logger.append(task, handled_by="claude-max", latency_ms=300)

    assert record.route == "cloud"
    assert record.tier == "C1"
    assert record.retries == 2
    assert record.tier_ups == 1
    assert record.cloud_calls == 1
    assert record.cloud_models == ["claude-haiku"]


def test_record_includes_phase_1_5_routing_fields(tmp_path: Path):
    """Phase 1.5: ExperienceRecord must carry job_id / trigger_type /
    v2 classification / skill_ids / model provenance / memory inject count."""
    logger = ExperienceLogger(tmp_path, enabled=True)
    task = _make_task()
    task.job_id = "morning_briefing"
    task.job_category = "read"
    task.trigger_type = "cron"
    task.trigger_source = "0 8 * * *"
    task.v2_job_type = "summarize"
    task.v2_classification_method = "keyword"
    task.skill_ids = ["calendar_ops/messaging/discord_notify"]
    task.slash_skill = None
    task.model_provider = "ollama"
    task.model_name = "qwen2.5:14b-instruct"
    task.memory_inject_count = 2

    rec = logger.append(task, handled_by="local", latency_ms=100)

    assert rec.job_id == "morning_briefing"
    assert rec.job_category == "read"
    assert rec.trigger_type == "cron"
    assert rec.trigger_source == "0 8 * * *"
    assert rec.v2_job_type == "summarize"
    assert rec.v2_classification_method == "keyword"
    assert rec.skill_ids == ["calendar_ops/messaging/discord_notify"]
    assert rec.slash_skill is None
    assert rec.model_provider == "ollama"
    assert rec.model_name == "qwen2.5:14b-instruct"
    assert rec.memory_inject_count == 2


def test_record_phase_1_5_defaults_when_unstamped(tmp_path: Path):
    """Tasks that don't go through a stamping branch (e.g. legacy router)
    keep defaults — None for nullable fields, ``discord_message`` for
    trigger_type, empty list for skill_ids."""
    logger = ExperienceLogger(tmp_path, enabled=True)
    task = _make_task()
    rec = logger.append(task, handled_by="local", latency_ms=10)

    assert rec.job_id is None
    assert rec.job_category is None
    assert rec.trigger_type == "discord_message"
    assert rec.v2_job_type is None
    assert rec.skill_ids == []
    assert rec.slash_skill is None
    assert rec.memory_inject_count == 0


def test_root_directory_is_created_on_first_write(tmp_path: Path):
    nested = tmp_path / "nested" / "dir" / "experience"
    assert not nested.exists()
    logger = ExperienceLogger(nested, enabled=True)
    logger.append(_make_task(), handled_by="local", latency_ms=1)
    assert nested.exists()
    assert len(list(nested.glob("*.jsonl"))) == 1
