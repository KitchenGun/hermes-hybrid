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

# stdout 은 Tee-Object 로 콘솔+파일 동시에. stderr 는 별도 .err.log 로 분리해
# 에러 grep 이 쉽게. 파이썬 출력 줄 버퍼링 — `-u` 로 un-buffered 강제하지
# 않으면 Tee 로 들어오는 시점이 늦어 디스크 로그 시각이 어긋난다.
$env:PYTHONUNBUFFERED = "1"
python -u scripts\run_bot.py 2> $LogErr | Tee-Object -FilePath $LogOut
