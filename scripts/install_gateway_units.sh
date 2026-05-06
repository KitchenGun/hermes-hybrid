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

# ── 1. calendar_ops gateway: TTY-wrapper override + EnvironmentFile (unit
#       자체는 이전 setup 에서 수동으로 배치돼 그대로 두고, drop-in 으로
#       ExecStart 와 .env 로딩만 덧입힌다).
mkdir -p "$SYSTEMD_USER_DIR/hermes-gateway-calendar_ops.service.d"
mkdir -p "$PROFILES_HOME/calendar_ops/logs"
cat > "$SYSTEMD_USER_DIR/hermes-gateway-calendar_ops.service.d/override.conf" <<'OVR'
[Service]
# 글로벌 + profile-scoped .env 둘 다 inject. systemd-user 는 사용자
# .bashrc 를 안 읽으므로 이게 없으면 ``${OPENAI_BASE_URL}`` 같은 변수가
# 빈 값으로 expand 되어 cron LLM 호출이 connection error 로 죽는다.
EnvironmentFile=-/home/kang/.hermes/.env
EnvironmentFile=-/home/kang/.hermes/profiles/calendar_ops/.env
# DISCORD_BOT_TOKEN 은 hermes-hybrid 봇(Windows) 이 단일 소유. EnvironmentFile
# 로 hermes gateway 까지 들어가면 ``Gateway hit a non-retryable startup
# conflict: discord: Discord bot token already in use`` 로 cron 게이트웨이가
# crashloop 한다. Environment= 가 EnvironmentFile= 보다 나중에 적용되므로
# 빈 값으로 override 해서 gateway 가 Discord platform 자체를 활성화 못 하게.
Environment=DISCORD_BOT_TOKEN=
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
# 글로벌 + profile-scoped .env 를 둘 다 inject 한다. 글로벌은 ANTHROPIC_TOKEN
# / CLAUDE_CODE_OAUTH_TOKEN / 기본 webhook 등을, profile 은 OPENAI_BASE_URL
# (WSL→Windows host IP 가 매 부팅 변동) 같은 로컬 값을 담는다. systemd-user
# 는 사용자 .bashrc 를 안 읽으므로 EnvironmentFile= 가 없으면 ``provider:
# custom`` + ``base_url: \${OPENAI_BASE_URL}`` 가 빈 값으로 expand 돼 모든
# cron LLM 호출이 ``Endpoint: \${OPENAI_BASE_URL}`` 그대로 connection error.
EnvironmentFile=-/home/kang/.hermes/.env
EnvironmentFile=-/home/kang/.hermes/profiles/kk_job/.env
# DISCORD_BOT_TOKEN 충돌 회피 — calendar_ops override 와 동일 사유.
Environment=DISCORD_BOT_TOKEN=
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

# ── 3. advisor_ops gateway: full unit + TTY-wrapper override.
#       advisor_ops 의 cron scheduler 가 자동 tick 하려면 그 profile 의
#       gateway 프로세스가 떠 있어야 한다. 이게 없으면 weekly_advisor_scan
#       이 last_run_at=null 인 채로 영원히 대기. 패턴은 kk_job 과 동일.
cat > "$SYSTEMD_USER_DIR/hermes-gateway-advisor_ops.service" <<UNIT
[Unit]
Description=Hermes Agent Gateway - advisor_ops profile
After=network.target
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
ExecStart=$HERMES_VENV/bin/python -m hermes_cli.main --profile advisor_ops gateway run --replace
WorkingDirectory=$HERMES_AGENT
EnvironmentFile=-/home/kang/.hermes/.env
EnvironmentFile=-/home/kang/.hermes/profiles/advisor_ops/.env
# DISCORD_BOT_TOKEN 충돌 회피 — calendar_ops override 와 동일 사유.
Environment=DISCORD_BOT_TOKEN=
Environment="PATH=$HERMES_VENV/bin:$HERMES_AGENT/node_modules/.bin:/home/kang/.hermes/node/bin:/home/kang/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Environment="VIRTUAL_ENV=$HERMES_VENV"
Environment="HERMES_HOME=$PROFILES_HOME/advisor_ops"
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

mkdir -p "$SYSTEMD_USER_DIR/hermes-gateway-advisor_ops.service.d"
mkdir -p "$PROFILES_HOME/advisor_ops/logs"
cat > "$SYSTEMD_USER_DIR/hermes-gateway-advisor_ops.service.d/override.conf" <<'OVR'
[Service]
ExecStart=
ExecStart=/usr/bin/script -qfc "/home/kang/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main --profile advisor_ops gateway run --replace" /home/kang/.hermes/profiles/advisor_ops/logs/gateway.log
OVR

# ── 4. Reload + enable.
systemctl --user daemon-reload
systemctl --user reset-failed hermes-gateway-calendar_ops.service 2>/dev/null || true
systemctl --user reset-failed hermes-gateway-kk_job.service 2>/dev/null || true
systemctl --user reset-failed hermes-gateway-advisor_ops.service 2>/dev/null || true
systemctl --user enable --now hermes-gateway-calendar_ops.service
systemctl --user enable --now hermes-gateway-kk_job.service
systemctl --user enable --now hermes-gateway-advisor_ops.service

# ── 5. Status report.
for svc in hermes-gateway-calendar_ops hermes-gateway-kk_job hermes-gateway-advisor_ops; do
    state=$(systemctl --user is-active "$svc.service" 2>&1 || true)
    echo "[install_gateway_units] $svc: $state"
done
