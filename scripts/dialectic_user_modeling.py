"""W8 — Honcho-style dialectic user modeling.

Reads profiles/user_profile.generated.md + recent ExperienceRecord, classifies
each profile claim as confirmed / weakened / new / retired.

Schedule: Mon 06:00 KST.

Usage:
    python scripts/dialectic_user_modeling.py --dry-run --window 7d
    python scripts/dialectic_user_modeling.py --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = REPO_ROOT / "profiles" / "user_profile.generated.md"
EXPERIENCE_ROOT = REPO_ROOT / "logs" / "experience"


def _parse_window(arg: str) -> timedelta:
    m = re.match(r"^(\d+)([dhm])$", arg.strip())
    if not m:
        return timedelta(days=7)
    n = int(m.group(1))
    unit = m.group(2)
    return {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]


def _extract_claims(md_text: str) -> list[str]:
    """Pull bullet lines from sections — proxy for claims."""
    claims: list[str] = []
    for line in (md_text or "").splitlines():
        line = line.strip()
        if line.startswith("- "):
            claim = line[2:].strip()
            if claim and len(claim) > 10:
                claims.append(claim)
    return claims


def _read_recent(since: datetime) -> list[dict]:
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


def _classify(claim: str, rows: list[dict]) -> str:
    """Very light heuristic — keyword matching."""
    text = claim.lower()
    keywords = {
        "korean": ("한국어", "korean"),
        "terse": ("짧", "concise", "terse"),
        "skill_promoter": ("skill_promoter", "promote"),
        "memory_inject": ("memory_inject", "memory inject"),
        "discord": ("discord",),
    }
    for token_label, kws in keywords.items():
        if any(kw in text for kw in kws):
            seen = sum(
                1 for r in rows
                if any(kw in (r.get("handled_by") or "").lower() for kw in kws)
            )
            if seen >= 3:
                return "confirmed"
            if seen >= 1:
                return "new"
    return "weakened" if len(rows) >= 5 else "confirmed"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--window", default="7d")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True)
    g.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if args.apply:
        args.dry_run = False

    if not PROFILE_PATH.exists():
        print(f"profile missing: {PROFILE_PATH}", file=sys.stderr)
        return 1

    text = PROFILE_PATH.read_text(encoding="utf-8")
    claims = _extract_claims(text)
    since = datetime.now(timezone.utc) - _parse_window(args.window)
    rows = _read_recent(since)

    out = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profile_path": str(PROFILE_PATH.relative_to(REPO_ROOT)),
        "window": args.window,
        "rows_in_window": len(rows),
        "confirmed_claims": [],
        "weakened_claims": [],
        "new_claims": [],
        "retired_claims": [],
    }
    for claim in claims:
        verdict = _classify(claim, rows)
        out[f"{verdict}_claims"].append({
            "claim": claim[:200],
            "evidence_rows": len(rows),
        })

    print(f"claims: {len(claims)}  rows: {len(rows)}")
    print(f"confirmed={len(out['confirmed_claims'])} weakened={len(out['weakened_claims'])}"
          f" new={len(out['new_claims'])} retired={len(out['retired_claims'])}")

    if args.dry_run:
        return 0

    out_path = REPO_ROOT / "data" / f"user_profile_drift_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump(out, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    print(f"wrote: {out_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
