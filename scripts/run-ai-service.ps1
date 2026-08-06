<#
.SYNOPSIS
    Runs the SeekAndDestroy FastAPI AI service.
#>
param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8088,
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating virtual environment..." -ForegroundColor Yellow
    python -m venv (Join-Path $repoRoot ".venv")
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r (Join-Path $repoRoot "ai-service\requirements.txt")
}

Push-Location (Join-Path $repoRoot "ai-service")
try {
    $reloadFlag = if ($Reload) { "--reload" } else { "" }
    & $venvPython -m uvicorn app.main:app --host $BindHost --port $Port $reloadFlag
}
finally {
    Pop-Location
}
