"""W5 — Capture data/growth_metrics.generated.yaml baseline.

Fields:
  - skill_count: agents/{cat}/{name}/SKILL.md count + src/skills/* count
  - memory_count: data/memory/memos.db row count + data/memory/MEMORY.md line count
  - job_count: config/job_factory.yaml job_types + generated job candidates
  - prompt_pattern_distribution: top-N intents from logs/experience/*.jsonl
  - ab_treatment_stats: ab experiment arm sample sizes
  - experience_record_count: total jsonl lines
  - timestamp + git_sha

Usage:
    python scripts/capture_growth_metrics.py --output data/growth_metrics.generated.yaml
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "data" / "growth_metrics.generated.yaml"


def _git_sha() -> str:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return r.stdout.strip()[:12]
    except Exception:
        pass
    return "unknown"


def _skill_count() -> int:
    n = 0
    agents = REPO_ROOT / "agents"
    if agents.exists():
        n += sum(1 for _ in agents.glob("**/SKILL.md"))
    src_skills = REPO_ROOT / "src" / "skills"
    if src_skills.exists():
        n += sum(1 for _ in src_skills.iterdir() if _.is_dir())
    return n


def _memory_count() -> dict:
    out = {"memos_db_rows": 0, "memory_md_lines": 0}
    db = REPO_ROOT / "data" / "memory" / "memos.db"
    if db.exists():
        try:
            r = subprocess.run(
                ["sqlite3", str(db), "SELECT COUNT(*) FROM memos"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                out["memos_db_rows"] = int(r.stdout.strip() or 0)
        except Exception:
            pass
    md = REPO_ROOT / "data" / "memory" / "MEMORY.md"
    if md.exists():
        try:
            out["memory_md_lines"] = len(md.read_text(encoding="utf-8").splitlines())
        except OSError:
            pass
    return out


def _job_count() -> dict:
    out = {"job_factory_types": 0, "generated_candidates": 0}
    jf = REPO_ROOT / "config" / "job_factory.yaml"
    if jf.exists():
        try:
            data = yaml.safe_load(jf.read_text(encoding="utf-8")) or {}
            out["job_factory_types"] = len(data.get("job_types") or [])
        except yaml.YAMLError:
            pass
    gen = REPO_ROOT / "jobs" / "generated" / "job_candidates.generated.yaml"
    if gen.exists():
        try:
            data = yaml.safe_load(gen.read_text(encoding="utf-8")) or {}
            out["generated_candidates"] = len(data.get("jobs") or [])
        except yaml.YAMLError:
            pass
    return out


def _experience_stats() -> dict:
    out = {
        "record_count": 0,
        "ab_treatment_stats": {"control": 0, "treatment": 0, "treatment_no_hits": 0, "none": 0},
        "prompt_pattern_distribution": {},
    }
    log_root = REPO_ROOT / "logs" / "experience"
    if not log_root.exists():
        return out
    handled_counter: Counter = Counter()
    for f in sorted(log_root.glob("*.jsonl")):
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                out["record_count"] += 1
                arm = row.get("experiment_arm")
                key = arm if arm in ("control", "treatment", "treatment_no_hits") else "none"
                out["ab_treatment_stats"][key] += 1
                hb = row.get("handled_by") or "unknown"
                handled_counter[hb] += 1
        except OSError:
            continue
    out["prompt_pattern_distribution"] = dict(handled_counter.most_common(10))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = p.parse_args()

    metrics = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_sha": _git_sha(),
        "skill_count": _skill_count(),
        "memory_count": _memory_count(),
        "job_count": _job_count(),
        **_experience_stats(),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        yaml.safe_dump(metrics, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"baseline → {args.output}")
    print(yaml.safe_dump(metrics, allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
