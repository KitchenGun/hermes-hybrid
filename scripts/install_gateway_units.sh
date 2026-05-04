#!/usr/bin/env bash
# install_gateway_units.sh — Install/refresh hermes-gateway systemd-user units
# for calendar_ops and kk_job profiles. Idempotent.
#
# Why this exists as a separate script:
#   The previous in-line approach embedded multi-line heredocs inside
#   ``wsl -d Ubuntu -- bash -lc "..."`` calls in run_all.bat. cmd.exe does
#   not support newlines inside a double-quoted argument: it terminates
#   the command at the first line break and tries to execute every
#   subsequent line ([Service], Environment=, [Install], …) as a cmd
#   command, all of which fail with "is not recognized". Result: gateway
#   units silently never get installed/updated, and the rest of run_all.bat
#   either limps along on stale state or fails outright.
#
# Splitting the heredocs into a real .sh, invoked via a single-line
# ``wsl -d Ubuntu -- bash <path>`` call, sidesteps the cmd parser entirely.
#
# Each unit:
#   - Wraps ExecStart in `script -qfc` to give the gateway a pseudo-TTY
#     (Hermes gateway exits 1 without a controlling TTY).
#   - Streams stdout to <profile_logs>/gateway.log so we can grep it.
#   - Auto-restarts on failure but skips exit code 75 (graceful reload).

set -euo pipefail

HERMES_AGENT=/home/kang/.hermes/hermes-agent
HERMES_VENV="$HERMES_AGENT/venv"
PROFILES_HOME=/home/kang/.hermes/profiles
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

mkdir -p "$SYSTEMD_USER_DIR"

# ── 1. calendar_ops gateway: TTY-wrapper override only (unit was placed
#       manually in an earlier setup; we don't recreate it to avoid
#       overwriting any local edits the user made directly).
mkdir -p "$SYSTEMD_USER_DIR/hermes-gateway-calendar_ops.service.d"
mkdir -p "$PROFILES_HOME/calendar_ops/logs"
cat > "$SYSTEMD_USER_DIR/hermes-gateway-calendar_ops.service.d/override.conf" <<'OVR'
[Service]
ExecStart=
ExecStart=/usr/bin/script -qfc "/home/kang/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main --profile calendar_ops gateway run --replace" /home/kang/.hermes/profiles/calendar_ops/logs/gateway.log
OVR

# ── 2. kk_job gateway: full unit + TTY-wrapper override.
cat > "$SYSTEMD_USER_DIR/hermes-gateway-kk_job.service" <<UNIT
[Unit]
Description=Hermes Agent Gateway - kk_job profile
After=network.target
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
ExecStart=$HERMES_VENV/bin/python -m hermes_cli.main --profile kk_job gateway run --replace
WorkingDirectory=$HERMES_AGENT
Environment="PATH=$HERMES_VENV/bin:$HERMES_AGENT/node_modules/.bin:/home/kang/.hermes/node/bin:/home/kang/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="VIRTUAL_ENV=$HERMES_VENV"
Environment="HERMES_HOME=$PROFILES_HOME/kk_job"
Restart=on-failure
RestartSec=30
RestartForceExitStatus=75
KillMode=mixed
KillSignal=SIGTERM
ExecReload=/bin/kill -USR1 \$MAINPID
TimeoutStopSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
UNIT

mkdir -p "$SYSTEMD_USER_DIR/hermes-gateway-kk_job.service.d"
mkdir -p "$PROFILES_HOME/kk_job/logs"
cat > "$SYSTEMD_USER_DIR/hermes-gateway-kk_job.service.d/override.conf" <<'OVR'
[Service]
ExecStart=
ExecStart=/usr/bin/script -qfc "/home/kang/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main --profile kk_job gateway run --replace" /home/kang/.hermes/profiles/kk_job/logs/gateway.log
OVR

# ── 3. Reload + enable.
systemctl --user daemon-reload
systemctl --user reset-failed hermes-gateway-calendar_ops.service 2>/dev/null || true
systemctl --user reset-failed hermes-gateway-kk_job.service 2>/dev/null || true
systemctl --user enable --now hermes-gateway-calendar_ops.service
systemctl --user enable --now hermes-gateway-kk_job.service

# ── 4. Status report.
for svc in hermes-gateway-calendar_ops hermes-gateway-kk_job; do
    state=$(systemctl --user is-active "$svc.service" 2>&1 || true)
    echo "[install_gateway_units] $svc: $state"
done
