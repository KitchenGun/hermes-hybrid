"""Phase 15 — SkillPromoter tests.

Locks down:
  * cluster_patterns: signature 별 grouping + min_evidence 컷
  * _already_covered: 등록 핸들들로만 구성 → True (skip)
  * weak_agent_audit: agent self_score 평균 < threshold + count >= min_evidence
  * _produce_skill_draft: master 호출 + draft 파일 생성 + auto_generated 필드
  * _produce_weak_draft: 약한 agent SKILL.md 보강 draft
  * _open_pr: gh CLI 없으면 None (graceful)
  * run_weekly: end-to-end (cluster → drafts → PR or graceful skip)
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from src.agents import AgentRegistry
from src.jobs.skill_promoter import (
    PatternCluster,
    SkillPromoter,
    SkillPromoterResult,
)


_REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class _Result:
    text: str = "draft skill md"
    model_name: str = "opus"
    input_tokens: int = 100
    output_tokens: int = 200
    duration_ms: int = 500
    session_id: str = "s1"
    total_cost_usd: float = 0.0


class _StubAdapter:
    def __init__(self, response: str = "draft", *, raises: Exception | None = None):
        self.calls: list[dict[str, Any]] = []
        self._response = response
        self._raises = raises

    async def run(self, *, prompt: str, history=None, model=None, timeout_ms=None):
        self.calls.append({"prompt": prompt})
        if self._raises is not None:
            raise self._raises
        return _Result(text=self._response)


def _seed_log(root: Path, entries: list[dict[str, Any]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    f = root / "2026-05-07.jsonl"
    f.write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n",
        encoding="utf-8",
    )


def _make_promoter(tmp_path: Path, **kwargs) -> SkillPromoter:
    base = dict(
        adapter=_StubAdapter(),
        agents=AgentRegistry(repo_root=_REPO_ROOT),
        experience_log_root=tmp_path / "exp",
        agents_root=_REPO_ROOT / "agents",
        draft_dir=tmp_path / "drafts",
        min_evidence=5,
        auto_pr=False,                        # tests don't actually push
        repo_root=_REPO_ROOT,
    )
    base.update(kwargs)
    return SkillPromoter(**base)


# ---- cluster_patterns ----------------------------------------------------


def test_cluster_returns_only_min_evidence_groups(tmp_path):
    rows = [
        # 6 of these — over min_evidence=5
        {
            "ts": f"2026-05-07T0{i}:00:00+00:00",
            "handled_by": "master:claude",
            "agent_handles": ["@coder"],
            "pipeline_id": None,
            "self_score": 0.7,
        }
        for i in range(6)
    ] + [
        # 2 of these — under min_evidence
        {
            "ts": f"2026-05-07T1{i}:00:00+00:00",
            "handled_by": "master:claude",
            "agent_handles": ["@finder"],
            "pipeline_id": None,
            "self_score": 0.3,
        }
        for i in range(2)
    ]
    _seed_log(tmp_path / "exp", rows)
    p = _make_promoter(tmp_path)
    until = datetime(2026, 5, 8, tzinfo=timezone.utc)
    since = datetime(2026, 5, 6, tzinfo=timezone.utc)

    clusters = list(p.cluster_patterns(since, until))
    assert len(clusters) == 1
    assert clusters[0].agent_handles == ("@coder",)
    assert clusters[0].evidence_count == 6


def test_cluster_avg_self_score(tmp_path):
    rows = [
        {
            "ts": "2026-05-07T0{}:00:00+00:00".format(i),
            "handled_by": "master:claude",
            "agent_handles": ["@coder"],
            "pipeline_id": None,
            "self_score": 0.5 if i < 3 else 0.9,
        }
        for i in range(6)
    ]
    _seed_log(tmp_path / "exp", rows)
    p = _make_promoter(tmp_path)
    since = datetime(2026, 5, 6, tzinfo=timezone.utc)
    until = datetime(2026, 5, 8, tzinfo=timezone.utc)

    cluster = next(iter(p.cluster_patterns(since, until)))
    # avg of [0.5, 0.5, 0.5, 0.9, 0.9, 0.9] = 0.7
    assert abs(cluster.avg_self_score - 0.7) < 0.01


# ---- _already_covered ----------------------------------------------------


def test_already_covered_when_all_handles_registered(tmp_path):
    p = _make_promoter(tmp_path)
    cluster = PatternCluster(
        handled_by="master:claude",
        agent_handles=("@coder", "@reviewer"),
        pipeline_id=None,
        evidence_count=10,
        avg_self_score=0.7,
    )
    assert p._already_covered(cluster) is True


def test_not_covered_when_handle_unknown(tmp_path):
    p = _make_promoter(tmp_path)
    cluster = PatternCluster(
        handled_by="master:claude",
        agent_handles=("@coder", "@nobody"),
        pipeline_id=None,
        evidence_count=10,
        avg_self_score=0.7,
    )
    assert p._already_covered(cluster) is False


# ---- weak_agent_audit ----------------------------------------------------


def test_weak_audit_finds_low_score_agents(tmp_path):
    rows = [
        # @debugger: 5 calls, avg score 0.3 (weak)
        {
            "ts": f"2026-05-07T0{i}:00:00+00:00",
            "handled_by": "master:claude",
            "agent_handles": ["@debugger"],
            "self_score": 0.3,
        }
        for i in range(5)
    ] + [
        # @coder: 5 calls, avg score 0.8 (good)
        {
            "ts": f"2026-05-07T1{i}:00:00+00:00",
            "handled_by": "master:claude",
            "agent_handles": ["@coder"],
            "self_score": 0.8,
        }
        for i in range(5)
    ]
    _seed_log(tmp_path / "exp", rows)
    p = _make_promoter(tmp_path, weak_score_threshold=0.4)
    since = datetime(2026, 5, 6, tzinfo=timezone.utc)
    until = datetime(2026, 5, 8, tzinfo=timezone.utc)

    weak = list(p.weak_agent_audit(since, until))
    handles = [h for h, _, _ in weak]
    assert "@debugger" in handles
    assert "@coder" not in handles


# ---- _produce_skill_draft ----------------------------------------------


@pytest.mark.asyncio
async def test_produce_skill_draft_creates_file(tmp_path):
    adapter = _StubAdapter(response="""---
