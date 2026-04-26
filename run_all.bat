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
REM Hermes gateway is intentionally kept disabled, with these trade-offs:
REM
REM PRO  cost of disable: calendar_ops cron jobs (morning_briefing,
REM      daily_wrap, etc.) do not fire. Per the official Hermes docs
REM      (user-guide/features/cron), cron tick is owned by the gateway
REM      daemon — dashboard alone does not tick.
REM
REM PRO  reason for disable: the gateway has known reliability issues
REM      under systemd in the current Hermes version. After any cron
REM      job error or restart, gateway-state.json's recorded PID is
REM      stale, and the next start exits with code 1 (upstream issues
REM      #13655 stale-PID, #11258 draining-state, #6631 update-restart-
REM      verify). Even resetting the PID and removing --replace did
REM      not stop the crash loop in our environment.
REM
REM PRO  why no Discord token conflict either way: our calendar_ops
REM      profile has channel_directory.json empty + auth.json with no
REM      Discord bot token, so the gateway, when running, only ticks
REM      cron — no platform connect attempt, no clash with hermes-hybrid
REM      Discord bot.
REM
REM See ARCHITECTURE.md "Hermes runtime — gateway vs dashboard" for the
REM full analysis. To re-enable once upstream stabilizes:
REM   wsl -- systemctl --user enable --now hermes-gateway-calendar_ops.service
echo [2.5/5] Keeping hermes-gateway service disabled (upstream systemd issues)...
wsl -d Ubuntu -- bash -lc "systemctl --user stop hermes-gateway-calendar_ops.service 2>/dev/null; systemctl --user disable hermes-gateway-calendar_ops.service 2>/dev/null; true" 2>nul
echo        Gateway disabled — dashboard handles admin only, cron ticks are paused.
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
