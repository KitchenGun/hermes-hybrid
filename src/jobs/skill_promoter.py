"""SkillPromoter — Phase 15 (2026-05-07).

Hermes Agent (Nous) 의 "creates skills from experience, improves them
during use" + "auto-promotes effective procedures" 패턴 흡수.

목적: Curator 가 만들던 후보 markdown 을 → 실제 SKILL.md draft + git PR.

흐름:
  1. ExperienceLog cluster_patterns(since, until) — 자주 등장한 (handled_by,
     agent_handles, prompt-keyword) cluster 추출.
  2. 기존 17 SKILL.md 어디에도 명시 안 된 새 패턴이면 → SKILL.md draft 생성
     (master 호출로 frontmatter + 본문 작성).
  3. weak_agent_audit() — self_score 평균 < threshold 인 agent 식별 →
     해당 SKILL.md 의 not_for / when_to_use 보강 draft.
  4. open_pr() — gh CLI 로 새 branch + commit + PR. gh 없으면
     `logs/curator/auto_skills/` 에 draft 만 (graceful).
  5. 일요일 23:30 (Curator 30분 뒤) systemd-user timer 가 호출.

Settings:
  - skill_promoter_enabled: bool = True
  - skill_promoter_auto_pr: bool = True (사용자 결정)
  - skill_promoter_min_evidence: int = 5
  - skill_promoter_draft_dir: Path = ./logs/curator/auto_skills
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from src.obs import get_logger

log = get_logger(__name__)


_KEYWORDS_RE = re.compile(r"[a-zA-Z가-힣]{3,}")


@dataclass
class PatternCluster:
    """One repeated usage pattern extracted from ExperienceLog."""
    handled_by: str
    agent_handles: tuple[str, ...]
    pipeline_id: str | None
    evidence_count: int
    avg_self_score: float
    sample_handler_token_keywords: tuple[str, ...] = ()  # top freq tokens from handled_by


@dataclass
class SkillPromoterResult:
    """One SkillPromoter run summary."""
    new_skill_drafts: list[Path] = field(default_factory=list)
    weak_agent_drafts: list[Path] = field(default_factory=list)
    prs_opened: list[str] = field(default_factory=list)   # PR URLs
    skipped_existing: list[str] = field(default_factory=list)  # cluster signature
    errors: list[str] = field(default_factory=list)


class SkillPromoter:
    """ExperienceLog → SKILL.md draft + git PR (opt-in)."""

    def __init__(
        self,
        adapter: Any,                         # ClaudeCodeAdapter-like
        agents: Any,                          # AgentRegistry
        experience_log_root: Path,
        agents_root: Path,
        *,
        draft_dir: Path,
        min_evidence: int = 5,
        auto_pr: bool = True,
        weak_score_threshold: float = 0.4,
        repo_root: Path | None = None,
    ):
        self.adapter = adapter
        self.agents = agents
        self.experience_log_root = Path(experience_log_root)
        self.agents_root = Path(agents_root)
        self.draft_dir = Path(draft_dir)
        self.min_evidence = max(1, min_evidence)
        self.auto_pr = auto_pr
        self.weak_score_threshold = max(0.0, min(1.0, weak_score_threshold))
        self.repo_root = (
            Path(repo_root) if repo_root is not None else self.agents_root.parent
        )

    async def run_weekly(self) -> SkillPromoterResult:
        """일요일 23:30 KST. 7일치 ExperienceLog 으로 cluster + draft + PR."""
        result = SkillPromoterResult()
        until = datetime.now(timezone.utc)
        since = until - timedelta(days=7)

        try:
            clusters = list(self.cluster_patterns(since, until))
        except Exception as e:  # noqa: BLE001
            log.warning("skill_promoter.cluster_failed", err=str(e))
            result.errors.append(f"cluster: {e}")
            return result

        for cluster in clusters:
            try:
                if self._already_covered(cluster):
                    sig = self._cluster_signature(cluster)
                    result.skipped_existing.append(sig)
                    continue
                draft_path = await self._produce_skill_draft(cluster)
                if draft_path is not None:
                    result.new_skill_drafts.append(draft_path)
            except Exception as e:  # noqa: BLE001
                log.warning("skill_promoter.draft_failed", err=str(e))
                result.errors.append(f"draft: {e}")

        # Weak agent audit
        try:
            weak = list(self.weak_agent_audit(since, until))
        except Exception as e:  # noqa: BLE001
            log.warning("skill_promoter.weak_audit_failed", err=str(e))
            weak = []

        for handle, avg_score, count in weak:
            try:
                draft_path = await self._produce_weak_draft(handle, avg_score, count)
                if draft_path is not None:
                    result.weak_agent_drafts.append(draft_path)
            except Exception as e:  # noqa: BLE001
                result.errors.append(f"weak {handle}: {e}")

        # Open PRs (single PR for the batch — easier to review)
        if self.auto_pr and (result.new_skill_drafts or result.weak_agent_drafts):
            try:
                pr_url = self._open_pr(
                    new_drafts=result.new_skill_drafts,
                    weak_drafts=result.weak_agent_drafts,
                )
                if pr_url:
                    result.prs_opened.append(pr_url)
            except Exception as e:  # noqa: BLE001
                log.warning("skill_promoter.pr_failed", err=str(e))
                result.errors.append(f"pr: {e}")

        log.info(
            "skill_promoter.done",
            new_drafts=len(result.new_skill_drafts),
            weak_drafts=len(result.weak_agent_drafts),
            skipped=len(result.skipped_existing),
            prs=len(result.prs_opened),
            errors=len(result.errors),
        )
        return result

    # ---- cluster extraction ------------------------------------------

    def cluster_patterns(
        self, since: datetime, until: datetime
    ) -> Iterable[PatternCluster]:
        """ExperienceLog 의 (handled_by, agent_handles, pipeline_id) cluster
        — min_evidence 이상 반복된 것만."""
        rows = list(self._read_log(since, until))
        if not rows:
            return

        # Group by signature
        bucket: dict[tuple, list[dict]] = {}
        for r in rows:
            sig = (
                r.get("handled_by", ""),
                tuple(sorted(r.get("agent_handles") or [])),
                r.get("pipeline_id") or "",
            )
            bucket.setdefault(sig, []).append(r)

        for sig, sig_rows in bucket.items():
            if len(sig_rows) < self.min_evidence:
                continue
            handled_by, handles, pipeline_id = sig
            avg_score = sum(
                float(r.get("self_score") or 0.0) for r in sig_rows
            ) / max(1, len(sig_rows))
            # Extract top keyword tokens from handled_by string for naming hints
            tokens = _KEYWORDS_RE.findall(handled_by.lower())
            top_tokens = tuple(t for t, _ in Counter(tokens).most_common(5))
            yield PatternCluster(
                handled_by=handled_by,
                agent_handles=handles,
                pipeline_id=pipeline_id or None,
                evidence_count=len(sig_rows),
                avg_self_score=avg_score,
                sample_handler_token_keywords=top_tokens,
            )

    def _already_covered(self, cluster: PatternCluster) -> bool:
        """기존 17 SKILL.md 중 하나의 책임으로 이미 표현됐는가 — agent_handles
        가 이미 등록된 핸들들로만 구성됐고 pipeline_id 도 기존 4 pipeline
        중이면 cover 됐다고 판단."""
        for h in cluster.agent_handles:
            if self.agents.by_handle(h) is None:
                return False
        # 모든 핸들이 등록 — 기존 패턴. pipeline_id 도 마찬가지면 cover.
        return True

    @staticmethod
    def _cluster_signature(cluster: PatternCluster) -> str:
        return (
            f"{cluster.handled_by}|{','.join(cluster.agent_handles)}|"
            f"{cluster.pipeline_id or '-'}"
        )

    # ---- weak agent audit --------------------------------------------

    def weak_agent_audit(
        self, since: datetime, until: datetime
    ) -> Iterable[tuple[str, float, int]]:
        """Agent 별 self_score 평균 — threshold 미만 + 호출 N회 이상."""
        rows = list(self._read_log(since, until))
        if not rows:
            return

        scores: dict[str, list[float]] = {}
        for r in rows:
            for h in r.get("agent_handles") or []:
                scores.setdefault(h, []).append(float(r.get("self_score") or 0.0))

        for handle, vals in scores.items():
            if len(vals) < self.min_evidence:
                continue
            avg = sum(vals) / len(vals)
            if avg < self.weak_score_threshold:
                yield handle, avg, len(vals)

    # ---- drafts ------------------------------------------------------

    async def _produce_skill_draft(self, cluster: PatternCluster) -> Path | None:
        """master 호출로 새 SKILL.md frontmatter + 본문 draft."""
        prompt = self._draft_prompt(cluster)
        try:
            result = await self.adapter.run(prompt=prompt, history=[])
        except Exception as e:  # noqa: BLE001
            log.warning("skill_promoter.adapter_failed", err=str(e))
            return None

        text = (getattr(result, "text", "") or "").strip()
        if not text:
            return None

        # name 추출 — frontmatter의 name 필드 또는 fallback
        name_match = re.search(r"^name:\s*(\S+)", text, re.MULTILINE)
        name = name_match.group(1) if name_match else f"auto_{cluster.evidence_count}"
        name = re.sub(r"[^a-z0-9_]", "_", name.lower())
        if not name:
            name = "auto_skill"

        self.draft_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = self.draft_dir / f"{ts}_{name}.md"
        path.write_text(text + "\n", encoding="utf-8")
        log.info(
            "skill_promoter.draft",
            name=name,
            path=str(path),
            evidence=cluster.evidence_count,
        )
        return path

    async def _produce_weak_draft(
        self, handle: str, avg_score: float, count: int
    ) -> Path | None:
        """약한 agent 의 SKILL.md not_for / when_to_use 보강 제안."""
        entry = self.agents.by_handle(handle)
        if entry is None:
            return None
        skill_path = self.repo_root / entry.skill_md_path
        existing = (
            skill_path.read_text(encoding="utf-8")
            if skill_path.exists()
            else ""
        )

        prompt = (
            f"Agent {handle} 의 평균 self_score={avg_score:.2f} "
            f"({count}회 호출). threshold {self.weak_score_threshold} 미만. "
            f"기존 SKILL.md 의 'not_for' / 'when_to_use' 를 보강해 약점을 명시. "
            f"기존 frontmatter 유지 + 항목 1-3개 추가만. 결과: 갱신된 전체 SKILL.md.\n\n"
            f"[기존]\n{existing}"
        )
        try:
            result = await self.adapter.run(prompt=prompt, history=[])
        except Exception as e:  # noqa: BLE001
            log.warning("skill_promoter.weak_adapter_failed", err=str(e))
            return None

        text = (getattr(result, "text", "") or "").strip()
        if not text:
            return None

        self.draft_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        safe_handle = handle.replace("@", "")
        path = self.draft_dir / f"{ts}_weak_{safe_handle}.md"
        path.write_text(text + "\n", encoding="utf-8")
        log.info(
            "skill_promoter.weak_draft",
            handle=handle,
            avg_score=avg_score,
            path=str(path),
        )
        return path

    @staticmethod
    def _draft_prompt(cluster: PatternCluster) -> str:
        return (
            "다음 ExperienceLog cluster 가 기존 17 sub-agent / 4 pipeline 으로 "
            "잘 표현 안 됨. 새 SKILL.md (frontmatter + 본문) 작성. 형식:\n\n"
            "---\nname: <snake_case>\nagent_handle: \"@<name>\"\ncategory: "
            "<research|planning|implementation|quality|documentation|infrastructure>\n"
            "role: <short>\ndescription: <한 줄>\nwhen_to_use: [3 항목]\n"
            "not_for: [2 항목]\ninputs: [2 항목]\noutputs: [2 항목]\n"
            "metadata:\n  hermes:\n    primary_tools: [...]\n    tags: [...]\n"
            "auto_generated:\n  date: " + datetime.now(timezone.utc).strftime("%Y-%m-%d") + "\n"
            f"  evidence_count: {cluster.evidence_count}\n"
            "---\n\n# @<name> — <역할>\n\n## 책임\n...\n\n## 사용 패턴\n...\n\n"
            "[cluster]\n"
            f"handled_by: {cluster.handled_by}\n"
            f"agent_handles: {','.join(cluster.agent_handles)}\n"
            f"pipeline_id: {cluster.pipeline_id or '-'}\n"
            f"evidence_count: {cluster.evidence_count}\n"
            f"avg_self_score: {cluster.avg_self_score:.2f}\n"
            f"top_handler_tokens: {','.join(cluster.sample_handler_token_keywords)}\n"
        )

    # ---- ExperienceLog read ------------------------------------------

    def _read_log(
        self, since: datetime, until: datetime
    ) -> Iterable[dict[str, Any]]:
        if not self.experience_log_root.exists():
            return
        for f in sorted(self.experience_log_root.glob("*.jsonl")):
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                ts = row.get("ts", "")
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if since <= dt < until:
                    yield row

    # ---- gh CLI PR ---------------------------------------------------

    def _open_pr(
        self,
        *,
        new_drafts: list[Path],
        weak_drafts: list[Path],
    ) -> str | None:
        """gh CLI 로 새 branch + commit + PR. gh 없으면 None.
        draft 파일들은 logs/curator/auto_skills/ 에 떨어진 채로 보존."""
        try:
            # which gh
            r = subprocess.run(
                ["gh", "--version"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                log.info("skill_promoter.gh_not_available")
                return None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            log.info("skill_promoter.gh_not_available")
            return None

        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        branch = f"auto/skill-promote-{ts}"

        try:
            subprocess.run(
                ["git", "checkout", "-b", branch],
                cwd=self.repo_root, check=True, capture_output=True, timeout=10,
            )
            for p in [*new_drafts, *weak_drafts]:
                subprocess.run(
                    ["git", "add", str(p)],
                    cwd=self.repo_root, check=True, capture_output=True, timeout=10,
                )
            msg = (
                "chore(auto-skill): SkillPromoter draft batch\n\n"
                f"New skill drafts: {len(new_drafts)}\n"
                f"Weak agent drafts: {len(weak_drafts)}\n\n"
                "Generated by SkillPromoter (Phase 15). Review before merging into\n"
                "agents/ — drafts in logs/curator/auto_skills/ are not active until\n"
                "moved to agents/<category>/<name>/SKILL.md.\n"
            )
            subprocess.run(
                ["git", "commit", "-m", msg],
                cwd=self.repo_root, check=True, capture_output=True, timeout=10,
            )
            subprocess.run(
                ["git", "push", "-u", "origin", branch],
                cwd=self.repo_root, check=True, capture_output=True, timeout=30,
            )
            r = subprocess.run(
                [
                    "gh", "pr", "create",
                    "--title", f"chore(auto-skill): {len(new_drafts)+len(weak_drafts)} drafts",
                    "--body", msg,
                    "--head", branch,
                ],
                cwd=self.repo_root,
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                log.warning("skill_promoter.pr_create_failed", err=r.stderr[:300])
                return None
            url = r.stdout.strip()
            log.info("skill_promoter.pr_opened", url=url)
            return url
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            log.warning("skill_promoter.git_failed", err=str(e))
            return None
        finally:
            try:
                subprocess.run(
                    ["git", "checkout", "-"],
                    cwd=self.repo_root, capture_output=True, timeout=10,
                )
            except Exception:  # noqa: BLE001
                pass


__all__ = ["PatternCluster", "SkillPromoter", "SkillPromoterResult"]
