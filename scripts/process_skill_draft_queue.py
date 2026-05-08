"""W10 — Skill draft queue drainer (every 30 min).

Drains data/skill_draft_queue.jsonl. For each cluster entry:
  - call SkillPromoter._produce_skill_draft(cluster) when ClaudeAdapter is
    available; otherwise emit a placeholder draft into
    logs/curator/auto_skills/<slug>.md so the next SkillPromoter weekly run
    can score and (optionally) auto-install.

Also performs the 30-day retention prune on data/recurring_request_log.jsonl.

Usage:
    python scripts/process_skill_draft_queue.py --dry-run
    python scripts/process_skill_draft_queue.py --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

QUEUE_PATH = REPO_ROOT / "data" / "skill_draft_queue.jsonl"
DRAFT_DIR = REPO_ROOT / "logs" / "curator" / "auto_skills"


def _slug(intent_cluster: str, ts: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in intent_cluster)
    return f"{ts}_{safe}".strip("_") or "auto_draft"


def _placeholder_draft(cluster: dict) -> str:
    intent = cluster.get("intent_cluster_hint") or "generic"
    sample = ", ".join(cluster.get("intent_token_sample", []))
    return (
        f"---\nname: auto_{intent}\nstatus: candidate\n"
        f"auto_generated:\n"
        f"  source: w10_recurring_request_detector\n"
        f"  similar_count: {cluster.get('similar_count')}\n"
        f"  ts: {cluster.get('ts')}\n"
        f"---\n\n# Auto SKILL — {intent}\n\n"
        f"## Purpose\n사용자가 최근 {cluster.get('similar_count')}회 비슷한 패턴으로 요청.\n\n"
        f"## Tokens\n{sample}\n\n"
        f"## When to Use\n- 사용자가 비슷한 의도를 다시 표현할 때.\n\n"
        f"## NEEDS_REVIEW\n사람이 SKILL.md 형식으로 정리 후 agents/<cat>/<name>/ 로 이동.\n"
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--queue", type=Path, default=QUEUE_PATH)
    p.add_argument("--draft-dir", type=Path, default=DRAFT_DIR)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True)
    g.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if args.apply:
        args.dry_run = False

    print(f"queue: {args.queue}")
    print(f"draft_dir: {args.draft_dir}")
    print(f"mode: {'apply' if args.apply else 'dry-run'}")

    if not args.queue.exists():
        print("queue empty / missing - nothing to drain")
    else:
        rows: list[dict] = []
        for line in args.queue.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except (ValueError, TypeError):
                continue
        print(f"queue rows: {len(rows)}")
        if not args.dry_run:
            args.draft_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            for i, cluster in enumerate(rows):
                slug = _slug(cluster.get("intent_cluster_hint", "auto"), ts)
                path = args.draft_dir / f"{slug}_{i:03d}.md"
                path.write_text(_placeholder_draft(cluster), encoding="utf-8")
                print(f"  + {path.relative_to(REPO_ROOT)}")
            args.queue.write_text("", encoding="utf-8")  # clear queue

    # Prune old recurring_request_log entries
    try:
        from src.orchestrator.recurring_request_detector_generated import prune_old
        kept = prune_old()
        print(f"recurring_request_log retained rows: {kept}")
    except Exception as e:  # noqa: BLE001
        print(f"prune skipped: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
