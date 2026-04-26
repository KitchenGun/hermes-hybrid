"""Register calendar_ops cron jobs with the Hermes CLI native scheduler.

Idempotent: reads existing jobs first and skips already-registered names.
Run from WSL: python3 /mnt/e/hermes-hybrid/scripts/register_cron_jobs.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

PROFILES_ROOT = Path(__file__).resolve().parent.parent / "profiles"
DELIVER_MAP = {"webhook": "discord", "dm": "local"}


def _wsl_run(cmd: list[str]) -> tuple[int, str]:
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, (result.stdout + result.stderr).strip()


def get_registered_names(profile: str) -> set[str]:
    """Parse 'hermes -p <profile> cron list' output for existing job names."""
    _, output = _wsl_run(["hermes", "-p", profile, "cron", "list"])
    names: set[str] = set()
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("Name:"):
            names.add(stripped.removeprefix("Name:").strip())
    return names


def load_jobs(profile: str) -> list[dict[str, Any]]:
    """Collect all cron YAML definitions for a profile."""
    cron_dir = PROFILES_ROOT / profile / "cron"
    jobs: list[dict[str, Any]] = []
    for yaml_file in sorted(cron_dir.rglob("*.yaml")):
        with yaml_file.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data and data.get("trigger", {}).get("type") == "cron":
            jobs.append(data)
    return jobs


def register_job(profile: str, job: dict[str, Any]) -> bool:
    name = job["name"]
    schedule = job["trigger"]["schedule"]
    prompt = job.get("prompt", "").strip()
    skills: list[str] = job.get("skills", [])
    channel = job.get("delivery", {}).get("channel", "webhook")
    deliver = DELIVER_MAP.get(channel, "discord")

    cmd = ["hermes", "-p", profile, "cron", "create", schedule, prompt,
           "--name", name, "--deliver", deliver]
    for skill in skills:
        cmd += ["--skill", skill]

    rc, out = _wsl_run(cmd)
    if rc == 0:
        print(f"  [+] registered: {name}  ({schedule})")
    else:
        print(f"  [!] failed:     {name}  — {out[:120]}", file=sys.stderr)
    return rc == 0


def main() -> int:
    profile = "calendar_ops"
    print(f"[cron] Syncing jobs for profile '{profile}'...")

    existing = get_registered_names(profile)
    if existing:
        print(f"  already registered: {', '.join(sorted(existing))}")

    jobs = load_jobs(profile)
    if not jobs:
        print("  no cron YAML files found.")
        return 0

    registered = 0
    for job in jobs:
        name = job.get("name", "")
        if name in existing:
            print(f"  [=] skip (exists): {name}")
            continue
        if register_job(profile, job):
            registered += 1

    print(f"\n[cron] Done — {registered} new job(s) registered, "
          f"{len(jobs) - registered} skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
