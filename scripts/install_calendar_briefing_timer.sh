#!/usr/bin/env bash
# install_calendar_briefing_timer.sh — systemd-user timer for the daily
# calendar briefing job.
#
# Schedules ``scripts/calendar_briefing_job.py`` to run every day at
# 07:30 (Asia/Seoul). The job fetches today's events from Google
# Calendar (calendars listed in GOOGLE_CALENDAR_IDS, default `primary`)
# and posts a single Discord webhook message.
#
# Idempotent: re-running overwrites the unit + timer with the latest
# template and reloads systemd-user.
#
# Run from WSL: bash /mnt/e/hermes-hybrid/scripts/install_calendar_briefing_timer.sh

set -euo pipefail

REPO_ROOT="/mnt/e/hermes-hybrid"
PYTHON_BIN="$REPO_ROOT/.venv-linux/bin/python"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
UNIT_NAME="hermes-hybrid-calendar-briefing"

if [[ ! -f "$REPO_ROOT/scripts/calendar_briefing_job.py" ]]; then
    echo "[install_calendar_briefing_timer] ERROR: $REPO_ROOT/scripts/calendar_briefing_job.py not found." >&2
    exit 1
fi

mkdir -p "$SYSTEMD_USER_DIR"

cat > "$SYSTEMD_USER_DIR/$UNIT_NAME.service" <<UNIT
[Unit]
Description=Hermes-Hybrid daily calendar briefing (Google Calendar -> Discord webhook)
After=network.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_ROOT
EnvironmentFile=-$REPO_ROOT/.env
ExecStart=$PYTHON_BIN $REPO_ROOT/scripts/calendar_briefing_job.py
StandardOutput=journal
StandardError=journal
UNIT

cat > "$SYSTEMD_USER_DIR/$UNIT_NAME.timer" <<UNIT
[Unit]
Description=Run calendar briefing daily at 07:30 KST

[Timer]
OnCalendar=*-*-* 07:30:00 Asia/Seoul
AccuracySec=30s
Persistent=true
Unit=$UNIT_NAME.service

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user reset-failed "$UNIT_NAME.timer" 2>/dev/null || true
systemctl --user enable --now "$UNIT_NAME.timer"

echo "[install_calendar_briefing_timer] installed $UNIT_NAME.timer"
echo "  next firing:"
systemctl --user list-timers "$UNIT_NAME.timer" --no-pager 2>&1 | sed 's/^/    /'
echo
echo "  manual run:        systemctl --user start $UNIT_NAME.service"
echo "  inspect last run:  journalctl --user -u $UNIT_NAME.service -n 50"
echo "  dry test:          $PYTHON_BIN $REPO_ROOT/scripts/calendar_briefing_job.py --dry-run -v"
echo "  one-time auth:     $PYTHON_BIN $REPO_ROOT/scripts/calendar_briefing_job.py --auth"
