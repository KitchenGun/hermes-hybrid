"""P0c — gated auto-apply runner.

Phases:
  c1: baseline capture
  c2: memory schema migration + ingest
  c3: 7 marker blocks (W4, W6a, W6b, W10, W11_curator, W11_promoter, W12)
  c4: skill promotion
  c5a: config/job_factory.yaml registration
  c5b: timer_handlers W3 marker blocks (3 platforms) + hermes-setup non-interactive

Each phase has a gate. On gate failure, runner stops and surfaces.

Usage:
    python scripts/run_p0c.py --phases c1,c2,c3,c4,c5a,c5b
    python scripts/run_p0c.py --phases c1            # only baseline
    python scripts/run_p0c.py --dry-run --phases c1,c2,c3,c4,c5a,c5b
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(cmd: list[str], cwd: Path | None = None) -> int:
    print(f"\n$ {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=cwd or REPO_ROOT)
    return r.returncode


def _python() -> str:
    return sys.executable


def phase_c1(dry: bool) -> int:
    cmd = [_python(), "scripts/capture_growth_metrics.py",
           "--output", "data/growth_metrics.generated.yaml"]
    rc = _run(cmd)
    out = REPO_ROOT / "data" / "growth_metrics.generated.yaml"
    if not out.exists():
        print("GATE 1 FAIL: data/growth_metrics.generated.yaml missing", file=sys.stderr)
        return 1
    text = out.read_text(encoding="utf-8")
    needed = ("skill_count", "memory_count", "job_count",
              "experience_record_count" if False else "record_count",
              "ab_treatment_stats", "timestamp", "git_sha")
    missing = [k for k in needed if k not in text]
    if missing:
        print(f"GATE 1 FAIL: baseline missing fields {missing}", file=sys.stderr)
        return 1
    print("GATE 1 OK")
    return 0 if rc == 0 else rc


def phase_c2(dry: bool) -> int:
    rc = _run([_python(), "scripts/migrate_memos_add_source.py", "--apply"])
    if rc != 0:
        print("GATE 2.0 FAIL: migrate_memos_add_source", file=sys.stderr)
        return 1
    if dry:
        rc = _run([_python(), "scripts/ingest_memory_candidates.py", "--dry-run"])
    else:
        rc = _run([_python(), "scripts/ingest_memory_candidates.py", "--apply"])
    if rc != 0:
        print("GATE 2 FAIL: ingest_memory_candidates", file=sys.stderr)
        return 1
    print("GATE 2 OK")
    return 0


def phase_c3(dry: bool) -> int:
    cmd = [_python(), "scripts/apply_marker_blocks.py"]
    if dry:
        cmd.append("--dry-run")
    else:
        cmd.append("--apply")
    rc = _run(cmd)
    if rc != 0:
        print("GATE 3 FAIL: apply_marker_blocks", file=sys.stderr)
        return 1
    if not dry:
        # smoke: imports must not fail
        rc2 = _run([_python(), "-c",
                    "import src.orchestrator.hermes_master, src.mcp.server, "
                    "src.jobs.curator_job, src.jobs.skill_promoter; print('imports OK')"])
        if rc2 != 0:
            print("GATE 3 FAIL: import sanity", file=sys.stderr)
            return 1
    print("GATE 3 OK")
    return 0


def phase_c4(dry: bool) -> int:
    cmd = [_python(), "scripts/promote_generated_skills.py"]
    cmd.append("--dry-run" if dry else "--apply")
    rc = _run(cmd)
    if rc != 0:
        print("GATE 4 FAIL: promote_generated_skills", file=sys.stderr)
        return 1
    print("GATE 4 OK")
    return 0


def phase_c5a(dry: bool) -> int:
    cmd = [_python(), "scripts/register_generated_jobs.py",
           "--candidates-only", "--validate"]
    cmd.append("--dry-run" if dry else "--apply")
    rc = _run(cmd)
    if rc != 0:
        print("GATE 5a FAIL: register_generated_jobs", file=sys.stderr)
        return 1
    print("GATE 5a OK")
    return 0


def phase_c5b(dry: bool) -> int:
    cmd = [_python(), "scripts/apply_marker_blocks.py", "--timers-only"]
    cmd.append("--dry-run" if dry else "--apply")
    rc = _run(cmd)
    if rc != 0:
        print("GATE 5b FAIL: apply_marker_blocks --timers-only", file=sys.stderr)
        return 1
    print("GATE 5b OK")
    print("Note: hermes-setup --non-interactive 는 사용자가 별도 실행 (timer 등록).")
    return 0


_PHASES = {
    "c1": phase_c1,
    "c2": phase_c2,
    "c3": phase_c3,
    "c4": phase_c4,
    "c5a": phase_c5a,
    "c5b": phase_c5b,
}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--phases", default="c1,c2,c3,c4,c5a,c5b")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    requested = [s.strip() for s in args.phases.split(",") if s.strip()]
    for ph in requested:
        if ph not in _PHASES:
            print(f"unknown phase: {ph}", file=sys.stderr)
            return 1

    for ph in requested:
        print(f"\n=== Phase P0c.{ph} ({'dry-run' if args.dry_run else 'apply'}) ===")
        rc = _PHASES[ph](args.dry_run)
        if rc != 0:
            print(f"\nP0c stopped at {ph} (exit {rc})", file=sys.stderr)
            return rc

    print("\nP0c complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
