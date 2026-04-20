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

Write-Host "[run] Starting hermes-hybrid Discord gateway..." -ForegroundColor Cyan
python scripts\run_bot.py
