<#
.SYNOPSIS
    Runs the full SeekAndDestroy test suite: Python (ai-service), Python (mcp-server), .NET (api-gateway).
    Requires the database to already be initialized (see init-db.ps1).
#>
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"

Write-Host "=== ai-service tests ===" -ForegroundColor Cyan
Push-Location (Join-Path $repoRoot "ai-service")
& $venvPython -m pytest tests -v
$aiServiceExit = $LASTEXITCODE
Pop-Location

Write-Host "`n=== mcp-server tests ===" -ForegroundColor Cyan
Push-Location (Join-Path $repoRoot "mcp-server")
& $venvPython -m pytest tests -v
$mcpExit = $LASTEXITCODE
Pop-Location

Write-Host "`n=== api-gateway tests ===" -ForegroundColor Cyan
Push-Location (Join-Path $repoRoot "api-gateway")
dotnet test SeekAndDestroy.slnx
$dotnetExit = $LASTEXITCODE
Pop-Location

Write-Host "`n=== Summary ===" -ForegroundColor Cyan
Write-Host "ai-service: $(if ($aiServiceExit -eq 0) { 'PASS' } else { 'FAIL' })"
Write-Host "mcp-server: $(if ($mcpExit -eq 0) { 'PASS' } else { 'FAIL' })"
Write-Host "api-gateway: $(if ($dotnetExit -eq 0) { 'PASS' } else { 'FAIL' })"

if ($aiServiceExit -ne 0 -or $mcpExit -ne 0 -or $dotnetExit -ne 0) {
    exit 1
}
