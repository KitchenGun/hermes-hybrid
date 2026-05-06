#!/usr/bin/env python3
"""Run the CuratorJob once.

Aggregates handled_by / tool stats from the experience log into:
  - logs/curator/handled_by_stats.json   (machine-readable)
  - logs/curator/{YYYY-MM-DD}.md         (human-readable summary)

Phase 15 (2026-05-07): --skill-promote 플래그로 SkillPromoter 도 함께 실행 —
ExperienceLog cluster → SKILL.md draft + 자동 git PR.

Usage:
    python scripts/curator_job.py
    python scripts/curator_job.py --window-days 7
    python scripts/curator_job.py --skill-promote        # Phase 15 — draft + PR
    python scripts/curator_job.py --skill-promote --no-auto-pr   # draft only
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from src.config import Settings  # noqa: E402
from src.core import ExperienceLogger  # noqa: E402
from src.jobs.curator_job import CuratorJob  # noqa: E402


async def _run_skill_promoter(settings: Settings, *, auto_pr: bool) -> int:
    """Phase 15 — SkillPromoter run. async because adapter.run is."""
    from src.agents import AgentRegistry
    from src.claude_adapter import ClaudeCodeAdapter
    from src.jobs.skill_promoter import SkillPromoter

    adapter = ClaudeCodeAdapter(settings)
    agents = AgentRegistry(repo_root=_REPO)
    log_root = settings.experience_log_root
    if not Path(log_root).is_absolute():
        log_root = _REPO / log_root

    promoter = SkillPromoter(
        adapter=adapter,
        agents=agents,
        experience_log_root=log_root,
        agents_root=_REPO / "agents",
        draft_dir=_REPO / settings.skill_promoter_draft_dir,
        min_evidence=settings.skill_promoter_min_evidence,
        auto_pr=auto_pr,
        weak_score_threshold=settings.skill_promoter_weak_score_threshold,
        repo_root=_REPO,
    )
    result = await promoter.run_weekly()
    print(
        f"SkillPromoter: new_drafts={len(result.new_skill_drafts)}, "
        f"weak_drafts={len(result.weak_agent_drafts)}, "
        f"prs={len(result.prs_opened)}, errors={len(result.errors)}"
    )
    for url in result.prs_opened:
        print(f"  PR: {url}")
    return 0 if not result.errors else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument(
        "--output-dir", type=Path, default=_REPO / "logs" / "curator"
    )
    parser.add_argument("--experience-log-root", type=Path, default=None)
    parser.add_argument(
        "--skill-promote", action="store_true",
        help="Phase 15 — also run SkillPromoter (cluster → SKILL.md draft + PR)",
    )
    parser.add_argument(
        "--no-auto-pr", action="store_true",
        help="With --skill-promote: draft only, do not open PR",
    )
    args = parser.parse_args()

    settings = Settings()
    log_root = args.experience_log_root or settings.experience_log_root
    if not Path(log_root).is_absolute():
        log_root = _REPO / log_root

    logger = ExperienceLogger(Path(log_root), enabled=True)
    job = CuratorJob(logger, args.output_dir, window_days=args.window_days)
    result = job.run()
    print(result.summary)
    if result.output_path:
        print(f"  → {result.output_path}")
    rc = 0 if result.ok else 1

    if args.skill_promote and settings.skill_promoter_enabled:
        auto_pr = settings.skill_promoter_auto_pr and not args.no_auto_pr
        promo_rc = asyncio.run(_run_skill_promoter(settings, auto_pr=auto_pr))
        if promo_rc != 0:
            rc = promo_rc
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
