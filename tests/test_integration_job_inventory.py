"""Tests for JobInventory (src/integration/job_inventory.py).

We exercise both:
  * a synthetic profiles tree built in tmp_path (deterministic schema)
  * the real repo's profiles directory (sanity — current 6 profiles +
    27 jobs reflected)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.integration import JobInventory


_PROFILE_CONFIG_YAML = """\
agent:
  max_turns: 10

model:
  provider: custom
  model: qwen2.5:14b-instruct

skills:
  auto_load:
    - messaging/discord_notify

approvals:
  mode: manual

x-hermes-hybrid:
  tier_policy:
    prefer_tier: L2
    max_tier: C1
  budget:
    cap_usd_per_day: 0.20
"""

_CRON_JOB_YAML = """\
name: morning_briefing
category: read
description: 매일 아침 브리핑
trigger:
  type: cron
  schedule: "0 8 * * *"
  timezone: Asia/Seoul
tier:
  max: L2
  prefer: L2
budget:
  usd_per_run_cap: 0.00
skills:
  - google_calendar
delivery:
  channel: webhook
  target_env: DISCORD_BRIEFING_WEBHOOK_URL
safety:
  requires_confirmation: false
prompt: |
  역할: 캘린더 비서
  단계 1) 일정 가져오기
  단계 2) 포맷
  단계 3) 전송
"""

_ON_DEMAND_YAML = """\
name: add_event
category: write
description: 일정 추가
trigger:
  type: on_demand
  patterns: ["일정 추가"]
tier:
  max: C1
  prefer: L2
budget:
  usd_per_run_cap: 0.02
skills:
  - google_calendar
delivery:
  channel: webhook
safety:
  requires_confirmation: true
prompt: "일정 추가 prompt"
"""

_WATCHER_POLL_YAML = """\
name: new_posting_alert
category: read
description: 1시간마다 RSS 폴링
trigger:
  type: watcher
  interval_seconds: 3600
  source:
    type: rss_poll
    feeds: ["https://example.com/rss"]
tier:
  max: L2
  prefer: L2
budget:
  usd_per_run_cap: 0.00
skills: []
delivery:
  channel: webhook
prompt: ""
"""

_WATCHER_EVENT_YAML = """\
name: conflict_detector
category: watcher
description: 충돌 감지
trigger:
  type: watcher
  source: internal.calendar_write_completed
tier:
  max: L2
  prefer: L2
budget:
  usd_per_run_cap: 0.00
skills: []
delivery:
  channel: dm
