"""ReflectionJob — weekly statistical reduction of the experience log.

The job answers four questions every Sunday night:
  1. How much work did the bot actually do this week?
  2. Where did it succeed and where did it degrade or fail?
  3. Which profiles / tools dominated the load?
  4. What patterns are worth investigating before next week?

Output: ``logs/reflection/{ISO-YEAR}-W{ISO-WEEK}.md`` — short markdown,
read by a human in 2 minutes. No LLM call — pure stats. Future passes
can layer an LLM commentary on top, but the raw stats stay machine-
readable so a curator job can compare week-over-week.

The job is **idempotent**: re-running it for the same week overwrites
the file with the latest snapshot.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from src.core import ExperienceLogger, ExperienceRecord
from src.jobs.base import BaseJob, JobResult


def _kst_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=9)))


def _iso_week_label(when: datetime) -> str:
    iso_year, iso_week, _ = when.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    k = int(round((len(s) - 1) * p))
    return s[k]


def _summarize(records: list[ExperienceRecord]) -> dict[str, object]:
    """Compute the stats block that drives the markdown render."""
    n = len(records)
    if n == 0:
        return {
            "total": 0,
            "success_rate": 0.0,
            "degraded_rate": 0.0,
            "fail_rate": 0.0,
            "by_profile": [],
            "by_handled_by": [],
            "by_tier": [],
            "latency_p50": 0,
            "latency_p95": 0,
            "cloud_calls_total": 0,
            "self_score_avg": 0.0,
            "top_failures": [],
            "top_tools": [],
        }

    succ = sum(1 for r in records if r.outcome == "succeeded")
    deg = sum(1 for r in records if r.outcome == "degraded")
    fail = sum(1 for r in records if r.outcome == "failed")

    by_profile = Counter((r.profile or r.forced_profile or "—") for r in records)
    by_handled_by = Counter(r.handled_by for r in records if r.handled_by)
    by_tier = Counter(r.tier for r in records if r.tier)

    latencies = [r.latency_ms for r in records if r.latency_ms > 0]
    cloud_total = sum(r.cloud_calls for r in records)

    scored = [r.self_score for r in records if r.self_score > 0]
    score_avg = round(sum(scored) / len(scored), 3) if scored else 0.0

    # Top failure intents — group by (forced_profile or profile, handled_by)
    failure_buckets: Counter[tuple[str, str]] = Counter()
    for r in records:
        if r.outcome != "failed":
            continue
        bucket = (
            r.forced_profile or r.profile or "—",
            r.handled_by or "?",
        )
        failure_buckets[bucket] += 1

    # Top tools (lane-aware) — all tool_calls aggregated
    tool_counter: Counter[str] = Counter()
    tool_failures: dict[str, int] = defaultdict(int)
    for r in records:
        for tc in r.tool_calls:
            tool = str(tc.get("tool") or "?")
            tool_counter[tool] += 1
            if not tc.get("ok", True):
                tool_failures[tool] += 1

    return {
        "total": n,
        "success_rate": round(succ / n, 3),
        "degraded_rate": round(deg / n, 3),
        "fail_rate": round(fail / n, 3),
        "by_profile": by_profile.most_common(10),
        "by_handled_by": by_handled_by.most_common(10),
        "by_tier": by_tier.most_common(),
        "latency_p50": _percentile(latencies, 0.50),
        "latency_p95": _percentile(latencies, 0.95),
        "cloud_calls_total": cloud_total,
        "self_score_avg": score_avg,
        "top_failures": failure_buckets.most_common(5),
        "top_tools": [
            (tool, count, tool_failures.get(tool, 0))
            for tool, count in tool_counter.most_common(8)
        ],
    }


def _render_markdown(
    *,
    week_label: str,
    since: datetime,
    until: datetime,
    stats: dict[str, object],
) -> str:
    """Render the stats block into a short markdown report."""
    lines: list[str] = []
    lines.append(f"# Reflection {week_label}")
    lines.append("")
    lines.append(
        f"_Window: {since.isoformat(timespec='minutes')} → "
        f"{until.isoformat(timespec='minutes')}_"
    )
    lines.append("")

    total = stats["total"]
    lines.append(f"**Total tasks:** {total}")
    if total == 0:
        lines.append("")
        lines.append(
            "_No experience log entries in this window. Either the bot was "
            "idle, the logger was disabled, or the log root was rotated._"
        )
        return "\n".join(lines) + "\n"

    lines.append(f"**Success rate:** {stats['success_rate']:.1%}")
    lines.append(f"**Degraded rate:** {stats['degraded_rate']:.1%}")
    lines.append(f"**Fail rate:** {stats['fail_rate']:.1%}")
    lines.append(f"**Self-score avg:** {stats['self_score_avg']}")
    lines.append("")

    lines.append("## Latency")
    lines.append(f"- p50: {stats['latency_p50']} ms")
    lines.append(f"- p95: {stats['latency_p95']} ms")
    lines.append(f"- cloud calls total: {stats['cloud_calls_total']}")
    lines.append("")

    lines.append("## By profile")
    for profile, count in stats["by_profile"]:  # type: ignore[assignment]
        lines.append(f"- `{profile}`: {count}")
    lines.append("")

    lines.append("## By handled_by")
    for label, count in stats["by_handled_by"]:  # type: ignore[assignment]
        lines.append(f"- `{label}`: {count}")
    lines.append("")

    lines.append("## By tier")
    for tier, count in stats["by_tier"]:  # type: ignore[assignment]
        lines.append(f"- {tier}: {count}")
    lines.append("")

    if stats["top_failures"]:
        lines.append("## Top failure buckets")
        for (profile, handled_by), count in stats["top_failures"]:  # type: ignore[assignment]
            lines.append(f"- `{profile}` / `{handled_by}` — {count}")
        lines.append("")

    if stats["top_tools"]:
        lines.append("## Top tools")
        for tool, total_calls, failures in stats["top_tools"]:  # type: ignore[assignment]
            failure_pct = (failures / total_calls) if total_calls else 0.0
            lines.append(
                f"- `{tool}` — {total_calls} calls, {failures} failed "
                f"({failure_pct:.0%})"
            )
        lines.append("")

    lines.append("## Suggestions for next week")
    lines.append(
        "_Auto-suggestion is P2 work — the human curator drafts this "
        "section after reading the stats above._"
    )
    lines.append("")
    return "\n".join(lines)


class ReflectionJob(BaseJob):
    """Weekly stats reducer over ExperienceLog records."""

    name = "reflection_weekly"
    schedule = "0 22 * * 0"  # KST 일요일 22:00
    description = (
        "Aggregate the past week's experience log into a markdown report. "
        "Pure stats — no LLM call."
    )

    def __init__(
        self,
        logger: ExperienceLogger,
        output_dir: Path,
        *,
        window_days: int = 7,
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
        stats = _summarize(records)

        # Use KST for the human-readable label since the operator reads
        # this in KST. Internally everything stays UTC.
        kst_end = end.astimezone(timezone(timedelta(hours=9)))
        week_label = _iso_week_label(kst_end)

        body = _render_markdown(
            week_label=week_label,
            since=start.astimezone(timezone.utc),
            until=end.astimezone(timezone.utc),
            stats=stats,
        )

        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.output_dir / f"{week_label}.md"
        out_path.write_text(body, encoding="utf-8")

        return JobResult(
            ok=True,
            summary=(
                f"reflection {week_label}: {stats['total']} tasks, "
                f"success {stats['success_rate']:.0%}"
            ),
            output_path=out_path,
            metrics={
                "total": stats["total"],
                "success_rate": stats["success_rate"],
                "fail_rate": stats["fail_rate"],
                "latency_p95": stats["latency_p95"],
            },
        )


def run_reflection(
    logger: ExperienceLogger,
    output_dir: Path,
    *,
    window_days: int = 7,
    since: datetime | None = None,
    until: datetime | None = None,
) -> JobResult:
    """Convenience wrapper for one-shot CLI usage."""
    job = ReflectionJob(logger, output_dir, window_days=window_days)
    return job.run(since=since, until=until)


__all__ = ["ReflectionJob", "run_reflection", "_summarize", "_render_markdown"]
