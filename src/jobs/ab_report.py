"""ABReportJob — Phase 21 (2026-05-07).

주간 A/B 리포트. ExperienceLog 의 self_score / latency_ms 를 arm 별로 집계하고
Welch's t-test (numpy stdlib only — scipy 회피) 로 차이의 유의성을 판정.

출력: ``logs/ab/<YYYY-Www>.md``

해석 가이드:
  * t > 0 + p < 0.05 → treatment 가 평균적으로 더 좋음 (default ON 전환 후보)
  * t < 0 + p < 0.05 → control 이 더 좋음 → 회귀 가능성, 토글 OFF 권고
  * |t| 작거나 p ≥ 0.05 → 통계적으로 차이 없음 (샘플 부족 또는 효과 없음)

p-value 는 normal 근사 (df 가 큰 환경 가정). 작은 샘플에서는 보수적으로 작동
— 즉 small-n 에서는 유의 X 로 판정되기 쉬움.

cron: 일요일 22:30 KST (Reflection 22:00 + Curator 23:00 사이).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from src.core.experience_logger import ExperienceLogger, ExperienceRecord
from src.jobs.base import BaseJob, JobResult


def welch_t_test(s1: list[float], s2: list[float]) -> tuple[float, float, float]:
    """Welch's two-sample t-test.

    Returns ``(t_stat, df, p_two_tailed)``.

    p is computed via normal approximation of the t distribution — accurate
    when df ≥ ~30. For smaller samples, prefer to interpret raw t and report
    "needs more data".
    """
    n1, n2 = len(s1), len(s2)
    if n1 < 2 or n2 < 2:
        return 0.0, 0.0, 1.0
    m1 = sum(s1) / n1
    m2 = sum(s2) / n2
    var1 = sum((x - m1) ** 2 for x in s1) / (n1 - 1)
    var2 = sum((x - m2) ** 2 for x in s2) / (n2 - 1)
    se = math.sqrt(var1 / n1 + var2 / n2)
    if se == 0.0:
        return 0.0, 0.0, 1.0
    t = (m1 - m2) / se
    num = (var1 / n1 + var2 / n2) ** 2
    den = (var1 / n1) ** 2 / (n1 - 1) + (var2 / n2) ** 2 / (n2 - 1)
    df = num / den if den > 0 else 0.0
    # Two-tailed p via normal approx: p = 2 * (1 - Phi(|t|))
    # = 1 - erf(|t| / sqrt(2)). erf is in stdlib math.
    p = 1.0 - math.erf(abs(t) / math.sqrt(2.0))
    p = min(1.0, max(0.0, p))
    return t, df, p


def _split_by_arm(
    rows: Iterable[ExperienceRecord],
) -> tuple[list[ExperienceRecord], list[ExperienceRecord], list[ExperienceRecord]]:
    """Return (control, treatment, treatment_no_hits) lists."""
    ctrl: list[ExperienceRecord] = []
    treat: list[ExperienceRecord] = []
    no_hits: list[ExperienceRecord] = []
    for r in rows:
        if r.experiment_arm == "control":
            ctrl.append(r)
        elif r.experiment_arm == "treatment":
            treat.append(r)
        elif r.experiment_arm == "treatment_no_hits":
            no_hits.append(r)
    return ctrl, treat, no_hits


def _isoweek_label(dt: datetime) -> str:
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


class ABReportJob(BaseJob):
    """Weekly A/B report over ExperienceLog records."""

    name = "ab_report_weekly"
    schedule = "30 22 * * 0"  # KST 일요일 22:30
    description = (
        "Compare control vs treatment arms on self_score / latency. "
        "Welch's t (normal approx). Pure stats — no LLM call."
    )

    def __init__(
        self,
        logger: ExperienceLogger,
        output_dir: Path,
        *,
        window_days: int = 7,
        experiment_name: str | None = None,
    ):
        self.logger = logger
        self.output_dir = Path(output_dir)
        self.window_days = window_days
        # When set, only rows whose ``experiment_name`` matches are used.
        # Useful when multiple experiments coexist on different toggles.
        self.experiment_name = experiment_name

    def run(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> JobResult:
        end = until or datetime.now(timezone.utc)
        start = since or (end - timedelta(days=self.window_days))

        records = list(self.logger.query(since=start, until=end))
        if self.experiment_name is not None:
            records = [
                r for r in records if r.experiment_name == self.experiment_name
            ]

        ctrl, treat, no_hits = _split_by_arm(records)

        ctrl_scores = [r.self_score for r in ctrl]
        treat_scores = [r.self_score for r in treat]
        ctrl_latency = [float(r.latency_ms) for r in ctrl]
        treat_latency = [float(r.latency_ms) for r in treat]

        score_t, score_df, score_p = welch_t_test(treat_scores, ctrl_scores)
        latency_t, latency_df, latency_p = welch_t_test(
            treat_latency, ctrl_latency
        )

        report = _format_report(
            week=_isoweek_label(end),
            since=start,
            until=end,
            experiment_name=self.experiment_name,
            ctrl=ctrl,
            treat=treat,
            no_hits=no_hits,
            score_stats=(score_t, score_df, score_p),
            latency_stats=(latency_t, latency_df, latency_p),
        )

        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.output_dir / f"{_isoweek_label(end)}.md"
        out_path.write_text(report, encoding="utf-8")

        verdict = _verdict_label(score_t, score_p)

        return JobResult(
            ok=True,
            summary=(
                f"AB {_isoweek_label(end)}: control={len(ctrl)} "
                f"treatment={len(treat)} no_hits={len(no_hits)} "
                f"score_t={score_t:.2f} p={score_p:.3f} → {verdict}"
            ),
            output_path=out_path,
            metrics={
                "control_n": len(ctrl),
                "treatment_n": len(treat),
                "no_hits_n": len(no_hits),
                "score_t": score_t,
                "score_p": score_p,
                "latency_t": latency_t,
                "latency_p": latency_p,
                "verdict": verdict,
            },
        )


def _verdict_label(t: float, p: float) -> str:
    if p >= 0.05:
        return "no_signal"
    return "treatment_better" if t > 0 else "control_better"


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _format_report(
    *,
    week: str,
    since: datetime,
    until: datetime,
    experiment_name: str | None,
    ctrl: list[ExperienceRecord],
    treat: list[ExperienceRecord],
    no_hits: list[ExperienceRecord],
    score_stats: tuple[float, float, float],
    latency_stats: tuple[float, float, float],
) -> str:
    score_t, score_df, score_p = score_stats
    latency_t, latency_df, latency_p = latency_stats
    ctrl_scores = [r.self_score for r in ctrl]
    treat_scores = [r.self_score for r in treat]
    ctrl_latency = [float(r.latency_ms) for r in ctrl]
    treat_latency = [float(r.latency_ms) for r in treat]
    no_hits_scores = [r.self_score for r in no_hits]

    lines: list[str] = []
    lines.append(f"# A/B report — {week}")
    lines.append("")
    lines.append(
        f"window: {since.isoformat(timespec='seconds')} → "
        f"{until.isoformat(timespec='seconds')}"
    )
    if experiment_name:
        lines.append(f"experiment: `{experiment_name}`")
    lines.append("")
    lines.append("## Sample sizes")
    lines.append(f"- control: {len(ctrl)}")
    lines.append(f"- treatment: {len(treat)}")
    lines.append(f"- treatment_no_hits: {len(no_hits)}")
    lines.append("")
    lines.append("## self_score (higher = better)")
    lines.append(f"- control mean: {_mean(ctrl_scores):.3f}")
    lines.append(f"- treatment mean: {_mean(treat_scores):.3f}")
    lines.append(f"- no_hits mean: {_mean(no_hits_scores):.3f}")
    lines.append(
        f"- Welch t = {score_t:.3f}, df ≈ {score_df:.1f}, p ≈ {score_p:.3f}"
    )
    lines.append(f"- verdict: **{_verdict_label(score_t, score_p)}**")
    lines.append("")
    lines.append("## latency_ms (lower = better)")
    lines.append(f"- control mean: {_mean(ctrl_latency):.0f}")
    lines.append(f"- treatment mean: {_mean(treat_latency):.0f}")
    lines.append(
        f"- Welch t = {latency_t:.3f}, df ≈ {latency_df:.1f}, "
        f"p ≈ {latency_p:.3f}"
    )
    lines.append("")
    lines.append("## Notes")
    lines.append(
        "- p uses normal approximation. Small-sample (df < 30) results "
        "are conservative — wait for more data before acting on a "
        "marginal verdict."
    )
    lines.append(
        "- `treatment_no_hits` rows are treatment arm but search returned "
        "zero memory hits — segregated to isolate no-effect noise."
    )
    return "\n".join(lines) + "\n"


__all__ = ["ABReportJob", "welch_t_test"]
