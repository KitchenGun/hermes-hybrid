"""W12 — Delegation pattern extractor.

Reads ExperienceRecord rows where `agent_handles` is non-empty (multi-agent
dispatch). Groups by intent cluster, ranks agent combinations.

Schedule: Mon 12:00 KST.

Usage:
    python scripts/delegation_pattern_extractor.py --dry-run --window 30d
    python scripts/delegation_pattern_extractor.py --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _parse_window(arg: str) -> timedelta:
    m = re.match(r"^(\d+)([dhm])$", arg.strip())
    if not m:
        return timedelta(days=30)
    n = int(m.group(1))
    unit = m.group(2)
    return {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]


def _read_rows(since: datetime) -> list[dict]:
    rows: list[dict] = []
    log_root = REPO_ROOT / "logs" / "experience"
    if not log_root.exists():
        return rows
    for f in sorted(log_root.glob("*.jsonl")):
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


def _classify(handled_by: str) -> str:
    """Light cluster classification — proxy for Loop 3 prompt-pattern taxonomy."""
    h = (handled_by or "").lower()
    if "github" in h:
        return "github_repo_analysis"
    if "review" in h:
        return "code_review"
    if "research" in h or "research" in h:
        return "research"
    if "weather" in h:
        return "weather"
    if "calendar" in h:
        return "calendar"
    if "journal" in h or "schedule" in h:
        return "schedule_logging"
    return "generic"


def _aggregate(rows: list[dict]) -> dict:
    multi: list[dict] = [r for r in rows if r.get("agent_handles")]
    by_cluster: dict[str, dict[tuple[str, ...], list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in multi:
        cluster = _classify(r.get("handled_by") or "")
        combo = tuple(sorted(r.get("agent_handles") or []))
        by_cluster[cluster][combo].append(r)

    out_clusters = []
    for cluster, combos in by_cluster.items():
        ranked = []
        for combo, rs in combos.items():
            scores = [float(r.get("self_score") or 0.0) for r in rs]
            lats = [int(r.get("latency_ms") or 0) for r in rs]
            ranked.append({
                "agents": list(combo),
                "avg_score": round(sum(scores) / len(scores), 3) if scores else 0.0,
                "sample_size": len(rs),
                "avg_latency_ms": int(sum(lats) / len(lats)) if lats else 0,
                "latest": max((r.get("ts") or "") for r in rs),
            })
        ranked.sort(key=lambda c: (-c["avg_score"], -c["sample_size"]))
        out_clusters.append({
            "intent_cluster": cluster,
            "best_combos": ranked[:3],
            "weak_combos": ranked[-3:][::-1] if len(ranked) > 3 else [],
            "total_samples": sum(c["sample_size"] for c in ranked),
        })
    out_clusters.sort(key=lambda c: -c["total_samples"])

    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows_analyzed": len(rows),
        "multi_agent_rows": len(multi),
        "clusters": out_clusters,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--window", default="30d")
    p.add_argument("--source", type=Path, default=None,
                   help="defaults to logs/experience/")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True)
    g.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if args.apply:
        args.dry_run = False

    since = datetime.now(timezone.utc) - _parse_window(args.window)
    rows = _read_rows(since)
    out = _aggregate(rows)

    print(f"rows: {out['rows_analyzed']}  multi-agent: {out['multi_agent_rows']}")
    print(f"clusters: {len(out['clusters'])}")
    for c in out["clusters"][:5]:
        print(f"  - {c['intent_cluster']} samples={c['total_samples']}")

    if out["multi_agent_rows"] == 0:
        print("insufficient data: 0 multi-agent rows in window")

    if args.dry_run:
        return 0

    out_path = REPO_ROOT / "data" / f"delegation_patterns_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump(out, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    print(f"wrote: {out_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
