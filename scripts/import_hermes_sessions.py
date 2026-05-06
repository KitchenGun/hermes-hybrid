#!/usr/bin/env python3
"""Import hermes session JSONs into the experience log (Phase 2).

Hermes cron / watcher jobs run via the WSL scheduler and bypass the
Orchestrator entirely. Without this importer their experience never
shows up in reflection / curator reports. Run hourly via systemd-user
timer (see scripts/install_session_importer_timer.sh).

Usage:
    python scripts/import_hermes_sessions.py
    python scripts/import_hermes_sessions.py --sessions-dir ~/.hermes/sessions
    python scripts/import_hermes_sessions.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
sys.path.insert(0, str(_REPO))

from src.config import Settings  # noqa: E402
from src.core import ExperienceLogger, import_sessions  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        default=None,
        help=(
            "Directory holding hermes session_<id>.json files. Default tries: "
            "$HERMES_SESSIONS_DIR, then ~/.hermes/sessions, then HERMES_HOME/sessions."
        ),
    )
    parser.add_argument(
        "--trigger-type",
        default="cron",
        help="trigger_type to stamp on imported records (default: cron)",
    )
    parser.add_argument(
        "--experience-log-root",
        type=Path,
        default=None,
        help="Override experience_log_root for this run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write — just report what would be imported",
    )
    args = parser.parse_args()

    settings = Settings()

    # Resolve sessions_dir.
    if args.sessions_dir:
        sessions_dir = args.sessions_dir
    else:
        import os
        env_dir = os.environ.get("HERMES_SESSIONS_DIR")
        if env_dir:
            sessions_dir = Path(env_dir)
        else:
            # Phase 8/10 후 hermes_home Settings 필드 폐기 — 기본 경로
            # 만 검사 (사용자가 hermes CLI 를 별도로 깔았다면 ~/.hermes
            # 가 자연스러운 위치).
            sessions_dir = Path.home() / ".hermes" / "sessions"

    if not sessions_dir.exists():
        print(f"⚠️ sessions_dir not found: {sessions_dir}")
        print("   Set HERMES_SESSIONS_DIR or pass --sessions-dir.")
        return 0  # not an error — just nothing to do

    log_root = args.experience_log_root or settings.experience_log_root
    if not Path(log_root).is_absolute():
        log_root = _REPO / log_root

    if args.dry_run:
        print(f"[dry-run] would scan {sessions_dir}")
        print(f"[dry-run] would write to {log_root}")
        files = list(sessions_dir.rglob("session_*.json"))
        print(f"[dry-run] {len(files)} session JSONs found")
        return 0

    logger = ExperienceLogger(Path(log_root), enabled=True)
    metrics = import_sessions(
        sessions_dir, logger, trigger_type=args.trigger_type
    )
    print(
        f"imported={metrics['imported']} "
        f"skipped={metrics['skipped']} errors={metrics['errors']}"
    )
    if "state_path" in metrics:
        print(f"  state: {metrics['state_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
