#!/usr/bin/env bash
# install_reflection_timer.sh — systemd-user timer for the weekly ReflectionJob.
#
# Schedules ``scripts/reflection_job.py`` to run every Sunday at 22:00
# in the system's local timezone (assumed to be KST on the operator's
# WSL host). The job reads ``logs/experience/{date}.jsonl``, reduces it
# to weekly stats, and writes ``logs/reflection/{ISO-YEAR}-W{WW}.md``.
#
# This is intentionally a separate script from ``install_gateway_units.sh``
# because:
#   * the reflection unit has zero dependency on Hermes gateway state
#     (no LLM call, no profile, no Discord token)
#   * gateway units run continuously; reflection is a oneshot timer
#   * keeping it separate lets an operator opt out of automation while
#     still installing the gateways
#
# Idempotent. Re-running overwrites the unit + timer with the latest
# template and reloads systemd-user.
#
# Run from WSL: bash /mnt/e/hermes-hybrid/scripts/install_reflection_timer.sh

set -euo pipefail

REPO_ROOT="/mnt/e/hermes-hybrid"
PYTHON_BIN="/usr/bin/python3"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
UNIT_NAME="hermes-hybrid-reflection"

if [[ ! -f "$REPO_ROOT/scripts/reflection_job.py" ]]; then
    echo "[install_reflection_timer] ERROR: $REPO_ROOT/scripts/reflection_job.py not found." >&2
    echo "  Adjust REPO_ROOT in this script if the repo lives elsewhere." >&2
    exit 1
fi

mkdir -p "$SYSTEMD_USER_DIR"

# ── Service: oneshot. Reads ./logs/experience, writes ./logs/reflection.
cat > "$SYSTEMD_USER_DIR/$UNIT_NAME.service" <<UNIT
[Unit]
Description=Hermes-Hybrid weekly reflection (statistical reduction over experience log)
After=network.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_ROOT
# Mirror the gateway pattern — pick up .env without depending on bash.
# The reflection job itself doesn't need most of these, but importing
# src.config.Settings does, so the .env load avoids spurious validation.
EnvironmentFile=-$REPO_ROOT/.env
ExecStart=$PYTHON_BIN $REPO_ROOT/scripts/reflection_job.py
StandardOutput=journal
StandardError=journal
UNIT

# ── Timer: Sunday 22:00 local. systemd-user honors the system timezone,
#    which on the operator's WSL is KST. If the WSL clock is UTC,
#    flip to ``Sun 13:00:00`` instead.
cat > "$SYSTEMD_USER_DIR/$UNIT_NAME.timer" <<UNIT
[Unit]
Description=Run weekly reflection every Sunday 22:00 KST

[Timer]
OnCalendar=Sun 22:00:00
# Persistent=false: don't replay missed runs after long suspends — the
# next Sunday's run picks up everything anyway, no point catching up.
Persistent=false
Unit=$UNIT_NAME.service

[Install]
WantedBy=timers.target
UNIT

systemctl --user daemon-reload
systemctl --user reset-failed "$UNIT_NAME.timer" 2>/dev/null || true
systemctl --user enable --now "$UNIT_NAME.timer"

echo "[install_reflection_timer] installed $UNIT_NAME.timer"
echo "  next firing:"
systemctl --user list-timers "$UNIT_NAME.timer" --no-pager 2>&1 | sed 's/^/    /'
echo
echo "  manual run:        systemctl --user start $UNIT_NAME.service"
echo "  inspect last run:  journalctl --user -u $UNIT_NAME.service -n 50"
echo "  output:            $REPO_ROOT/logs/reflection/{ISO-YEAR}-W{WW}.md"
