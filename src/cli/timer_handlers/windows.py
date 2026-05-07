"""Windows timer handler — schtasks WEEKLY tasks (Phase 19, 2026-05-07).

Three tasks:
  HermesReflection — Sun 22:00, runs scripts/reflection_job.py
  HermesCurator    — Sun 23:00, runs scripts/curator_job.py
  HermesPromoter   — Sun 23:30, runs scripts/curator_job.py --skill-promote

We do NOT pass /RU SYSTEM — that requires Admin and surprises users. The
current-user account is fine for a personal automation. If schtasks fails
(permission, schedule conflict), we surface the stderr to the user but
do not abort the rest of the batch.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

# (TaskName, ScheduleTime "HH:MM", relative script path, extra args)
_TASKS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("HermesReflection", "22:00", "scripts/reflection_job.py", ()),
    # Phase 21 (2026-05-07): A/B report between Reflection and Curator
    # so all three share the same 7-day data window.
    ("HermesABReport",   "22:30", "scripts/ab_report_job.py",  ()),
    ("HermesCurator",    "23:00", "scripts/curator_job.py",    ()),
    ("HermesPromoter",   "23:30", "scripts/curator_job.py",    ("--skill-promote",)),
)


def _python_exe() -> str:
    # Use the python.exe currently running — it sits in the venv if any.
    import sys
    return sys.executable


def _command(repo: Path, script: str, extra: tuple[str, ...]) -> str:
    parts = [f'"{_python_exe()}"', f'"{repo / script}"', *extra]
    return " ".join(parts)


def plan(repo: Path) -> list[list[str]]:
    """Return the schtasks /Create commands that register would execute."""
    plans: list[list[str]] = []
    for task_name, schedule_time, script, extra in _TASKS:
        plans.append([
            "schtasks", "/Create",
            "/SC", "WEEKLY",
            "/D", "SUN",
            "/ST", schedule_time,
            "/TN", task_name,
            "/TR", _command(repo, script, extra),
            "/F",                                # overwrite if exists
        ])
    return plans


def register(repo: Path, *, ack: bool = True) -> list[str]:
    """Execute schtasks /Create for each task. Returns names successfully
    registered. Raises nothing — failures get printed and skipped.
    """
    if not ack:
        return []
    registered: list[str] = []
    for cmd in plan(repo):
        task_name = cmd[cmd.index("/TN") + 1]
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                registered.append(task_name)
            else:
                _err(f"schtasks /Create failed for {task_name}: {r.stderr.strip()}")
        except (OSError, subprocess.TimeoutExpired) as e:
            _err(f"schtasks invocation failed for {task_name}: {e}")
    return registered


def _err(msg: str) -> None:
    import sys
    sys.stderr.write("[hermes-setup:windows] " + msg + "\n")


__all__ = ["plan", "register"]
