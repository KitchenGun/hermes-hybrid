#!/usr/bin/env python3
"""Run the weekly ReflectionJob once.

Usage:
    python scripts/reflection_job.py
    python scripts/reflection_job.py --window-days 14
    python scripts/reflection_job.py --output-dir /tmp/reflection
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from src.config import Settings  # noqa: E402
from src.core import ExperienceLogger  # noqa: E402
from src.jobs.reflection_job import ReflectionJob  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window-days",
        type=int,
        default=7,
        help="How many days of experience log to aggregate (default 7)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_REPO / "logs" / "reflection",
        help="Where to write the markdown report",
    )
    parser.add_argument(
        "--experience-log-root",
        type=Path,
        default=None,
        help="Override settings.experience_log_root for this run",
    )
    args = parser.parse_args()

    settings = Settings()
    log_root = args.experience_log_root or settings.experience_log_root
    if not Path(log_root).is_absolute():
        log_root = _REPO / log_root

    logger = ExperienceLogger(Path(log_root), enabled=True)
    job = ReflectionJob(
        logger, args.output_dir, window_days=args.window_days
    )
    result = job.run()
    print(result.summary)
    if result.output_path:
        print(f"  → {result.output_path}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
