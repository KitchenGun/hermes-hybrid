"""CuratorJob — first stat-aggregation pass over the experience log.

The full P5 curator (skill auto-promotion, archive, MEMORY.md cleanup) is
a multi-week build. This is its first brick: per-``handled_by`` success
and failure counts, written to ``logs/curator/handled_by_stats.json`` for
downstream consumers (skill registry, the human curator's review).

Why a separate file from ``skills/registry.yaml``:
  * registry.yaml is a static index of SKILL.md frontmatter (sourced from
    profile authors). Stats are dynamic and accumulate from runtime data.
  * Mixing them creates a race between the build_skill_registry.py CLI
    (manual / pre-commit) and the curator timer. Splitting keeps each
    file write-owned by exactly one process.

Output schema (JSON, atomic write):

    {
      "generated_at": "2026-05-05T14:00:00+00:00",
      "window_days": 30,
      "total_records": 412,
      "by_handled_by": {
        "skill:hybrid-memo": {"runs": 27, "successes": 27, "failures": 0,
                              "failure_rate": 0.0, "last_run": "..."},
        ...
      },
      "by_tool": {
        "sheets_append": {"calls": 14, "ok": 13, "failures": 1,
                          "failure_rate": 0.071},
        ...
      }
    }
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.core import ExperienceLogger, ExperienceRecord
from src.jobs.base import BaseJob, JobResult


def _empty_handled_stat() -> dict[str, Any]:
    return {
        "runs": 0,
        "successes": 0,
        "failures": 0,
        "degraded": 0,
        "failure_rate": 0.0,
        "last_run": None,
    }


def _empty_tool_stat() -> dict[str, Any]:
    return {"calls": 0, "ok": 0, "failures": 0, "failure_rate": 0.0}


# Phase 3 (2026-05-06): skill promotion / archive thresholds.
# Tuned conservatively — humans still review every candidate before
# anything moves to ``profiles/*/skills/`` or ``skills/archived/``.
PROMOTION_MIN_RUNS = 5            # too few runs → not enough evidence
PROMOTION_MAX_FAILURE_RATE = 0.20 # ≥ 20% fail rate → not promotion-grade
ARCHIVE_MIN_RUNS = 10             # archive needs more evidence than promote
ARCHIVE_MIN_FAILURE_RATE = 0.30   # ≥ 30% fail → archive candidate


def find_promotion_candidates(
    records: list[ExperienceRecord],
) -> list[dict[str, Any]]:
    """Group successful records by (profile_id, job_id) and surface
    those that hit the promotion thresholds.

    The candidate is a *recipe*: profile_id + job_id + skill_ids
    sequence. A human reviewer decides whether to materialize it as a
    new SKILL.md under the relevant profile.
    """
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for r in records:
        if not r.profile or not r.job_id:
            continue
        key = (r.profile, r.job_id)
        b = buckets.setdefault(
            key,
            {
                "profile_id": r.profile,
                "job_id": r.job_id,
                "runs": 0,
                "successes": 0,
                "failures": 0,
                "skill_id_counter": defaultdict(int),
                "last_used": None,
            },
        )
        b["runs"] += 1
        if r.outcome == "succeeded":
            b["successes"] += 1
        elif r.outcome == "failed":
            b["failures"] += 1
        for sid in r.skill_ids:
            b["skill_id_counter"][sid] += 1
        if b["last_used"] is None or r.ts > b["last_used"]:
            b["last_used"] = r.ts

    candidates: list[dict[str, Any]] = []
    for b in buckets.values():
        runs = b["runs"]
        if runs < PROMOTION_MIN_RUNS:
            continue
        fail_rate = b["failures"] / runs if runs else 0.0
        if fail_rate > PROMOTION_MAX_FAILURE_RATE:
            continue
        # Surface the most-frequent skill_ids as the candidate's recipe.
        top_skills = sorted(
            b["skill_id_counter"].items(),
            key=lambda kv: -kv[1],
        )[:5]
        candidates.append(
            {
                "profile_id": b["profile_id"],
                "job_id": b["job_id"],
                "runs": runs,
                "successes": b["successes"],
                "failures": b["failures"],
                "failure_rate": round(fail_rate, 3),
                "top_skills": [{"skill_id": s, "count": c} for s, c in top_skills],
                "last_used": b["last_used"],
            }
        )
    candidates.sort(key=lambda c: (-c["successes"], c["profile_id"], c["job_id"]))
    return candidates


def find_archive_candidates(
    records: list[ExperienceRecord],
) -> list[dict[str, Any]]:
    """Group records by skill_id and surface those above the archive
    thresholds. Skill that fails too often *while still seeing real
    traffic* is the target — a long-untouched skill stays alive."""
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "skill_id": "",
            "runs": 0,
            "failures": 0,
            "last_used": None,
        }
    )
    for r in records:
        for sid in r.skill_ids:
            b = buckets[sid]
            b["skill_id"] = sid
            b["runs"] += 1
            if r.outcome == "failed" or r.outcome == "degraded":
                b["failures"] += 1
            if b["last_used"] is None or r.ts > b["last_used"]:
                b["last_used"] = r.ts

    candidates: list[dict[str, Any]] = []
    for b in buckets.values():
        runs = b["runs"]
        if runs < ARCHIVE_MIN_RUNS:
            continue
        fail_rate = b["failures"] / runs if runs else 0.0
        if fail_rate < ARCHIVE_MIN_FAILURE_RATE:
            continue
        candidates.append(
            {
                "skill_id": b["skill_id"],
                "runs": runs,
                "failures": b["failures"],
                "failure_rate": round(fail_rate, 3),
                "last_used": b["last_used"],
            }
        )
    candidates.sort(key=lambda c: (-c["failure_rate"], -c["runs"]))
    return candidates


def aggregate_stats(records: list[ExperienceRecord]) -> dict[str, Any]:
    """Pure function: records → stats dict. Used directly by CuratorJob
    and exercised in tests without touching the filesystem."""
    by_handled: dict[str, dict[str, Any]] = defaultdict(_empty_handled_stat)
    by_tool: dict[str, dict[str, Any]] = defaultdict(_empty_tool_stat)

    for r in records:
        if not r.handled_by:
            continue
        h = by_handled[r.handled_by]
        h["runs"] += 1
        if r.outcome == "succeeded":
            h["successes"] += 1
        elif r.outcome == "failed":
            h["failures"] += 1
        elif r.outcome == "degraded":
            h["degraded"] += 1
        if h["last_run"] is None or r.ts > h["last_run"]:
            h["last_run"] = r.ts

        for tc in r.tool_calls:
            tool = str(tc.get("tool") or "")
            if not tool:
                continue
            t = by_tool[tool]
            t["calls"] += 1
            if tc.get("ok", True):
                t["ok"] += 1
            else:
                t["failures"] += 1

    for stats in by_handled.values():
        stats["failure_rate"] = (
            round(stats["failures"] / stats["runs"], 3) if stats["runs"] else 0.0
        )
    for stats in by_tool.values():
        stats["failure_rate"] = (
            round(stats["failures"] / stats["calls"], 3) if stats["calls"] else 0.0
        )

    return {
        "by_handled_by": dict(by_handled),
        "by_tool": dict(by_tool),
        # Phase 3: skill promotion/archive candidates. Empty lists are
        # legitimate signals — "this window had no candidate" is itself
        # information for the curator.
        "promotion_candidates": find_promotion_candidates(records),
        "archive_candidates": find_archive_candidates(records),
    }


def render_summary_md(
    *,
    generated_at: datetime,
    window_days: int,
    total: int,
    stats: dict[str, Any],
    top_n: int = 8,
) -> str:
    """Render a short markdown for human review.

    Sorted by failure_rate descending — the curator wants problems first.
    """
    lines: list[str] = []
    lines.append("# Curator stats")
    lines.append("")
    lines.append(f"_Generated: {generated_at.isoformat(timespec='seconds')}_")
    lines.append(f"_Window: last {window_days} days; {total} records_")
    lines.append("")

    if total == 0:
        lines.append("_No records in window — nothing to aggregate._")
        return "\n".join(lines) + "\n"

    handled = stats["by_handled_by"]
    if handled:
        lines.append("## handled_by — sorted by failure_rate, descending")
        lines.append("")
        lines.append("| handled_by | runs | succ | fail | fail_rate | last_run |")
        lines.append("|---|---:|---:|---:|---:|---|")
        sorted_handled = sorted(
            handled.items(),
            key=lambda kv: (-kv[1]["failure_rate"], -kv[1]["runs"]),
        )
        for name, s in sorted_handled[:top_n]:
            lines.append(
                f"| `{name}` | {s['runs']} | {s['successes']} | {s['failures']} "
                f"| {s['failure_rate']:.1%} | {s['last_run'] or '—'} |"
            )
        lines.append("")

    tools = stats["by_tool"]
    if tools:
        lines.append("## tools — sorted by failure_rate, descending")
        lines.append("")
        lines.append("| tool | calls | ok | fail | fail_rate |")
        lines.append("|---|---:|---:|---:|---:|")
        sorted_tools = sorted(
            tools.items(),
            key=lambda kv: (-kv[1]["failure_rate"], -kv[1]["calls"]),
        )
        for name, s in sorted_tools[:top_n]:
            lines.append(
                f"| `{name}` | {s['calls']} | {s['ok']} | {s['failures']} "
                f"| {s['failure_rate']:.1%} |"
            )
        lines.append("")

    # Phase 3: Promotion / Archive candidates surfaced for human review.
    promo = stats.get("promotion_candidates") or []
    if promo:
        lines.append("## Skill promotion candidates")
        lines.append("")
        lines.append(
            "_같은 (profile, job) 가 5회 이상 성공 + 실패율 ≤ 20% — 사람 review 후 "
            "`profiles/{profile}/skills/...` 로 승격 검토._"
        )
        lines.append("")
        lines.append("| profile | job_id | runs | succ | fail_rate | top skills | last |")
        lines.append("|---|---|---:|---:|---:|---|---|")
        for c in promo[:10]:
            top = ", ".join(
                f"`{s['skill_id']}`×{s['count']}" for s in c["top_skills"][:3]
            ) or "—"
            lines.append(
                f"| `{c['profile_id']}` | `{c['job_id']}` | {c['runs']} | "
                f"{c['successes']} | {c['failure_rate']:.1%} | {top} | "
                f"{c['last_used'] or '—'} |"
            )
        lines.append("")

    archive = stats.get("archive_candidates") or []
    if archive:
        lines.append("## Skill archive candidates")
        lines.append("")
        lines.append(
            "_단일 skill 이 10회 이상 + 실패율 ≥ 30% — 사람 review 후 "
            "`skills/archived/` 로 이동 검토._"
        )
        lines.append("")
        lines.append("| skill_id | runs | fail | fail_rate | last |")
        lines.append("|---|---:|---:|---:|---|")
        for c in archive[:10]:
            lines.append(
                f"| `{c['skill_id']}` | {c['runs']} | {c['failures']} | "
                f"{c['failure_rate']:.1%} | {c['last_used'] or '—'} |"
            )
        lines.append("")

    lines.append("## Suggestions")
    suggestions: list[str] = []
    # Trivial heuristic: handled_by with failure_rate >= 30% and runs >= 5
    # is a candidate for inspection. Skill auto-archive is a later pass.
    for name, s in handled.items():
        if s["runs"] >= 5 and s["failure_rate"] >= 0.30:
            suggestions.append(
                f"- `{name}`: {s['failure_rate']:.0%} failure rate over "
                f"{s['runs']} runs — review or consider deactivating."
            )
    for name, s in tools.items():
        if s["calls"] >= 10 and s["failure_rate"] >= 0.20:
            suggestions.append(
                f"- tool `{name}`: {s['failure_rate']:.0%} failure rate over "
                f"{s['calls']} calls — error path needs attention."
            )
    if suggestions:
        lines.extend(suggestions)
    else:
        lines.append("- _No automatic flags this window._")
    lines.append("")
    return "\n".join(lines)


class CuratorJob(BaseJob):
    """Aggregate experience log into per-handler / per-tool stats."""

    name = "curator_stats"
    schedule = "0 23 * * 0"  # KST 일요일 23:00 (Reflection 1시간 후)
    description = (
        "Aggregate handled_by/tool success/failure stats from the experience "
        "log into logs/curator/handled_by_stats.json + a short markdown."
    )

    def __init__(
        self,
        logger: ExperienceLogger,
        output_dir: Path,
        *,
        window_days: int = 30,
    ):
        self.logger = logger
        self.output_dir = Path(output_dir)
        self.window_days = window_days

    def run(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> JobResult:
        end = until or datetime.now(timezone.utc)
        start = since or (end - timedelta(days=self.window_days))
        records = list(self.logger.query(since=start, until=end))
        stats = aggregate_stats(records)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.output_dir / "handled_by_stats.json"
        md_path = self.output_dir / f"{end.date().isoformat()}.md"

        json_payload = {
            "generated_at": end.isoformat(timespec="seconds"),
            "window_days": self.window_days,
            "total_records": len(records),
            **stats,
        }
        # Atomic-ish write: write full content in one syscall, no streaming.
        json_path.write_text(
            json.dumps(json_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        md = render_summary_md(
            generated_at=end,
            window_days=self.window_days,
            total=len(records),
            stats=stats,
        )
        md_path.write_text(md, encoding="utf-8")

        return JobResult(
            ok=True,
            summary=(
                f"curator stats: {len(records)} records over "
                f"{self.window_days}d → {len(stats['by_handled_by'])} "
                f"handlers, {len(stats['by_tool'])} tools"
            ),
            output_path=md_path,
            metrics={
                "total": len(records),
                "handlers": len(stats["by_handled_by"]),
                "tools": len(stats["by_tool"]),
            },
        )


def run_curator(
    logger: ExperienceLogger,
    output_dir: Path,
    *,
    window_days: int = 30,
    since: datetime | None = None,
    until: datetime | None = None,
) -> JobResult:
    job = CuratorJob(logger, output_dir, window_days=window_days)
    return job.run(since=since, until=until)


__all__ = [
    "CuratorJob",
    "run_curator",
    "aggregate_stats",
    "render_summary_md",
    "find_promotion_candidates",
    "find_archive_candidates",
]
