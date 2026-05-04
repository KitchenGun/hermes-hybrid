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

REM ---- 2.5/2.6. Gateway systemd-user units (calendar_ops + kk_job) ----
REM Why this is one shell-out instead of two cmd-quoted heredocs:
REM   cmd.exe terminates a double-quoted argument at the first line break,
REM   so the previous form embedded `[Service]` / `Environment=` / `[Install]`
REM   etc. directly in the batch parser, where each line was attempted as a
REM   cmd command and failed with "is not recognized". Net result: gateway
REM   units silently never got refreshed. The .sh script does the heredocs
REM   under bash where they actually work, and we just shell out once.
REM
REM   Hermes gateway exits 1 without a controlling TTY, so the script wraps
REM   ExecStart in `script -qfc` to provide a pseudo-TTY. See
REM   ARCHITECTURE.md "Hermes runtime — gateway vs dashboard".
echo [2.5/5] Installing gateway systemd-user units (calendar_ops + kk_job)...
wsl -d Ubuntu -- bash /mnt/e/hermes-hybrid/scripts/install_gateway_units.sh
if errorlevel 1 (
    echo        WARN: install_gateway_units.sh failed. Check: wsl -d Ubuntu -- journalctl --user -u hermes-gateway-calendar_ops -n 30
) else (
    echo        Gateway units installed/refreshed.
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
