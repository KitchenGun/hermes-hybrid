@echo off
setlocal
chcp 65001 >nul
title hermes-hybrid launcher

echo ==========================================
echo   hermes-hybrid full stack launcher
echo   Phase 10 - opencode/gpt-5.5 master
echo ==========================================
echo.

REM ---- 1. Ollama (optional - needed only for memory embedding) ----
REM Phase 8: master uses opencode CLI (gpt-5.5, zero marginal cost).
REM Ollama is only needed when MEMORY_SEARCH_BACKEND=embedding (bge-m3).
REM Skip this step if OLLAMA_ENABLED=false in .env.
echo [1/3] Starting Ollama server (optional)...
tasklist /FI "IMAGENAME eq ollama.exe" 2>nul | find /I "ollama.exe" >nul
if %errorlevel%==0 (
    echo        Ollama already running. Skipping launch.
) else (
    start "ollama-serve" /MIN cmd /c "ollama serve"
    echo        Launched in minimized window.
)
echo        Probing port 11434 (up to 15s)...
powershell -NoProfile -Command "$ok=$false; for($i=0; $i -lt 15; $i++){ try { (Invoke-WebRequest -Uri 'http://localhost:11434/api/tags' -TimeoutSec 1 -UseBasicParsing) | Out-Null; $ok=$true; break } catch { Start-Sleep -Seconds 1 } }; if($ok){ Write-Host '       Ollama is up.' -ForegroundColor Green } else { Write-Host '       INFO: Ollama not running. OK unless MEMORY_SEARCH_BACKEND=embedding.' -ForegroundColor DarkYellow }"
echo.

REM ---- 2. WSL warm-up + persistent session keep-alive ----
REM Bot runs on Windows host (Python). WSL is only used for opencode/claude
REM CLI subprocess calls. The keep-alive workaround addresses microsoft/WSL#10205
REM where systemd-user dies once the last login session ends - we hold a
REM hidden bash loop so it stays alive across the bot lifetime.
echo [2/3] Warming up WSL (Ubuntu) + spawning keep-alive...
wsl -d Ubuntu -- echo "WSL ready"
if errorlevel 1 (
    echo        ERROR: WSL warm-up failed. Check 'wsl -d Ubuntu' works.
    pause
    exit /b 1
)
start "hermes-wsl-keepalive" /B wsl -d Ubuntu --user kang -- bash -lc "while true; do sleep 60; done"
echo        WSL warm + keepalive spawned.
echo.

REM ---- 3. Discord bot ----
echo [3/3] Starting Discord bot...
echo        (logs in logs/bot-YYYYMMDD-HHMMSS.log; Ctrl+C to stop)
echo ------------------------------------------
echo.
call "%~dp0start.bat"

endlocal
