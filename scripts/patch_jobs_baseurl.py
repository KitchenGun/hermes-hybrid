#!/usr/bin/env python3
"""Patch jobs.json base_url with the resolved OPENAI_BASE_URL value.

Why this exists: ``register_cron_jobs.py`` writes the literal
``${OPENAI_BASE_URL}`` string into ``jobs.json`` because that's what the
profile config.yaml carries. Hermes' cron scheduler reads jobs.json *raw*
without env expansion, so the request goes out to the literal endpoint
"${OPENAI_BASE_URL}" and connection-errors. Patching jobs.json with the
already-resolved URL (the same one that the per-profile .env carries
after ``refresh_ollama_base_urls.sh`` runs) closes the gap.

Run after ``register_cron_jobs.py`` and after every boot — WSL's host IP
can shift across reboots so the URL is rewritten every time.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROFILES = ["calendar_ops", "kk_job"]
HERMES_HOME = Path("/home/kang/.hermes")


def read_base_url(profile: str) -> str:
    env_file = HERMES_HOME / "profiles" / profile / ".env"
    if not env_file.exists():
        return ""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("OPENAI_BASE_URL="):
            return line.split("=", 1)[1].strip()
    return ""


def patch(profile: str, url: str) -> int:
    jobs_path = HERMES_HOME / "profiles" / profile / "cron" / "jobs.json"
    if not jobs_path.exists():
        return 0
    data = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return 0
    n = 0
    for j in jobs:
        if not isinstance(j, dict):
            continue
        j["base_url"] = url
        n += 1
    jobs_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return n


def main() -> int:
    for prof in PROFILES:
        url = read_base_url(prof)
        if not url:
            print(f"{prof}: OPENAI_BASE_URL not in .env — skip")
            continue
        n = patch(prof, url)
        print(f"{prof}: patched {n} jobs with base_url={url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
