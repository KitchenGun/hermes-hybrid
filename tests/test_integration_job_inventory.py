"""Tests for JobInventory (src/integration/job_inventory.py).

Phase 8 (2026-05-06) 후 inventory 책임 축소: profile/job/skill 폐기 →
agent-only. 17 sub-agent 노출만 검증.

레거시 stub (profiles() / jobs() / skills() / find_job / skills_for) 은
호환을 위해 남아있으며 항상 빈 결과를 반환.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.integration import JobInventory


# ---- legacy stubs (Phase 8 후 항상 빈) -------------------------------


def test_profiles_jobs_skills_are_empty_after_phase_8(tmp_path):
    inv = JobInventory(tmp_path)
    assert inv.profiles() == {}
    assert inv.jobs() == []
    assert inv.skills() == []
    assert inv.find_job("anything") is None
    assert inv.skills_for("anything") == []


def test_summary_reports_zero_profiles_and_jobs(tmp_path):
    inv = JobInventory(tmp_path)
    summary = inv.summary()
    assert summary["profile_count"] == 0
    assert summary["job_count"] == 0
    assert summary["skill_count"] == 0
    # agent_count is whatever the real agents/ tree has — see below.
    assert summary["agent_count"] >= 0


# ---- Phase 7+: agents lookup -----------------------------------------


def test_real_repo_inventory_exposes_seventeen_agents():
    """JobInventory.agents() must surface the global ``agents/`` tree
    so the master can resolve @coder etc. without a separate import."""
    repo = Path(__file__).resolve().parent.parent
    agents_root = repo / "agents"
    if not agents_root.exists():
        pytest.skip("running outside repo")
    inv = JobInventory(repo_root=repo)
    agents = inv.agents()
    assert len(agents) == 17
    handles = sorted(a.handle for a in agents)
    assert "@coder" in handles
    assert "@reviewer" in handles
    assert "@finder" in handles


def test_agent_by_handle_round_trips():
    repo = Path(__file__).resolve().parent.parent
    agents_root = repo / "agents"
    if not agents_root.exists():
        pytest.skip("running outside repo")
    inv = JobInventory(repo_root=repo)
    coder = inv.agent_by_handle("@coder")
    assert coder is not None
    assert coder.category == "implementation"


def test_summary_includes_agent_counts():
    repo = Path(__file__).resolve().parent.parent
    agents_root = repo / "agents"
    if not agents_root.exists():
        pytest.skip("running outside repo")
    inv = JobInventory(repo_root=repo)
    summary = inv.summary()
    assert summary["agent_count"] == 17
    abc = summary["agents_by_category"]
    assert abc["implementation"] == 4
    assert abc["quality"] == 4
    assert abc["research"] == 3
