#!/usr/bin/env bash
# install_session_importer_timer.sh — hourly hermes session → ExperienceLog
#
# Why: cron / watcher jobs run via the Hermes scheduler and bypass the
# Orchestrator. Without this importer, the experience log only carries
# Discord-direct traffic, which is a tiny minority of the bot's actual
# work. ReflectionJob / CuratorJob become misleading.
#
# Schedule: every hour. The job is idempotent (state file dedups by
# session_id), so re-running on overlap is safe.
#
# Idempotent install. Re-run to refresh the unit + timer.
# Run from WSL: bash /mnt/e/hermes-hybrid/scripts/install_session_importer_timer.sh

set -euo pipefail

REPO_ROOT="/mnt/e/hermes-hybrid"
PYTHON_BIN="/usr/bin/python3"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
UNIT_NAME="hermes-hybrid-session-importer"

if [[ ! -f "$REPO_ROOT/scripts/import_hermes_sessions.py" ]]; then
    echo "[install] ERROR: $REPO_ROOT/scripts/import_hermes_sessions.py not found." >&2
    exit 1
fi

mkdir -p "$SYSTEMD_USER_DIR"

cat > "$SYSTEMD_USER_DIR/$UNIT_NAME.service" <<UNIT
[Unit]
Description=Hermes-Hybrid session importer (cron/watcher → ExperienceLog)
After=network.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_ROOT
EnvironmentFile=-$REPO_ROOT/.env
# HERMES_SESSIONS_DIR can be set in .env to point at a non-default
# sessions location; otherwise the script tries ~/.hermes/sessions
# and \$HERMES_HOME/sessions.
ExecStart=$PYTHON_BIN $REPO_ROOT/scripts/import_hermes_sessions.py
StandardOutput=journal
StandardError=journal
UNIT

cat > "$SYSTEMD_USER_DIR/$UNIT_NAME.timer" <<UNIT
[Unit]
Description=Run hermes session importer every hour

[Timer]
OnCalendar=hourly
Persistent=true
Unit=$UNIT_NAME.service

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user reset-failed "$UNIT_NAME.timer" 2>/dev/null || true
systemctl --user enable --now "$UNIT_NAME.timer"

echo "[install] enabled $UNIT_NAME.timer (hourly)"
echo "  manual:   systemctl --user start $UNIT_NAME.service"
echo "  status:   systemctl --user status $UNIT_NAME.service"
echo "  log:      journalctl --user -u $UNIT_NAME.service -n 50"
