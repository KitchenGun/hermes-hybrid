#!/bin/bash
# Sync edited kk_job profile static files from repo (E: drive) to WSL hermes
# profile dir. Preserves WSL-side runtime data (state.db, sessions/, logs/,
# sandboxes/, .skills_prompt_snapshot.json) — those stay on WSL local fs for
# performance (per ARCHITECTURE.md: /mnt/e is slow under WSL).
#
# Usage (from WSL or via `wsl -e bash -c`):
#   bash /mnt/e/hermes-hybrid/scripts/sync_kk_job_to_wsl.sh
#
# Idempotent: rsync --update only copies when source mtime is newer.
set -euo pipefail
SRC=/mnt/e/hermes-hybrid/profiles/kk_job
DST=$HOME/.hermes/profiles/kk_job

if [ ! -d "$SRC" ]; then
  echo "[sync] ERROR: $SRC missing" >&2
  exit 1
fi
if [ ! -d "$DST" ]; then
  echo "[sync] ERROR: $DST missing — run bootstrap_profile.sh kk_job first" >&2
  exit 1
fi

# Static directory trees — code/config/skills/cron/watchers/memories/intents.
for sub in skills cron watchers memories on_demand; do
  if [ -d "$SRC/$sub" ]; then
    rsync -av --update "$SRC/$sub/" "$DST/$sub/" | tail -n +2
  fi
done

# Top-level static files.
for f in SOUL.md config.yaml intent_schema.json; do
  if [ -f "$SRC/$f" ]; then
    cp -uv "$SRC/$f" "$DST/$f"
  fi
done

echo "[sync] kk_job static files synced to $DST"
