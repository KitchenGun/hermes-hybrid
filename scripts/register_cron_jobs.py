"""Register calendar_ops cron jobs with the Hermes CLI native scheduler.

Idempotent: reads existing jobs first and skips already-registered names.
Run from WSL: python3 /mnt/e/hermes-hybrid/scripts/register_cron_jobs.py

Why we patch jobs.json after `hermes cron create`:

The CLI doesn't expose --model/--provider flags (per `hermes cron create
--help`), so jobs are registered with model=null/provider=null. The
runtime is supposed to fall back to the profile's `model:` config block,
but in practice the resolution leaks an empty string into the chat
completion request body and OpenAI rejects with HTTP 400 ("you must
provide a model parameter"). Workaround: after creating each job via
the CLI, open jobs.json and fill in model / provider / base_url from
the profile's config.yaml.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

PROFILES_ROOT = Path(__file__).resolve().parent.parent / "profiles"
DELIVER_MAP = {"webhook": "discord", "dm": "local"}

HERMES_HOME = Path("/home/kang/.hermes")


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


def load_profile_model_config(profile: str) -> dict[str, Any]:
    """Read the profile's primary model block from config.yaml.

    Returns {model, provider, base_url} dict. Empty values stay empty.
    """
    cfg_path = HERMES_HOME / "profiles" / profile / "config.yaml"
    if not cfg_path.exists():
        # Fall back to the repo-side config (Windows mount may shadow this).
        cfg_path = PROFILES_ROOT / profile / "config.yaml"
    if not cfg_path.exists():
        return {}
    with cfg_path.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    m = cfg.get("model") or {}
    return {
        "model": m.get("model") or "",
        "provider": m.get("provider") or "",
        "base_url": m.get("base_url") or "",
    }


def patch_jobs_model_fields(profile: str, model_cfg: dict[str, Any]) -> int:
    """Fill in model/provider/base_url on every job entry that has them empty.

    Returns the number of jobs patched. Idempotent — leaves jobs that
    already have a non-empty model untouched.
    """
    jobs_path = HERMES_HOME / "profiles" / profile / "cron" / "jobs.json"
    if not jobs_path.exists():
        return 0
    data = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return 0
    patched = 0
    for j in jobs:
        if not isinstance(j, dict):
            continue
        # Only fill when the existing value is missing / empty — never
        # overwrite an explicit per-job override.
        if not j.get("model") and model_cfg.get("model"):
            j["model"] = model_cfg["model"]
            patched += 1
        if not j.get("provider") and model_cfg.get("provider"):
            j["provider"] = model_cfg["provider"]
        if not j.get("base_url") and model_cfg.get("base_url"):
            j["base_url"] = model_cfg["base_url"]
    if patched:
        jobs_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return patched


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

    # Workaround: hermes cron create doesn't accept --model/--provider,
    # so jobs are registered with model=null. Patch them in-place from
    # the profile's config.yaml model block.
    model_cfg = load_profile_model_config(profile)
    if model_cfg.get("model"):
        patched = patch_jobs_model_fields(profile, model_cfg)
        if patched:
            print(
                f"\n[cron] Patched {patched} job(s) with model="
                f"{model_cfg['model']!r}, provider={model_cfg['provider']!r}."
            )
    else:
        print("\n[cron] WARN: profile model config empty — skipping patch.")

    print(f"\n[cron] Done — {registered} new job(s) registered, "
          f"{len(jobs) - registered} skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
