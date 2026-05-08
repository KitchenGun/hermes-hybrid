"""W9 — Skill self-modification meta-loop.

For each agents/{cat}/{name}/SKILL.md:
  - read usage count + score trend from ExperienceRecord
  - if last-5-uses avg < score_30d - 0.15, propose SKILL.draft.md with
    failure modes appended to "Safety / Constraints"

Schedule: Sat 23:00 KST.

Usage:
    python scripts/skill_self_modify.py --dry-run
    python scripts/skill_self_modify.py --dry-run --skill agents/research/researcher
    python scripts/skill_self_modify.py --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
EXPERIENCE_ROOT = REPO_ROOT / "logs" / "experience"
AGENTS_ROOT = REPO_ROOT / "agents"


def _read_rows(since: datetime) -> list[dict]:
    rows: list[dict] = []
    if not EXPERIENCE_ROOT.exists():
        return rows
    for f in sorted(EXPERIENCE_ROOT.glob("*.jsonl")):
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


def _scores_by_handle(rows: list[dict]) -> dict[str, list[tuple[datetime, float]]]:
    out: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for r in rows:
        score = r.get("self_score")
        if score is None:
            continue
        ts = r.get("ts", "")
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        for h in r.get("agent_handles") or []:
            out[h.lower()].append((dt, float(score)))
    return out


def _propose_modification(skill_path: Path, recent_avg: float, baseline_avg: float) -> Path:
    text = skill_path.read_text(encoding="utf-8", errors="replace")
    note = (
        f"\n\n## Auto-modified note ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})\n"
        f"- recent avg score: {recent_avg:.2f}\n"
        f"- 30d baseline avg: {baseline_avg:.2f}\n"
        f"- 자동 보강: 실패 모드 patterns review 후 not_for / 제약 항목 추가 검토.\n"
    )
    draft_path = skill_path.with_name("SKILL.draft.md")
    draft_path.write_text(text + note, encoding="utf-8")
    return draft_path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--skill", type=Path, default=None,
                   help="optional: limit to one agent dir")
    p.add_argument("--decline-delta", type=float, default=0.15)
    p.add_argument("--min-recent", type=int, default=5)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True)
    g.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if args.apply:
        args.dry_run = False

    if not AGENTS_ROOT.exists():
        print(f"agents/ missing: {AGENTS_ROOT}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    rows_30d = _read_rows(now - timedelta(days=30))
    rows_recent = _read_rows(now - timedelta(days=5))

    baseline = _scores_by_handle(rows_30d)
    recent = _scores_by_handle(rows_recent)

    targets: list[Path]
    if args.skill is not None:
        skill_md = args.skill / "SKILL.md"
        targets = [skill_md] if skill_md.exists() else []
    else:
        targets = sorted(AGENTS_ROOT.glob("**/SKILL.md"))

    print(f"agents scanned: {len(targets)}")
    print(f"30d rows: {len(rows_30d)}, recent rows: {len(rows_recent)}")
    print(f"mode: {'apply' if args.apply else 'dry-run'}")

    # extract handle from frontmatter
    handle_re = re.compile(r'^agent_handle:\s*"?(@[\w-]+)"?', re.MULTILINE)
    proposals: list[dict] = []
    for md in targets:
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = handle_re.search(text)
        if not m:
            continue
        handle = m.group(1).lower()
        b = [s for _, s in baseline.get(handle, [])]
        r = [s for _, s in recent.get(handle, [])]
        if len(r) < args.min_recent:
            continue
        baseline_avg = sum(b) / len(b) if b else 0.0
        recent_avg = sum(r) / len(r)
        if recent_avg + args.decline_delta < baseline_avg:
            proposals.append({
                "skill": str(md.relative_to(REPO_ROOT)),
                "handle": handle,
                "recent_avg": round(recent_avg, 3),
                "baseline_avg": round(baseline_avg, 3),
            })
            if args.apply:
                draft = _propose_modification(md, recent_avg, baseline_avg)
                proposals[-1]["draft"] = str(draft.relative_to(REPO_ROOT))

    if not proposals:
        print("no modification needed (scores stable / insufficient data)")
        return 0

    print(f"proposals: {len(proposals)}")
    for p_ in proposals[:10]:
        print(f"  - {p_['handle']} recent={p_['recent_avg']} vs base={p_['baseline_avg']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
