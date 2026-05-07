#!/usr/bin/env python3
"""Run the weekly ABReportJob once. Phase 21 (2026-05-07).

ExperienceLog 의 control vs treatment arm 별 self_score / latency 를
Welch's t 로 비교 → ``logs/ab/<YYYY-Www>.md`` 에 리포트 작성.

Usage:
    python scripts/ab_report_job.py
    python scripts/ab_report_job.py --window-days 14
    python scripts/ab_report_job.py --experiment-name memory_inject
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from src.config import Settings  # noqa: E402
from src.core import ExperienceLogger  # noqa: E402
from src.jobs.ab_report import ABReportJob  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-days", type=int, default=7)
    parser.add_argument(
        "--output-dir", type=Path, default=_REPO / "logs" / "ab",
    )
    parser.add_argument("--experience-log-root", type=Path, default=None)
    parser.add_argument(
        "--experiment-name", type=str, default=None,
        help="Filter rows by experiment_name. Defaults to settings value.",
    )
    args = parser.parse_args()

    settings = Settings()
    log_root = args.experience_log_root or settings.experience_log_root
    if not Path(log_root).is_absolute():
        log_root = _REPO / log_root

    output_dir = args.output_dir
    if not output_dir.is_absolute():
        output_dir = _REPO / output_dir

    name = args.experiment_name or settings.ab_experiment_name

    logger = ExperienceLogger(Path(log_root), enabled=True)
    job = ABReportJob(
        logger, output_dir,
        window_days=args.window_days,
        experiment_name=name,
    )
    result = job.run()
    print(result.summary)
    if result.output_path:
        print(f"  → {result.output_path}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
