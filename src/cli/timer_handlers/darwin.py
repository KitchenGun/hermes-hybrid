"""macOS timer handler — launchd plist for weekly jobs (Phase 19).

Writes ``~/Library/LaunchAgents/dev.hermes.<name>.plist`` then runs
``launchctl load`` to register. Cron expression maps to StartCalendarInterval
with Weekday=0 (Sunday).

We pick LaunchAgents (per-user) over LaunchDaemons (system-wide) — the
user's bot context is already a personal automation; daemons would need
sudo and surface unintended privileges.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_TASKS: tuple[tuple[str, int, int, str, tuple[str, ...]], ...] = (
    ("dev.hermes.reflection", 22, 0,  "scripts/reflection_job.py", ()),
    # Phase 21 (2026-05-07): weekly A/B report between Reflection and
    # Curator so the three share a coherent 7-day data window.
    ("dev.hermes.ab_report",  22, 30, "scripts/ab_report_job.py",  ()),
    ("dev.hermes.curator",    23, 0,  "scripts/curator_job.py",    ()),
    ("dev.hermes.promoter",   23, 30, "scripts/curator_job.py",    ("--skill-promote",)),
    # --- W3 growth-loop timer extensions ---
    ("dev.hermes.self_review",        21, 0,  "scripts/migration_self_review.py",       ()),
    ("dev.hermes.dialectic",           6, 0,  "scripts/dialectic_user_modeling.py",     ("--apply",)),
    ("dev.hermes.skill_self_modify",  23, 0,  "scripts/skill_self_modify.py",           ()),
    ("dev.hermes.delegation_pattern", 12, 0,  "scripts/delegation_pattern_extractor.py",("--apply",)),
    ("dev.hermes.skill_draft_queue_drainer", 0, 0, "scripts/process_skill_draft_queue.py", ("--apply",)),
    # --- end ---
)


def _plist(label: str, hour: int, minute: int, repo: Path, script: str, extra: tuple[str, ...]) -> str:
    args_xml = "".join(
        f"        <string>{v}</string>\n"
        for v in [str(repo / script), *extra]
    )
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
{args_xml}    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>0</integer>
        <key>Hour</key>
        <integer>{hour}</integer>
        <key>Minute</key>
        <integer>{minute}</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
"""


def _agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plan(repo: Path) -> list[list[str]]:
    """Returns the launchctl load commands per task."""
    out: list[list[str]] = []
    for label, hour, minute, _, _ in _TASKS:
        plist_path = _agents_dir() / f"{label}.plist"
        out.append(["launchctl", "load", "-w", str(plist_path)])
    return out


def register(repo: Path, *, ack: bool = True) -> list[str]:
    if not ack:
        return []
    agents_dir = _agents_dir()
    agents_dir.mkdir(parents=True, exist_ok=True)
    registered: list[str] = []
    for label, hour, minute, script, extra in _TASKS:
        path = agents_dir / f"{label}.plist"
        try:
            path.write_text(
                _plist(label, hour, minute, repo, script, extra),
                encoding="utf-8",
            )
        except OSError as e:
            _err(f"could not write {path}: {e}")
            continue

        try:
            r = subprocess.run(
                ["launchctl", "load", "-w", str(path)],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                registered.append(label)
            else:
                _err(f"launchctl load failed for {label}: {r.stderr.strip()}")
        except (OSError, subprocess.TimeoutExpired) as e:
            _err(f"launchctl invocation failed for {label}: {e}")
    return registered


def _err(msg: str) -> None:
    import sys
    sys.stderr.write("[hermes-setup:darwin] " + msg + "\n")


__all__ = ["plan", "register"]
