#!/usr/bin/env python3
"""Run the CuratorJob once.

Aggregates handled_by / tool stats from the experience log into:
  - logs/curator/handled_by_stats.json   (machine-readable)
  - logs/curator/{YYYY-MM-DD}.md         (human-readable summary)

Usage:
    python scripts/curator_job.py
    python scripts/curator_job.py --window-days 7
    python scripts/curator_job.py --output-dir /tmp/curator
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
from src.jobs.curator_job import CuratorJob  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument(
        "--output-dir", type=Path, default=_REPO / "logs" / "curator"
    )
    parser.add_argument("--experience-log-root", type=Path, default=None)
    args = parser.parse_args()

    settings = Settings()
    log_root = args.experience_log_root or settings.experience_log_root
    if not Path(log_root).is_absolute():
        log_root = _REPO / log_root

    logger = ExperienceLogger(Path(log_root), enabled=True)
    job = CuratorJob(logger, args.output_dir, window_days=args.window_days)
    result = job.run()
    print(result.summary)
    if result.output_path:
        print(f"  → {result.output_path}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
