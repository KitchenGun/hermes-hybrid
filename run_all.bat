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

REM ---- 2. WSL warm-up ----
echo [2/5] Warming up WSL (Ubuntu)...
wsl -d Ubuntu -- echo "WSL ready"
if errorlevel 1 (
    echo        ERROR: WSL warm-up failed. Check that 'wsl -d Ubuntu' works.
    pause
    exit /b 1
)
REM Per official Hermes docs, the gateway daemon is what ticks the cron
REM scheduler every 60s. Without it running, calendar_ops cron jobs
REM (morning_briefing, daily_wrap, etc.) never fire. Dashboard alone is
REM admin UI only — it does NOT tick cron.
REM
REM Earlier this script disabled the gateway service to "avoid a race
REM with dashboard-spawned gateway", but that was a misdiagnosis: our
REM calendar_ops profile has no Discord/messaging platforms registered
REM (channel_directory.json is empty arrays, auth.json has no Discord
REM bot token), so the gateway only ticks cron — no platform connect,
REM no token conflict with the hermes-hybrid Discord bot.
echo [2.5/5] Enabling hermes-gateway service (cron tick daemon)...
wsl -d Ubuntu -- bash -lc "systemctl --user enable --now hermes-gateway-calendar_ops.service 2>&1 | tail -3"
wsl -d Ubuntu -- bash -lc "systemctl --user is-active hermes-gateway-calendar_ops.service"
if errorlevel 1 (
    echo        WARN: gateway not active. Check: wsl -d Ubuntu -- journalctl --user -u hermes-gateway-calendar_ops -n 30
) else (
    echo        Gateway active — cron tick is alive.
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
