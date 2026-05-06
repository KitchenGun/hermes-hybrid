@echo off
setlocal
chcp 65001 >nul
title hermes-hybrid launcher

echo ==========================================
echo   hermes-hybrid full stack launcher
echo   Phase 10 — opencode/gpt-5.5 master
echo ==========================================
echo.

REM ---- 1. Ollama (optional — needed only for memory embedding) ----
REM Phase 8 후 master 는 opencode CLI 를 사용 (gpt-5.5, $0 marginal).
REM Ollama 는 memory_search_backend=embedding 일 때 bge-m3 호출용 fallback 으로만 의미.
REM ollama_enabled=false 면 이 단계 전체를 skip 해도 무방.
echo [1/3] Starting Ollama server (optional)...
tasklist /FI "IMAGENAME eq ollama.exe" 2>nul | find /I "ollama.exe" >nul
if %errorlevel%==0 (
    echo        Ollama already running. Skipping launch.
) else (
    start "ollama-serve" /MIN cmd /c "ollama serve"
    echo        Launched in minimized window.
)
echo        Probing port 11434 (up to 15s)...
powershell -NoProfile -Command "$ok=$false; for($i=0; $i -lt 15; $i++){ try { (Invoke-WebRequest -Uri 'http://localhost:11434/api/tags' -TimeoutSec 1 -UseBasicParsing) | Out-Null; $ok=$true; break } catch { Start-Sleep -Seconds 1 } }; if($ok){ Write-Host '       Ollama is up.' -ForegroundColor Green } else { Write-Host '       INFO: Ollama not running. OK unless HERMES_MEMORY_SEARCH_BACKEND=embedding.' -ForegroundColor DarkYellow }"
echo.

REM ---- 2. WSL warm-up + persistent session keep-alive ----
REM 봇 자체는 Windows host 에서 Python 으로 돈다. WSL 은 opencode/claude CLI
REM subprocess 호출용. keepalive 는 microsoft/WSL#10205 회피 — 마지막 로그인
REM 세션이 사라지면 systemd-user 가 die 하므로 hidden bash loop 로 잡아둔다.
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
