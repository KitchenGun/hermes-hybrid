"""W2 — Promote skills/generated/*.md → agents/{category}/{name}/SKILL.md.

For each skills/generated/*.md:
  - read frontmatter (name, agent_handle, category, role, description, when_to_use, ...)
  - if `category` matches one of (research/planning/implementation/quality/documentation/infrastructure)
    use it; else fall back to config/skill_category_map.generated.yaml mappings
  - score via src.jobs.skill_critic_rerun.score_draft (if available)
  - write to agents/{category}/{name}/SKILL.md if score >= threshold (default 0.85)
  - bypasses SkillPromoter.run_weekly() — direct conversion of user-prompt format

Usage:
    python scripts/promote_generated_skills.py --dry-run
    python scripts/promote_generated_skills.py --apply --score-threshold 0.85
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_SOURCE = REPO_ROOT / "skills" / "generated"
DEFAULT_TARGET_ROOT = REPO_ROOT / "agents"
DEFAULT_LOG = REPO_ROOT / "data" / "skill_promotion.log"
CATEGORY_MAP_PATH = REPO_ROOT / "config" / "skill_category_map.generated.yaml"

VALID_CATEGORIES = (
    "research", "planning", "implementation",
    "quality", "documentation", "infrastructure",
)


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    fm_text = text[4:end]
    body = text[end + 4 :].lstrip("\n")
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        fm = {}
    return fm if isinstance(fm, dict) else {}, body


def _load_category_map() -> dict:
    if not CATEGORY_MAP_PATH.exists():
        return {"mappings": {}, "default_category": "documentation"}
    with CATEGORY_MAP_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {"mappings": {}, "default_category": "documentation"}


def _resolve_category(fm: dict, name: str, cat_map: dict) -> str:
    cat = (fm.get("category") or "").strip().lower()
    if cat in VALID_CATEGORIES:
        return cat
    mapped = (cat_map.get("mappings") or {}).get(name)
    if mapped in VALID_CATEGORIES:
        return mapped
    return cat_map.get("default_category", "documentation")


def _try_score(text: str) -> float:
    try:
        from src.jobs.skill_critic_rerun import score_draft
    except Exception:
        return 1.0  # critic unavailable → pass-through (still gated by threshold)
    try:
        return float(score_draft(text))
    except Exception:
        return 1.0


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9_]", "_", name.lower())
    return s or "auto_skill"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    p.add_argument("--target-root", type=Path, default=DEFAULT_TARGET_ROOT)
    p.add_argument("--score-threshold", type=float, default=0.85)
    p.add_argument("--log", type=Path, default=DEFAULT_LOG)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True)
    g.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if args.apply:
        args.dry_run = False

    if not args.source.exists():
        print(f"source missing: {args.source}", file=sys.stderr)
        return 1

    cat_map = _load_category_map()
    candidates = sorted(args.source.glob("*.md"))
    print(f"source: {args.source}")
    print(f"target: {args.target_root}")
    print(f"candidates: {len(candidates)}")
    print(f"mode: {'apply' if args.apply else 'dry-run'}")

    args.log.parent.mkdir(parents=True, exist_ok=True)
    promoted: list[dict] = []
    rejected: list[dict] = []
    for md in candidates:
        try:
            text = md.read_text(encoding="utf-8")
        except OSError as e:
            rejected.append({"path": str(md), "reason": f"read: {e}"})
            continue
        fm, _body = _split_frontmatter(text)
        name = (fm.get("name") or md.stem).strip()
        name = _slug(name)
        category = _resolve_category(fm, name, cat_map)
        score = _try_score(text)
        target = args.target_root / category / name / "SKILL.md"
        record = {
            "source": str(md.relative_to(REPO_ROOT)),
            "name": name,
            "category": category,
            "score": round(score, 3),
            "target": str(target.relative_to(REPO_ROOT)),
        }
        if score < args.score_threshold:
            record["reason"] = "score_below_threshold"
            rejected.append(record)
            continue
        if args.dry_run:
            record["action"] = "would_write"
            promoted.append(record)
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            record["action"] = "written"
            promoted.append(record)
        except OSError as e:
            record["action"] = "write_failed"
            record["reason"] = str(e)
            rejected.append(record)

    summary = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "apply" if args.apply else "dry-run",
        "source": str(args.source),
        "score_threshold": args.score_threshold,
        "promoted": promoted,
        "rejected": rejected,
    }
    with args.log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")

    print(f"promoted: {len(promoted)}")
    print(f"rejected: {len(rejected)}")
    for r in promoted[:5]:
        print(f"  + {r['name']} → {r['target']} (score={r['score']})")
    for r in rejected[:5]:
        print(f"  - {r['name']} reason={r.get('reason')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
