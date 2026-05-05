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


__all__ = ["CuratorJob", "run_curator", "aggregate_stats", "render_summary_md"]
