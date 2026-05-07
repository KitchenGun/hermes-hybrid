#!/usr/bin/env bash
# install_mail_alert_timer.sh — systemd-user timer for the mail alert job.
#
# Schedules ``scripts/mail_alert_job.py`` to run every 5 minutes. The job
# polls each account in profiles/mail_ops/accounts.yaml, diffs against the
# checkpoint at state/mail_watcher/{account}.json, and posts a Discord
# webhook for each new INBOX message.
#
# Idempotent: re-running overwrites the unit + timer with the latest
# template and reloads systemd-user.
#
# Run from WSL: bash /mnt/e/hermes-hybrid/scripts/install_mail_alert_timer.sh

set -euo pipefail

REPO_ROOT="/mnt/e/hermes-hybrid"
PYTHON_BIN="$REPO_ROOT/.venv-linux/bin/python"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
UNIT_NAME="hermes-hybrid-mail-alert"

if [[ ! -f "$REPO_ROOT/scripts/mail_alert_job.py" ]]; then
    echo "[install_mail_alert_timer] ERROR: $REPO_ROOT/scripts/mail_alert_job.py not found." >&2
    exit 1
fi

mkdir -p "$SYSTEMD_USER_DIR"

cat > "$SYSTEMD_USER_DIR/$UNIT_NAME.service" <<UNIT
[Unit]
Description=Hermes-Hybrid mail watcher (Gmail + Naver -> Discord webhook)
After=network.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_ROOT
EnvironmentFile=-$REPO_ROOT/.env
ExecStart=$PYTHON_BIN $REPO_ROOT/scripts/mail_alert_job.py
StandardOutput=journal
StandardError=journal
UNIT

cat > "$SYSTEMD_USER_DIR/$UNIT_NAME.timer" <<UNIT
[Unit]
Description=Run mail watcher every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=30s
Persistent=false
Unit=$UNIT_NAME.service

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user reset-failed "$UNIT_NAME.timer" 2>/dev/null || true
systemctl --user enable --now "$UNIT_NAME.timer"

echo "[install_mail_alert_timer] installed $UNIT_NAME.timer"
echo "  next firing:"
systemctl --user list-timers "$UNIT_NAME.timer" --no-pager 2>&1 | sed 's/^/    /'
echo
echo "  manual run:        systemctl --user start $UNIT_NAME.service"
echo "  inspect last run:  journalctl --user -u $UNIT_NAME.service -n 50"
echo "  dry test:          $PYTHON_BIN $REPO_ROOT/scripts/mail_alert_job.py --dry-run -v"
