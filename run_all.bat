@echo off
setlocal
chcp 65001 >nul
title hermes-hybrid launcher

echo ==========================================
echo   hermes-hybrid full stack launcher
echo ==========================================
echo.

REM ---- 1. Ollama ----
echo [1/3] Starting Ollama server...
tasklist /FI "IMAGENAME eq ollama.exe" 2>nul | find /I "ollama.exe" >nul
if %errorlevel%==0 (
    echo        Ollama already running. Skipping.
) else (
    start "ollama-serve" /MIN cmd /c "ollama serve"
    echo        Launched in minimized window. Waiting 3s for warmup...
    timeout /t 3 /nobreak >nul
)
echo.

REM ---- 2. WSL warm-up + persistent session keep-alive ----
echo [2/5] Warming up WSL (Ubuntu)...
wsl -d Ubuntu -- echo "WSL ready"
if errorlevel 1 (
    echo        ERROR: WSL warm-up failed. Check that 'wsl -d Ubuntu' works.
    pause
    exit /b 1
)

REM Persistent WSL session — required workaround for microsoft/WSL#10205
REM where systemd-user dies whenever the last login session ends, even
REM with linger=yes. Without this, hermes-gateway dies seconds later.
echo        Spawning hidden WSL session keep-alive (microsoft/WSL#10205 workaround)...
start "hermes-wsl-keepalive" /B wsl -d Ubuntu --user kang -- bash -lc "while true; do sleep 60; done"
echo.

REM ---- 2.5. Gateway override (script -qfc pseudo-TTY) + enable ----
REM Hermes gateway exits with code 1 immediately when started without a
REM controlling TTY (per `hermes gateway --help`, "run is recommended for
REM WSL"). systemd by default has no TTY, so we wrap ExecStart in
REM `script -qfc` which provides a pseudo-TTY. Without this wrapper, the
REM gateway crash-loops at startup banner and cron tick never fires.
REM See ARCHITECTURE.md "Hermes runtime — gateway vs dashboard".
echo [2.5/5] Installing gateway TTY-wrapper override + enabling service...
wsl -d Ubuntu -- bash -lc "mkdir -p ~/.config/systemd/user/hermes-gateway-calendar_ops.service.d && cat > ~/.config/systemd/user/hermes-gateway-calendar_ops.service.d/override.conf <<'OVR'
[Service]
ExecStart=
ExecStart=/usr/bin/script -qfc \"/home/kang/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main --profile calendar_ops gateway run --replace\" /home/kang/.hermes/profiles/calendar_ops/logs/gateway.log
OVR
mkdir -p /home/kang/.hermes/profiles/calendar_ops/logs
systemctl --user daemon-reload
systemctl --user reset-failed hermes-gateway-calendar_ops.service 2>/dev/null
systemctl --user enable --now hermes-gateway-calendar_ops.service"
wsl -d Ubuntu -- bash -lc "systemctl --user is-active hermes-gateway-calendar_ops.service"
if errorlevel 1 (
    echo        WARN: gateway not active. Check: wsl -d Ubuntu -- journalctl --user -u hermes-gateway-calendar_ops -n 30
) else (
    echo        Gateway active — cron tick is alive.
)
echo.

REM ---- 2.6. kk_job gateway: same pattern (unit + TTY override + enable) ----
REM kk_job has its own gateway so morning_game_jobs cron (07:10 KST) can fire.
REM Unit file is created here too (calendar_ops's unit was placed manually);
REM cat > is idempotent so re-running run_all.bat is safe.
echo [2.6/5] Installing kk_job gateway service + override...
wsl -d Ubuntu -- bash -lc "cat > ~/.config/systemd/user/hermes-gateway-kk_job.service <<'UNIT'
[Unit]
Description=Hermes Agent Gateway - kk_job profile
After=network.target
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
ExecStart=/home/kang/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main --profile kk_job gateway run --replace
WorkingDirectory=/home/kang/.hermes/hermes-agent
Environment=\"PATH=/home/kang/.hermes/hermes-agent/venv/bin:/home/kang/.hermes/hermes-agent/node_modules/.bin:/home/kang/.hermes/node/bin:/home/kang/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\"
Environment=\"VIRTUAL_ENV=/home/kang/.hermes/hermes-agent/venv\"
Environment=\"HERMES_HOME=/home/kang/.hermes/profiles/kk_job\"
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
mkdir -p ~/.config/systemd/user/hermes-gateway-kk_job.service.d && cat > ~/.config/systemd/user/hermes-gateway-kk_job.service.d/override.conf <<'OVR'
[Service]
ExecStart=
ExecStart=/usr/bin/script -qfc \"/home/kang/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main --profile kk_job gateway run --replace\" /home/kang/.hermes/profiles/kk_job/logs/gateway.log
OVR
mkdir -p /home/kang/.hermes/profiles/kk_job/logs
systemctl --user daemon-reload
systemctl --user reset-failed hermes-gateway-kk_job.service 2>/dev/null
systemctl --user enable --now hermes-gateway-kk_job.service"
wsl -d Ubuntu -- bash -lc "systemctl --user is-active hermes-gateway-kk_job.service"
if errorlevel 1 (
    echo        WARN: kk_job gateway not active. Check: wsl -d Ubuntu -- journalctl --user -u hermes-gateway-kk_job -n 30
) else (
    echo        kk_job gateway active.
)
echo.

REM ---- 3. Hermes Dashboard (via systemd user service) ----
echo [3/5] Starting Hermes web dashboard (port 9119)...
REM Prebuild web UI if missing (hermes's auto-build is unreliable in our setup).
wsl -d Ubuntu -- bash -lc "[ -f ~/.hermes/hermes-agent/hermes_cli/web_dist/index.html ] || (echo '        Building web UI (first run)...'; cd ~/.hermes/hermes-agent/web && npm install --silent && npm run build > /dev/null 2>&1)"
REM Use systemd user service ? it handles detachment, auto-restart, and logging
REM properly. Avoids wsl.exe/cmd.exe quote+lifecycle issues under Task Scheduler.
wsl -d Ubuntu -- bash -lc "systemctl --user restart hermes-dashboard.service"
echo        Waiting for port 9119 to open (up to 15s)...
wsl -d Ubuntu -- bash -lc "for i in $(seq 1 15); do ss -tln 2>/dev/null | grep -q ':9119 ' && exit 0; sleep 1; done; exit 1"
if errorlevel 1 (
    echo        WARN: dashboard not listening yet. Check: wsl -d Ubuntu -- journalctl --user -u hermes-dashboard -n 50
) else (
    echo        Dashboard up: http://localhost:9119
)
echo.

REM ---- 4. Register cron jobs ----
echo [4/5] Registering cron jobs (idempotent)...
wsl -d Ubuntu -- bash -lc "python3 /mnt/e/hermes-hybrid/scripts/register_cron_jobs.py 2>/dev/null" 2>nul
echo        Done.
echo.

REM ---- 5. Bot ----
echo [5/5] Starting Discord bot...
echo        (bot logs will appear below; Ctrl+C to stop)
echo ------------------------------------------
echo.
call "%~dp0start.bat"

endlocal
