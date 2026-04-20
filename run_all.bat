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
echo [2/3] Warming up WSL (Ubuntu)...
wsl -d Ubuntu -- echo "WSL ready"
if errorlevel 1 (
    echo        ERROR: WSL warm-up failed. Check that 'wsl -d Ubuntu' works.
    pause
    exit /b 1
)
echo.

REM ---- 3. Bot ----
echo [3/3] Starting Discord bot...
echo        (bot logs will appear below; Ctrl+C to stop)
echo ------------------------------------------
echo.
call "%~dp0start.bat"

endlocal
