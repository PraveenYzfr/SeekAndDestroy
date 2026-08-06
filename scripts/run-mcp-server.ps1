<#
.SYNOPSIS
    Runs the SeekAndDestroy MCP server over stdio.
#>
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"

Push-Location (Join-Path $repoRoot "mcp-server")
try {
    & $venvPython server.py
}
finally {
    Pop-Location
}
