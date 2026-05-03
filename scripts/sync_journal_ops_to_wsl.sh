#!/bin/bash
# One-shot: copy edited journal_ops profile files from repo to WSL hermes profile dir.
set -euo pipefail
SRC=/mnt/e/hermes-hybrid/profiles/journal_ops
DST=$HOME/.hermes/profiles/journal_ops
cp "$SRC/SOUL.md"                                                  "$DST/SOUL.md"
cp "$SRC/on_demand/log_activity.yaml"                              "$DST/on_demand/log_activity.yaml"
cp "$SRC/config.yaml"                                              "$DST/config.yaml"
cp "$SRC/skills/storage/sheets_append/scripts/post_to_sheet.py"    "$DST/skills/storage/sheets_append/scripts/post_to_sheet.py"
echo "[sync] copied SOUL.md, log_activity.yaml, config.yaml, post_to_sheet.py to $DST"
ls -l "$DST/SOUL.md" "$DST/on_demand/log_activity.yaml" "$DST/config.yaml" "$DST/skills/storage/sheets_append/scripts/post_to_sheet.py"
