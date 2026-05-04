# hermes-hybrid startup script
$ProjectDir = $PSScriptRoot
Set-Location $ProjectDir

# Preflight is also performed inside the Python entry point. This script
# just handles the venv activation; gateway stop/disable runs from preflight
# with explicit logs so users can see what happened.

$venvActivate = Join-Path $ProjectDir ".venv\Scripts\Activate.ps1"
if (Test-Path $venvActivate) {
    Write-Host "[venv] Activating..." -ForegroundColor Cyan
    & $venvActivate
} else {
    Write-Host "[venv] Not found — using system Python" -ForegroundColor Yellow
}

# Bot stdout/stderr 를 logs/bot-<dt>.log 로 redirect.
# 이전에는 콘솔에만 가서 (1) Task Scheduler 로 띄워진 LogonTrigger 세션의
# 콘솔이 사용자가 logoff 하면 사라지고 (2) 부팅 시 봇이 안 떴는지 디버깅할
# 단서가 0이었다. tee 로 콘솔 + 파일 양쪽에 흘려 인터랙티브 실행 시에도
# 동시에 보이게 한다.
$LogDir = Join-Path $ProjectDir "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$Stamp  = Get-Date -Format "yyyyMMdd-HHmmss"
$LogOut = Join-Path $LogDir "bot-$Stamp.log"
$LogErr = Join-Path $LogDir "bot-$Stamp.err.log"

Write-Host "[run] Starting hermes-hybrid Discord gateway..." -ForegroundColor Cyan
Write-Host "[run] Logs: $LogOut (stdout) / $LogErr (stderr)" -ForegroundColor Cyan

# stdout/stderr 둘 다 파일로 redirect.
#
# 인코딩 주의: Windows PowerShell 5.1 의 ``Tee-Object`` / ``>`` 는 기본이
# UTF-16 LE (BOM 포함) 라 wsl/cat/grep 으로 봇 로그를 들여다볼 때 NUL 바이트
# 가 섞여 가독성이 0이 된다. UTF-8 로 강제해야 ``tail -f``, ``Select-String``,
# ``rg`` 가 정상 동작한다.
#
# stderr 분리: PowerShell 5.1 native exe stderr 를 pipeline 안에서 redirect
# 하면 NativeCommandError 로 wrap 되어 ``$?`` 가 false 로 떨어지고 종료 코드
# 도 의미가 바뀐다. cmd /c 안에서 1>/2> 로 redirect 하면 OS 레벨 redirect 라
# 그런 wrap 없이 깔끔하다. 그래서 봇 실행은 cmd /c 로 감싼다.
#
# 콘솔 출력 미러: Task Scheduler 컨텍스트에선 어차피 콘솔이 사용자에게 안
# 보이므로 ``Tee`` 의 콘솔 mirror 를 포기한다. 인터랙티브 디버깅 시에는
# 별도 창에서 ``Get-Content -Wait`` 또는 wsl ``tail -f`` 로 따라간다.
$env:PYTHONUNBUFFERED = "1"
cmd /c "python -u scripts\run_bot.py 1>""$LogOut"" 2>""$LogErr"""
