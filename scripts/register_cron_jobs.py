"""Register Hermes cron jobs with the CLI native scheduler.

Idempotent: reads existing jobs first and skips already-registered names.
Run from WSL: python3 /mnt/e/hermes-hybrid/scripts/register_cron_jobs.py
              python3 /mnt/e/hermes-hybrid/scripts/register_cron_jobs.py --profile kk_job
              python3 /mnt/e/hermes-hybrid/scripts/register_cron_jobs.py --profile all

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

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

PROFILES_ROOT = Path(__file__).resolve().parent.parent / "profiles"
DELIVER_MAP = {"webhook": "discord", "dm": "local"}

HERMES_HOME = Path("/home/kang/.hermes")

# Profiles that have at least one cron YAML and should be processed by --all.
CRON_PROFILES = ["calendar_ops", "kk_job", "journal_ops", "advisor_ops"]

# Job-specific Claude CLI model selection for "quiet" system_mode wrap.
# Analytical/heavy reasoning jobs get Sonnet; lightweight briefs/notifications
# get Haiku to stay under Max quota. Unknown job_name defaults to Haiku.
# 2026-05-04: replaces the silent-skip GUARD with a Claude CLI wrap so cron
# jobs keep running during games at $0 marginal (Max OAuth) cost.
JOB_QUIET_MODEL: dict[str, str] = {
    # advisor — multi-step inventory + web research + recommendation synthesis.
    "weekly_advisor_scan": "sonnet",
    # kk_job — site crawl + matching + report. Analytical.
    "morning_game_jobs": "sonnet",
    "weekly_job_digest": "sonnet",
    "deadline_reminder": "haiku",  # date check + Discord post; trivial.
    # calendar_ops — three categories.
    "morning_briefing": "haiku",      # list events + format. Simple.
    "weather_briefing": "haiku",      # OpenWeather call + format.
    "daily_wrap": "haiku",            # day summary. Light.
    "weekly_preview": "haiku",        # week ahead. Light.
    "focus_time_report": "sonnet",    # schedule analytics. Heavy.
    "monthly_pattern": "sonnet",      # month-long analysis. Heavy.
    "weekly_retrospective": "sonnet", # qualitative retro. Heavy.
}


def _claude_model_for(job_name: str) -> str:
    """Return Sonnet/Haiku for the given cron job name. Default Haiku."""
    return JOB_QUIET_MODEL.get(job_name, "haiku")


def _guard_prompt_for(job_name: str) -> str:
    """Build the per-job system_mode wrap guard.

    When system_mode is "quiet" (user is in-game, ollama OFF), the cron job's
    LLM step routes to Claude CLI (Max OAuth, $0 marginal) at the job-specific
    model tier. When "active", proceeds with the YAML's normal flow.
    """
    model = _claude_model_for(job_name)
    return f"""=== Step 0. system_mode 가드 (Claude CLI wrap) ===
terminal: python3 /mnt/e/hermes-hybrid/scripts/system_mode.py get

결과가 "quiet" 이면 (사용자가 게임 중 — ollama OFF 상태):
  이 잡 본문의 LLM 추론을 Claude CLI로 직접 호출 (Max OAuth, $0).
  예시:
    claude -p "<task body — JSON 결과만 출력>" \\
      --model {model} \\
      --output-format json
  Claude CLI 결과를 받아 본문의 후처리 단계(MCP/terminal/Discord/Kanban)는
  그대로 진행. 즉 ollama 호출만 Claude CLI로 대체, 외부 도구/IO는 동일.

결과가 "active" 이면 이하 본문의 ollama 흐름 그대로 진행.

"""


def inject_guard(prompt: str, job_name: str) -> str:
    """Prepend the system_mode wrap guard to a cron prompt. Empty prompt
    passes through. Idempotent: if guard already present, returns as-is."""
    if not prompt:
        return prompt
    if "Step 0. system_mode 가드" in prompt:
        return prompt
    return _guard_prompt_for(job_name) + prompt


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
    prompt = inject_guard(job.get("prompt", "").strip(), name)
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


def sync_jobs_prompts(profile: str, yaml_jobs: list[dict[str, Any]]) -> int:
    """Re-sync each registered job's prompt from its source YAML.

    Hermes' `hermes -p ... cron create ...` only sets the prompt at
    creation time. Editing the cron YAML afterwards has no effect on
    the runtime jobs.json. This routine reconciles them: for every
    YAML job, if jobs.json's prompt drifted, replace it with the
    YAML's. Lets `register_cron_jobs.py` double as a "git pull, then
    run me" sync command.
    """
    jobs_path = HERMES_HOME / "profiles" / profile / "cron" / "jobs.json"
    if not jobs_path.exists():
        return 0
    data = json.loads(jobs_path.read_text(encoding="utf-8"))
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return 0
    by_name = {j["name"]: j for j in jobs if isinstance(j, dict) and "name" in j}
    patched = 0
    for y in yaml_jobs:
        name = y.get("name")
        new_prompt = inject_guard((y.get("prompt") or "").strip(), name or "")
        if not name or not new_prompt:
            continue
        target = by_name.get(name)
        if target is None:
            continue
        if (target.get("prompt") or "").strip() != new_prompt:
            target["prompt"] = new_prompt
            patched += 1
    if patched:
        jobs_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return patched


def sync_profile(profile: str) -> int:
    """Sync a single profile's cron YAML to jobs.json. Returns # new registered."""
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
        print(f"\n[cron] WARN: profile '{profile}' model config empty — skipping patch.")

    # Sync prompts from current YAML files into jobs.json — Hermes only
    # honors the prompt at create-time, so YAML edits after registration
    # would otherwise drift. This makes register_cron_jobs.py the single
    # idempotent command for both new jobs and prompt updates.
    prompt_patched = sync_jobs_prompts(profile, jobs)
    if prompt_patched:
        print(f"[cron] Re-synced {prompt_patched} prompt(s) from YAML to jobs.json.")

    print(f"[cron] '{profile}' done — {registered} new job(s) registered, "
          f"{len(jobs) - registered} skipped.\n")
    return registered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--profile",
        default="calendar_ops",
        help=("Target profile name. Use 'all' to sync every profile in "
              f"CRON_PROFILES ({', '.join(CRON_PROFILES)}). Default: calendar_ops."),
    )
    args = parser.parse_args()

    targets = CRON_PROFILES if args.profile == "all" else [args.profile]
    total = 0
    for p in targets:
        total += sync_profile(p)
    print(f"[cron] Total: {total} new job(s) registered across "
          f"{len(targets)} profile(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
