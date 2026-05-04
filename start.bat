@echo off
REM Task Scheduler(HermesHybridLauncher) 환경에서는 STDIN 이 없어 ``pause``
REM 가 input 대기로 hang → ExitCode 0xFF (-1) 로 abort 된다. 봇이 정상
REM 동작하는 동안에는 powershell 가 살아있어 pause 가 fire 안 했지만, 봇이
REM 죽거나 종료되면 pause 가 task 환경에서 영구 hang.
REM
REM 디버깅 시 콘솔 출력은 logs/bot-<datetime>.log 에 남으므로 인터랙티브
REM pause 가 굳이 필요하지 않다. errorlevel 만 그대로 노출한다.
powershell -ExecutionPolicy Bypass -File "%~dp0start.ps1"
exit /b %errorlevel%