name: notifier
agent_handle: "@notifier"
category: infrastructure
role: notification
description: 자주 사용된 알림 패턴
---

# @notifier — 알림""")

    p = _make_promoter(tmp_path, adapter=adapter)
    cluster = PatternCluster(
        handled_by="master:claude",
        agent_handles=("@coder",),
        pipeline_id=None,
        evidence_count=10,
        avg_self_score=0.7,
        sample_handler_token_keywords=("master", "claude"),
    )
    path = await p._produce_skill_draft(cluster)
    assert path is not None
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "@notifier" in text
    assert "category: infrastructure" in text
    # adapter prompt 가 cluster metadata 포함
    assert "evidence_count: 10" in adapter.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_produce_skill_draft_returns_none_on_empty_response(tmp_path):
    adapter = _StubAdapter(response="")
    p = _make_promoter(tmp_path, adapter=adapter)
    cluster = PatternCluster(
        handled_by="x", agent_handles=("@coder",), pipeline_id=None,
        evidence_count=10, avg_self_score=0.5,
    )
    path = await p._produce_skill_draft(cluster)
    assert path is None


@pytest.mark.asyncio
async def test_produce_skill_draft_swallows_adapter_exception(tmp_path):
    adapter = _StubAdapter(raises=RuntimeError("LLM down"))
    p = _make_promoter(tmp_path, adapter=adapter)
    cluster = PatternCluster(
        handled_by="x", agent_handles=("@coder",), pipeline_id=None,
        evidence_count=10, avg_self_score=0.5,
    )
    path = await p._produce_skill_draft(cluster)
    assert path is None  # graceful


# ---- _produce_weak_draft -----------------------------------------------


@pytest.mark.asyncio
async def test_produce_weak_draft_creates_file(tmp_path):
    adapter = _StubAdapter(response="updated SKILL.md content")
    p = _make_promoter(tmp_path, adapter=adapter)
    path = await p._produce_weak_draft("@debugger", 0.3, 8)
    assert path is not None
    assert path.exists()
    assert "weak_debugger" in str(path)


# ---- run_weekly end-to-end --------------------------------------------


@pytest.mark.asyncio
async def test_run_weekly_handles_no_data(tmp_path):
    """Empty ExperienceLog → no drafts, no errors."""
    p = _make_promoter(tmp_path)
    result = await p.run_weekly()
    assert result.new_skill_drafts == []
    assert result.weak_agent_drafts == []
    assert result.errors == []


@pytest.mark.asyncio
async def test_run_weekly_skips_covered_clusters(tmp_path):
    """All cluster handles registered → skipped (no draft)."""
    rows = [
        {
            "ts": (datetime.now(timezone.utc) - timedelta(hours=i)).isoformat(),
            "handled_by": "master:claude",
            "agent_handles": ["@coder"],
            "pipeline_id": None,
            "self_score": 0.7,
        }
        for i in range(6)
    ]
    _seed_log(tmp_path / "exp", rows)

    p = _make_promoter(tmp_path)
    result = await p.run_weekly()
    assert result.new_skill_drafts == []
    assert len(result.skipped_existing) == 1


@pytest.mark.asyncio
async def test_run_weekly_creates_draft_for_unknown_handles(tmp_path):
    """Unknown handle in cluster → SKILL.md draft."""
    rows = [
        {
            "ts": (datetime.now(timezone.utc) - timedelta(hours=i)).isoformat(),
            "handled_by": "master:claude",
            "agent_handles": ["@notifier"],   # unknown — not registered
            "pipeline_id": None,
            "self_score": 0.7,
        }
        for i in range(6)
    ]
    _seed_log(tmp_path / "exp", rows)

    adapter = _StubAdapter(response="""---
name: notifier
agent_handle: "@notifier"
---

# @notifier""")
    p = _make_promoter(tmp_path, adapter=adapter)
    result = await p.run_weekly()
    assert len(result.new_skill_drafts) == 1
    assert result.new_skill_drafts[0].exists()


# ---- _open_pr ----------------------------------------------------------


def test_open_pr_returns_none_when_gh_missing(tmp_path, monkeypatch):
    """gh CLI 없으면 graceful None 반환 (FileNotFoundError 캐치)."""
    p = _make_promoter(tmp_path, auto_pr=True)

    import subprocess

    def _fake_run(cmd, **kw):
        raise FileNotFoundError("gh: not found")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    url = p._open_pr(new_drafts=[], weak_drafts=[])
    assert url is None
