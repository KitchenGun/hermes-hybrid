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


# ---- Phase 18 (2026-05-07): auto-install + auto-revert ----------------


_AUTO_INSTALL_GOOD = """\
---
name: my_auto_skill
agent_handle: "@my_auto_skill"
category: implementation
role: helper
description: 자동 설치 후보 SKILL.md. 충분히 길어 description 보너스 받음.
when_to_use:
  - 사용 사례 1
  - 사용 사례 2
  - 사용 사례 3
not_for:
  - 비대상 1
inputs:
  - 입력 1
outputs:
  - 출력 1
---
# body
"""


_AUTO_INSTALL_BAD = """\
---
name: incomplete
agent_handle: "@incomplete"
category: implementation
---
# body
"""


def test_auto_install_promotes_high_score_draft(tmp_path):
    agents_root = tmp_path / "agents"
    agents_root.mkdir(parents=True)
    p = _make_promoter(
        tmp_path,
        agents=AgentRegistry(agents_root=agents_root, repo_root=tmp_path),
        agents_root=agents_root,
        repo_root=tmp_path,
        auto_install=True,
    )

    draft_dir = tmp_path / "drafts"
    draft_dir.mkdir(parents=True)
    draft_path = draft_dir / "20260507_my_auto_skill.md"
    draft_path.write_text(_AUTO_INSTALL_GOOD, encoding="utf-8")

    installed = p._maybe_auto_install(draft_path)
    assert installed is not None
    assert installed.name == "SKILL.md"
    assert installed.parent.parent.name == "auto"
    assert installed.read_text(encoding="utf-8") == _AUTO_INSTALL_GOOD


def test_auto_install_rejects_low_score_draft(tmp_path):
    agents_root = tmp_path / "agents"
    agents_root.mkdir(parents=True)
    p = _make_promoter(
        tmp_path,
        agents=AgentRegistry(agents_root=agents_root, repo_root=tmp_path),
        agents_root=agents_root,
        repo_root=tmp_path,
        auto_install=True,
    )

    draft_dir = tmp_path / "drafts"
    draft_dir.mkdir(parents=True)
    draft_path = draft_dir / "20260507_incomplete.md"
    draft_path.write_text(_AUTO_INSTALL_BAD, encoding="utf-8")

    installed = p._maybe_auto_install(draft_path)
    assert installed is None
    # original .md gone, .rejected sibling exists
    assert not draft_path.exists()
    rejected = draft_dir / "20260507_incomplete.md.rejected"
    assert rejected.exists()


def test_auto_install_invalidates_registry(tmp_path):
    agents_root = tmp_path / "agents"
    agents_root.mkdir(parents=True)
    reg = AgentRegistry(agents_root=agents_root, repo_root=tmp_path)
    reg.all()                                    # prime cache (empty)

    p = _make_promoter(
        tmp_path,
        agents=reg,
        agents_root=agents_root,
        repo_root=tmp_path,
        auto_install=True,
    )
    draft_dir = tmp_path / "drafts"
    draft_dir.mkdir(parents=True)
    draft_path = draft_dir / "x.md"
    draft_path.write_text(_AUTO_INSTALL_GOOD, encoding="utf-8")

    p._maybe_auto_install(draft_path)
    # Registry now picks up the new agent without an explicit reload.
    assert reg.by_handle("@my_auto_skill") is not None


