"""Quick read of jobs.json metadata (last_run / last_status / delivery_error) for every profile.

WSL: python3 /mnt/e/hermes-hybrid/scripts/check_jobs_meta.py
"""
from __future__ import annotations

import json
from pathlib import Path

HERMES_HOME = Path("/home/kang/.hermes")
PROFILES = ["calendar_ops", "kk_job", "advisor_ops"]


def main() -> int:
    for prof in PROFILES:
        p = HERMES_HOME / "profiles" / prof / "cron" / "jobs.json"
        if not p.exists():
            print(f"[{prof}] jobs.json missing")
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        jobs = data.get("jobs") if isinstance(data, dict) else data
        print(f"\n[{prof}] {len(jobs)} jobs")
        for j in jobs:
            last_run = j.get("last_run_at") or "-"
            if last_run != "-":
                last_run = last_run[:19]
            status = j.get("last_status") or "-"
            err = j.get("last_error") or ""
            dlv_err = (j.get("last_delivery_error") or "")[:90]
            model = j.get("model", "-")
            print(
                f"  {j.get('name'):<25} model={model:<28} "
                f"last_run={last_run}  status={status}"
            )
            if err:
                print(f"    err: {err[:200]}")
            if dlv_err:
                print(f"    dlv_err: {dlv_err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
