#!/usr/bin/env bash
# install_curator_timer.sh — systemd-user timer for the weekly CuratorJob.
#
# Schedules ``scripts/curator_job.py`` to run every Sunday at 23:00 in
# the system's local timezone (assumed KST on the operator's WSL host).
# That's intentionally one hour AFTER the reflection timer so the curator
# always sees the same data window — both look at last 7 days, but the
# curator runs after reflection has had a chance to flag patterns.
#
# Output:
#   - logs/curator/handled_by_stats.json   (machine-readable, latest snapshot)
#   - logs/curator/{YYYY-MM-DD}.md         (human-readable summary)
#
# Idempotent. Re-running overwrites the unit + timer with the latest
# template and reloads systemd-user.
#
# Run from WSL: bash /mnt/e/hermes-hybrid/scripts/install_curator_timer.sh

set -euo pipefail

REPO_ROOT="/mnt/e/hermes-hybrid"
PYTHON_BIN="/usr/bin/python3"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
UNIT_NAME="hermes-hybrid-curator"

if [[ ! -f "$REPO_ROOT/scripts/curator_job.py" ]]; then
    echo "[install_curator_timer] ERROR: $REPO_ROOT/scripts/curator_job.py not found." >&2
    echo "  Adjust REPO_ROOT in this script if the repo lives elsewhere." >&2
    exit 1
fi

mkdir -p "$SYSTEMD_USER_DIR"

# ── Service: oneshot. Reads ./logs/experience, writes ./logs/curator.
cat > "$SYSTEMD_USER_DIR/$UNIT_NAME.service" <<UNIT
[Unit]
Description=Hermes-Hybrid weekly curator (handled_by/tool stat aggregation)
After=network.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_ROOT
EnvironmentFile=-$REPO_ROOT/.env
ExecStart=$PYTHON_BIN $REPO_ROOT/scripts/curator_job.py
StandardOutput=journal
StandardError=journal
UNIT

# ── Timer: Sunday 23:00 local. Runs an hour after the reflection timer
#    so they share the same data window without contending on the file
#    system or experience log reader.
cat > "$SYSTEMD_USER_DIR/$UNIT_NAME.timer" <<UNIT
[Unit]
Description=Run weekly curator every Sunday 23:00 KST

[Timer]
OnCalendar=Sun 23:00:00
Persistent=false
Unit=$UNIT_NAME.service

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user reset-failed "$UNIT_NAME.timer" 2>/dev/null || true
systemctl --user enable --now "$UNIT_NAME.timer"

echo "[install_curator_timer] installed $UNIT_NAME.timer"
echo "  next firing:"
systemctl --user list-timers "$UNIT_NAME.timer" --no-pager 2>&1 | sed 's/^/    /'
echo
echo "  manual run:        systemctl --user start $UNIT_NAME.service"
echo "  inspect last run:  journalctl --user -u $UNIT_NAME.service -n 50"
echo "  output:            $REPO_ROOT/logs/curator/{YYYY-MM-DD}.md"