def test_auto_revert_archives_low_score_auto_skill(tmp_path):
    agents_root = tmp_path / "agents"
    auto_dir = agents_root / "auto" / "rusty"
    auto_dir.mkdir(parents=True)
    md = auto_dir / "SKILL.md"
    md.write_text(
        _AUTO_INSTALL_GOOD.replace("@my_auto_skill", "@rusty"),
        encoding="utf-8",
    )

    exp_root = tmp_path / "exp"
    # 5 rows for @rusty with low self_score.
    rows = [
        {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "task_id": f"t{i}",
            "session_id": "s",
            "user_id": "u",
            "agent_handles": ["@rusty"],
            "self_score": 0.1,
        }
        for i in range(5)
    ]
    _seed_log(exp_root, rows)

    p = _make_promoter(
        tmp_path,
        agents=AgentRegistry(agents_root=agents_root, repo_root=tmp_path),
        agents_root=agents_root,
        experience_log_root=exp_root,
        repo_root=tmp_path,
        auto_install=True,
        revert_min_uses=5,
        revert_score_threshold=0.3,
    )

    reverted = p._auto_revert_underperforming(
        datetime.now(timezone.utc) - timedelta(days=1),
        datetime.now(timezone.utc) + timedelta(days=1),
    )
    assert "@rusty" in reverted
    assert not auto_dir.exists()                 # moved
    archived = agents_root / "_archived" / "rusty"
    assert archived.exists()


def test_auto_revert_keeps_high_score_skill(tmp_path):
    agents_root = tmp_path / "agents"
    auto_dir = agents_root / "auto" / "good"
    auto_dir.mkdir(parents=True)
    md = auto_dir / "SKILL.md"
    md.write_text(
        _AUTO_INSTALL_GOOD.replace("@my_auto_skill", "@good"),
        encoding="utf-8",
    )

    exp_root = tmp_path / "exp"
    rows = [
        {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "task_id": f"t{i}",
            "session_id": "s",
            "user_id": "u",
            "agent_handles": ["@good"],
            "self_score": 0.9,                   # well above threshold
        }
        for i in range(5)
    ]
    _seed_log(exp_root, rows)

    p = _make_promoter(
        tmp_path,
        agents=AgentRegistry(agents_root=agents_root, repo_root=tmp_path),
        agents_root=agents_root,
        experience_log_root=exp_root,
        repo_root=tmp_path,
        auto_install=True,
        revert_min_uses=5,
        revert_score_threshold=0.3,
    )

    reverted = p._auto_revert_underperforming(
        datetime.now(timezone.utc) - timedelta(days=1),
        datetime.now(timezone.utc) + timedelta(days=1),
    )
    assert reverted == []
    assert auto_dir.exists()


def test_auto_revert_skips_when_not_enough_uses(tmp_path):
    """N < revert_min_uses 면 평가 안 — 데이터 부족."""
    agents_root = tmp_path / "agents"
    auto_dir = agents_root / "auto" / "new"
    auto_dir.mkdir(parents=True)
    md = auto_dir / "SKILL.md"
    md.write_text(
        _AUTO_INSTALL_GOOD.replace("@my_auto_skill", "@new"),
        encoding="utf-8",
    )

    exp_root = tmp_path / "exp"
    rows = [
        {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "task_id": "t1",
            "session_id": "s",
            "user_id": "u",
            "agent_handles": ["@new"],
            "self_score": 0.0,                   # very low
        }
    ]
    _seed_log(exp_root, rows)

    p = _make_promoter(
        tmp_path,
        agents=AgentRegistry(agents_root=agents_root, repo_root=tmp_path),
        agents_root=agents_root,
        experience_log_root=exp_root,
        repo_root=tmp_path,
        auto_install=True,
        revert_min_uses=5,
    )

    reverted = p._auto_revert_underperforming(
        datetime.now(timezone.utc) - timedelta(days=1),
        datetime.now(timezone.utc) + timedelta(days=1),
    )
    assert reverted == []                        # only 1 use, < min 5