prompt: ""
"""


def _build_synthetic_profiles(root: Path) -> None:
    """One profile (calendar_ops) with 4 jobs across all trigger types."""
    cal = root / "calendar_ops"
    (cal / "cron" / "read").mkdir(parents=True)
    (cal / "on_demand").mkdir(parents=True)
    (cal / "watchers").mkdir(parents=True)

    (cal / "config.yaml").write_text(_PROFILE_CONFIG_YAML, encoding="utf-8")
    (cal / "SOUL.md").write_text("# calendar_ops\n캘린더 비서.\n", encoding="utf-8")
    (cal / "cron" / "read" / "morning_briefing.yaml").write_text(
        _CRON_JOB_YAML, encoding="utf-8"
    )
    (cal / "on_demand" / "add_event.yaml").write_text(
        _ON_DEMAND_YAML, encoding="utf-8"
    )
    (cal / "watchers" / "new_posting_alert.yaml").write_text(
        _WATCHER_POLL_YAML, encoding="utf-8"
    )
    (cal / "watchers" / "conflict_detector.yaml").write_text(
        _WATCHER_EVENT_YAML, encoding="utf-8"
    )


def test_scan_synthetic_profile_yields_one_profile_and_four_jobs(tmp_path):
    _build_synthetic_profiles(tmp_path)
    inv = JobInventory(tmp_path)

    profiles = inv.profiles()
    assert list(profiles.keys()) == ["calendar_ops"]
    spec = profiles["calendar_ops"]
    assert spec.model_provider == "custom"
    assert spec.model_name == "qwen2.5:14b-instruct"
    assert spec.tier_prefer == "L2"
    assert spec.tier_max == "C1"
    assert spec.budget_cap_usd_per_day == pytest.approx(0.20)
    assert spec.approvals_mode == "manual"
    assert spec.auto_load_skills == ["messaging/discord_notify"]
    assert spec.has_cron and spec.has_on_demand and spec.has_watchers

    jobs = inv.jobs()
    job_ids = sorted(j.job_id for j in jobs)
    assert job_ids == [
        "add_event",
        "conflict_detector",
        "morning_briefing",
        "new_posting_alert",
    ]


def test_trigger_type_split_for_watchers(tmp_path):
    _build_synthetic_profiles(tmp_path)
    inv = JobInventory(tmp_path)
    triggers = {j.job_id: j.trigger_type for j in inv.jobs()}
    assert triggers["morning_briefing"] == "cron"
    assert triggers["add_event"] == "on_demand"
    assert triggers["new_posting_alert"] == "watcher_poll"
    assert triggers["conflict_detector"] == "watcher_event"


def test_jobs_filter_by_profile_and_trigger(tmp_path):
    _build_synthetic_profiles(tmp_path)
    inv = JobInventory(tmp_path)
    cron_only = inv.jobs(trigger_type="cron")
    assert [j.job_id for j in cron_only] == ["morning_briefing"]
    cal_only = inv.jobs(profile_id="calendar_ops")
    assert len(cal_only) == 4
    other = inv.jobs(profile_id="does_not_exist")
    assert other == []


def test_find_job_returns_match_or_none(tmp_path):
    _build_synthetic_profiles(tmp_path)
    inv = JobInventory(tmp_path)
    assert inv.find_job("morning_briefing") is not None
    assert inv.find_job("nope") is None


def test_requires_confirmation_carried_through(tmp_path):
    _build_synthetic_profiles(tmp_path)
    inv = JobInventory(tmp_path)
    add_event = inv.find_job("add_event")
    assert add_event is not None
    assert add_event.requires_confirmation is True
    morning = inv.find_job("morning_briefing")
    assert morning is not None
    assert morning.requires_confirmation is False


def test_summary_counts_match_synthetic_input(tmp_path):
    _build_synthetic_profiles(tmp_path)
    inv = JobInventory(tmp_path)
    summary = inv.summary()
    assert summary["profile_count"] == 1
    assert summary["job_count"] == 4
    assert summary["jobs_by_trigger"] == {
        "cron": 1,
        "on_demand": 1,
        "watcher_event": 1,
        "watcher_poll": 1,
    }


def test_real_repo_inventory_matches_documented_counts():
    """Sanity against the actual repo. If this fails, JOB_INVENTORY.md
    needs updating (or the scanner regressed)."""
    repo = Path(__file__).resolve().parent.parent
    profiles = repo / "profiles"
    if not profiles.exists():
        return  # skip if running outside the repo
    inv = JobInventory(profiles, repo_root=repo)
    profile_ids = sorted(inv.profiles().keys())
    assert profile_ids == [
        "advisor_ops",
        "calendar_ops",
        "installer_ops",
        "journal_ops",
        "kk_job",
        "mail_ops",
    ]
    summary = inv.summary()
    # Total jobs ≥ 26 (Phase 6 follow-up added installer_ops's
    # process_kanban_tasks → 27).
    assert summary["job_count"] >= 26
    assert summary["jobs_by_trigger"]["cron"] >= 10
    assert summary["jobs_by_trigger"]["on_demand"] >= 11
    assert summary["jobs_by_trigger"]["watcher_event"] >= 1
    assert summary["jobs_by_trigger"]["watcher_poll"] >= 1


def test_skills_for_profile_filters_correctly():
    """SkillLibrary integration — skills_for(profile_id) filters."""
    repo = Path(__file__).resolve().parent.parent
    profiles = repo / "profiles"
    if not profiles.exists():
        return
    inv = JobInventory(profiles, repo_root=repo)
    cal_skills = inv.skills_for("calendar_ops")
    assert len(cal_skills) >= 2
    assert all(s.profile == "calendar_ops" for s in cal_skills)


# ---- Phase 7: agents lookup --------------------------------------------


def test_real_repo_inventory_exposes_seventeen_agents():
    """JobInventory.agents() must surface the global ``agents/`` tree
    so the master can resolve @coder etc. without a separate import."""
    repo = Path(__file__).resolve().parent.parent
    profiles = repo / "profiles"
    if not profiles.exists():
        pytest.skip("running outside repo")
    inv = JobInventory(profiles, repo_root=repo)
    agents = inv.agents()
    assert len(agents) == 17
    handles = sorted(a.handle for a in agents)
    assert "@coder" in handles
    assert "@reviewer" in handles
    assert "@finder" in handles


def test_agent_by_handle_round_trips():
    repo = Path(__file__).resolve().parent.parent
    profiles = repo / "profiles"
    if not profiles.exists():
        pytest.skip("running outside repo")
    inv = JobInventory(profiles, repo_root=repo)
    coder = inv.agent_by_handle("@coder")
    assert coder is not None
    assert coder.category == "implementation"


def test_summary_includes_agent_counts():
    repo = Path(__file__).resolve().parent.parent
    profiles = repo / "profiles"
    if not profiles.exists():
        pytest.skip("running outside repo")
    inv = JobInventory(profiles, repo_root=repo)
    summary = inv.summary()
    assert summary["agent_count"] == 17
    abc = summary["agents_by_category"]
    assert abc["implementation"] == 4
    assert abc["quality"] == 4
    assert abc["research"] == 3
