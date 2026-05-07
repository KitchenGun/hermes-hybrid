#!/usr/bin/env bash
# install_weather_alert_timer.sh — systemd-user timer for the weather alert job.
#
# Schedules ``scripts/weather_alert.py`` to run every day at 07:10 KST. The job
# fetches KMA apihub getVilageFcst for nx/ny in .env, formats today's briefing,
# and posts a Discord webhook (DISCORD_WEATHER_WEBHOOK_URL).
#
# This replaces the legacy `weather_briefing` job from the pre-Phase-8 inventory
# (docs/JOB_INVENTORY.md), which had no runtime registration after Phase 8.
#
# Idempotent: re-running overwrites the unit + timer with the latest template
# and reloads systemd-user.
#
# Run from WSL: bash /mnt/e/hermes-hybrid/scripts/install_weather_alert_timer.sh

set -euo pipefail

REPO_ROOT="/mnt/e/hermes-hybrid"
PYTHON_BIN="$REPO_ROOT/.venv-linux/bin/python"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
UNIT_NAME="hermes-hybrid-weather-alert"

if [[ ! -f "$REPO_ROOT/scripts/weather_alert.py" ]]; then
    echo "[install_weather_alert_timer] ERROR: $REPO_ROOT/scripts/weather_alert.py not found." >&2
    exit 1
fi

mkdir -p "$SYSTEMD_USER_DIR"

cat > "$SYSTEMD_USER_DIR/$UNIT_NAME.service" <<UNIT
[Unit]
Description=Hermes-Hybrid weather briefing (KMA apihub -> Discord webhook)
After=network.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_ROOT
EnvironmentFile=-$REPO_ROOT/.env
ExecStart=$PYTHON_BIN $REPO_ROOT/scripts/weather_alert.py
StandardOutput=journal
StandardError=journal
UNIT

cat > "$SYSTEMD_USER_DIR/$UNIT_NAME.timer" <<UNIT
[Unit]
Description=Run weather briefing daily at 07:10 KST

[Timer]
OnCalendar=*-*-* 07:10:00
AccuracySec=30s
Persistent=true
Unit=$UNIT_NAME.service

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user reset-failed "$UNIT_NAME.timer" 2>/dev/null || true
systemctl --user enable --now "$UNIT_NAME.timer"

echo "[install_weather_alert_timer] installed $UNIT_NAME.timer"
echo "  next firing:"
systemctl --user list-timers "$UNIT_NAME.timer" --no-pager 2>&1 | sed 's/^/    /'
echo
echo "  manual run:        systemctl --user start $UNIT_NAME.service"
echo "  inspect last run:  journalctl --user -u $UNIT_NAME.service -n 50"
echo "  dry test:          $PYTHON_BIN $REPO_ROOT/scripts/weather_alert.py --dry-run -v"
