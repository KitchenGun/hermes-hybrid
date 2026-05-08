"""W7 — Migration self-review job.

Runs Sun 21:00 KST (before Reflection 22:00 → ABReport 22:30 → Curator
23:00 → SkillPromoter 23:30).

Reads:
  - data/growth_metrics.generated.yaml (W5 baseline)
  - logs/experience/*.jsonl (last 7d)

Computes deltas across 6 dimensions and emits:
  - data/self_review_<date>.md (human-readable action list)
  - memory/candidates_from_self_review_<date>.yaml (W11)
  - skills/generated_from_self_review/<date>/*.md (W11)
  - jobs/candidates_from_self_review_<date>.yaml (W11)

Usage:
    python scripts/migration_self_review.py --dry-run
    python scripts/migration_self_review.py --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read_jsonl(root: Path, since: datetime) -> list[dict]:
    rows: list[dict] = []
    if not root.exists():
        return rows
    for f in sorted(root.glob("*.jsonl")):
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except (ValueError, TypeError):
                    continue
                ts = r.get("ts", "")
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt >= since:
                    rows.append(r)
        except OSError:
            continue
    return rows


def _detect_recurring(rows: list[dict], min_count: int = 3) -> list[tuple[str, int]]:
    """Cluster handled_by tokens — proxy for recurring intents."""
    tokens: Counter = Counter()
    for r in rows:
        hb = r.get("handled_by") or ""
        if hb:
            tokens[hb] += 1
    return [(k, v) for k, v in tokens.items() if v >= min_count]


def _emit_memory_candidates(date: str, recurring: list[tuple[str, int]],
                            target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "candidates": [
            {
                "id": f"sr_{date}_{i:02d}",
                "type": "workflow",
                "content": f"반복 intent '{token}' ({count} 회) — self-review {date}.",
                "evidence": [f"logs/experience/ (handled_by={token}, count={count})"],
                "confidence": 0.7,
                "should_store": True,
            }
            for i, (token, count) in enumerate(recurring[:10])
        ],
    }
    target.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                      encoding="utf-8")
    return target


def _emit_job_candidates(date: str, recurring: list[tuple[str, int]],
                         target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "jobs": [
            {
                "id": f"sr_job_{date}_{i:02d}",
                "name": f"HermesSelfReviewProposed_{i:02d}",
                "purpose": f"self-review 가 추출한 반복 intent '{token}'를 cron 으로 자동화 검토.",
                "schedule": "0 9 * * *",
                "timezone": "Asia/Seoul",
                "status": "candidate",
                "evidence": f"self_review_{date} (handled_by={token}, count={count})",
            }
            for i, (token, count) in enumerate(recurring[:5])
        ],
    }
    target.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
                      encoding="utf-8")
    return target


def _emit_skill_drafts(date: str, recurring: list[tuple[str, int]],
                       target_dir: Path) -> list[Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    for i, (token, count) in enumerate(recurring[:3]):
        slug = (token or f"sr_{i}").replace("/", "_").replace(":", "_").replace(" ", "_")
        path = target_dir / f"{slug}.md"
        path.write_text(
            f"---\nname: sr_{slug}\nstatus: candidate\nauto_generated:\n"
            f"  source: self_review_{date}\n"
            f"  evidence_count: {count}\n---\n\n# {token} (self-review draft)\n\n"
            f"반복 intent (count={count}) 발견 — SKILL.md 미커버.\n",
            encoding="utf-8",
        )
        out.append(path)
    return out


def _delta_report(baseline: dict | None, rows: list[dict],
                  recurring: list[tuple[str, int]]) -> str:
    lines = [f"# Self-Review {_today_str()}", ""]
    lines.append(f"_records analyzed: {len(rows)}_")
    if baseline:
        lines.append("")
        lines.append("## Baseline reference")
        lines.append(f"- skill_count: {baseline.get('skill_count')}")
        lines.append(f"- memory_count: {baseline.get('memory_count')}")
        lines.append(f"- job_count: {baseline.get('job_count')}")
    lines.append("")
    lines.append(f"## Recurring patterns (count >= 3)")
    for token, count in recurring[:10]:
        lines.append(f"- `{token}` ×{count}")
    lines.append("")
    lines.append("## Proposed actions (queued in candidate yamls)")
    lines.append("- `memory/candidates_from_self_review_*.yaml` → next CuratorJob")
    lines.append("- `skills/generated_from_self_review/*/` → next SkillPromoter")
    lines.append("- `jobs/candidates_from_self_review_*.yaml` → manual review (P1)")
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baseline",
                   type=Path,
                   default=REPO_ROOT / "data" / "growth_metrics.generated.yaml")
    p.add_argument("--window-days", type=int, default=7)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True)
    g.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if args.apply:
        args.dry_run = False

    baseline = None
    if args.baseline.exists():
        try:
            baseline = yaml.safe_load(args.baseline.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            baseline = None

    since = datetime.now(timezone.utc) - timedelta(days=args.window_days)
    rows = _read_jsonl(REPO_ROOT / "logs" / "experience", since)
    recurring = _detect_recurring(rows)

    date = _today_str()
    review_md = _delta_report(baseline, rows, recurring)
    paths_planned = [
        REPO_ROOT / "data" / f"self_review_{date}.md",
        REPO_ROOT / "memory" / f"candidates_from_self_review_{date}.yaml",
        REPO_ROOT / "skills" / "generated_from_self_review" / date,
        REPO_ROOT / "jobs" / f"candidates_from_self_review_{date}.yaml",
    ]

    print(f"records: {len(rows)} (window={args.window_days}d)")
    print(f"recurring patterns (>=3): {len(recurring)}")
    print(f"mode: {'apply' if args.apply else 'dry-run'}")

    if args.dry_run:
        for p_ in paths_planned:
            print(f"  would write: {p_.relative_to(REPO_ROOT)}")
        return 0

    paths_planned[0].parent.mkdir(parents=True, exist_ok=True)
    paths_planned[0].write_text(review_md, encoding="utf-8")
    _emit_memory_candidates(date, recurring, paths_planned[1])
    _emit_skill_drafts(date, recurring, paths_planned[2])
    _emit_job_candidates(date, recurring, paths_planned[3])
    for p_ in paths_planned:
        print(f"wrote: {p_.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