def test_weak_audit_flags_handle_with_high_negative_feedback(tmp_path):
    """Phase 20 — score 평균 OK 여도 negative feedback ≥ threshold 면 weak."""
    from src.core import ExperienceLogger
    exp_root = tmp_path / "exp"

    rows = [
        {
            "ts": "2026-05-07T01:00:00+00:00",
            "task_id": f"t{i}",
            "session_id": "s",
            "user_id": "u",
            "agent_handles": ["@coder"],
            "self_score": 0.95,                  # well above weak threshold
        }
        for i in range(5)
    ]
    _seed_log(exp_root, rows)

    logger = ExperienceLogger(exp_root, enabled=True)
    # 3 negative reactions — at threshold default 3.
    for tid in ("t0", "t1", "t2"):
        logger.append_feedback(tid, feedback="negative")

    p = _make_promoter(
        tmp_path,
        agents=AgentRegistry(repo_root=_REPO_ROOT),
        experience_log_root=exp_root,
        repo_root=_REPO_ROOT,
        experience_logger=logger,
        negative_threshold=3,
    )

    weak = list(p.weak_agent_audit(
        datetime(2026, 5, 6, tzinfo=timezone.utc),
        datetime(2026, 5, 8, tzinfo=timezone.utc),
    ))
    handles = [w[0] for w in weak]
    assert "@coder" in handles


def test_weak_audit_skips_handle_with_low_negative_count(tmp_path):
    """Phase 20 — negative_count < threshold + score OK → not flagged."""
    from src.core import ExperienceLogger
    exp_root = tmp_path / "exp"

    rows = [
        {
            "ts": "2026-05-07T01:00:00+00:00",
            "task_id": f"t{i}",
            "session_id": "s",
            "user_id": "u",
            "agent_handles": ["@coder"],
            "self_score": 0.95,
        }
        for i in range(5)
    ]
    _seed_log(exp_root, rows)

    logger = ExperienceLogger(exp_root, enabled=True)
    logger.append_feedback("t0", feedback="negative")     # only 1, < 3

    p = _make_promoter(
        tmp_path,
        agents=AgentRegistry(repo_root=_REPO_ROOT),
        experience_log_root=exp_root,
        repo_root=_REPO_ROOT,
        experience_logger=logger,
        negative_threshold=3,
    )

    weak = list(p.weak_agent_audit(
        datetime(2026, 5, 6, tzinfo=timezone.utc),
        datetime(2026, 5, 8, tzinfo=timezone.utc),
    ))
    assert weak == []                            # neither score nor feedback weak


def test_weak_audit_without_logger_falls_back_to_score_only(tmp_path):
    """experience_logger=None → 기존 score 기반 동작 유지."""
    exp_root = tmp_path / "exp"
    rows = [
        {
            "ts": "2026-05-07T01:00:00+00:00",
            "task_id": f"t{i}",
            "session_id": "s",
            "user_id": "u",
            "agent_handles": ["@coder"],
            "self_score": 0.1,                   # low → score-weak
        }
        for i in range(5)
    ]
    _seed_log(exp_root, rows)

    p = _make_promoter(
        tmp_path,
        agents=AgentRegistry(repo_root=_REPO_ROOT),
        experience_log_root=exp_root,
        repo_root=_REPO_ROOT,
        experience_logger=None,
    )
    weak = list(p.weak_agent_audit(
        datetime(2026, 5, 6, tzinfo=timezone.utc),
        datetime(2026, 5, 8, tzinfo=timezone.utc),
    ))
    handles = [w[0] for w in weak]
    assert "@coder" in handles


def test_pr_command_includes_auto_skill_label(tmp_path, monkeypatch):
    """Phase 18 — gh pr create 가 --label auto-skill 인자를 포함해야."""
    p = _make_promoter(tmp_path, auto_pr=True, repo_root=tmp_path)

    captured: list[list[str]] = []

    import subprocess

    class _OK:
        returncode = 0
        stdout = "https://github.com/example/pr/1\n"
        stderr = ""

    def _fake_run(cmd, **kw):
        captured.append(list(cmd))
        return _OK()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    # one synthetic draft path so the PR has something to push
    draft = tmp_path / "drafts" / "x.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("placeholder", encoding="utf-8")

    p._open_pr(new_drafts=[draft], weak_drafts=[])

    pr_cmd = next(
        (c for c in captured if len(c) >= 2 and c[0] == "gh" and c[1] == "pr"),
        None,
    )
    assert pr_cmd is not None
    assert "--label" in pr_cmd
    assert "auto-skill" in pr_cmd
