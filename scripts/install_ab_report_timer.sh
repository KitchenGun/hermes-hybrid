#!/usr/bin/env bash
# install_ab_report_timer.sh — systemd-user timer for the weekly ABReportJob.
#
# Schedules ``scripts/ab_report_job.py`` to run every Sunday at 22:30 in
# the system's local timezone (KST on the operator's WSL host). 30 min
# after Reflection (22:00) and 30 min before Curator (23:00) so the three
# share a coherent data window without contending on the experience log.
#
# Output:
#   - logs/ab/<YYYY-Www>.md  (Welch's t verdict over the 7-day window)
#
# Idempotent. Re-running overwrites the unit + timer with the latest
# template and reloads systemd-user.

set -euo pipefail

REPO_ROOT="/mnt/e/hermes-hybrid"
PYTHON_BIN="/usr/bin/python3"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
UNIT_NAME="hermes-hybrid-ab-report"

if [[ ! -f "$REPO_ROOT/scripts/ab_report_job.py" ]]; then
    echo "[install_ab_report_timer] ERROR: $REPO_ROOT/scripts/ab_report_job.py not found." >&2
    echo "  Adjust REPO_ROOT in this script if the repo lives elsewhere." >&2
    exit 1
fi

mkdir -p "$SYSTEMD_USER_DIR"

cat > "$SYSTEMD_USER_DIR/$UNIT_NAME.service" <<UNIT
[Unit]
Description=Hermes-Hybrid weekly A/B report (Welch's t over arm-tagged rows)
After=network.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_ROOT
EnvironmentFile=-$REPO_ROOT/.env
ExecStart=$PYTHON_BIN $REPO_ROOT/scripts/ab_report_job.py
StandardOutput=journal
StandardError=journal
UNIT

cat > "$SYSTEMD_USER_DIR/$UNIT_NAME.timer" <<UNIT
[Unit]
Description=Run weekly A/B report every Sunday 22:30 KST

[Timer]
OnCalendar=Sun 22:30:00
Persistent=false
Unit=$UNIT_NAME.service

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user reset-failed "$UNIT_NAME.timer" 2>/dev/null || true
systemctl --user enable --now "$UNIT_NAME.timer"

echo "[install_ab_report_timer] installed $UNIT_NAME.timer"
echo "  next firing:"
systemctl --user list-timers "$UNIT_NAME.timer" --no-pager 2>&1 | sed 's/^/    /'
echo
echo "  manual run:        systemctl --user start $UNIT_NAME.service"
echo "  inspect last run:  journalctl --user -u $UNIT_NAME.service -n 50"
echo "  output:            $REPO_ROOT/logs/ab/<YYYY-Www>.md"
