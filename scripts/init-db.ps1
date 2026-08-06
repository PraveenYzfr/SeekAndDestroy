<#
.SYNOPSIS
    Applies the SeekAndDestroy schema and seed data to SQL Server.
.PARAMETER Server
    SQL Server instance name. Defaults to the specification's LAPTOP-R6U8H616.
.PARAMETER Database
    Database name. Defaults to PraveenDB.
.PARAMETER Reset
    If set, runs reset.sql first (drops only the `sad` schema - safe on a shared DB).
#>
param(
    [string]$Server = "LAPTOP-R6U8H616",
    [string]$Database = "PraveenDB",
    [switch]$Reset
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

if ($Reset) {
    Write-Host "Resetting schema 'sad' on $Server/$Database..." -ForegroundColor Yellow
    sqlcmd -S $Server -d $Database -E -C -i (Join-Path $repoRoot "database\reset.sql") -b
}

Write-Host "Applying schema..." -ForegroundColor Cyan
sqlcmd -S $Server -d $Database -E -C -i (Join-Path $repoRoot "database\schema.sql") -b

Write-Host "Applying seed data..." -ForegroundColor Cyan
sqlcmd -S $Server -d $Database -E -C -i (Join-Path $repoRoot "database\seed.sql") -b

Write-Host "Done. Verifying table count..." -ForegroundColor Cyan
sqlcmd -S $Server -d $Database -E -C -Q "SELECT COUNT(*) AS Tables FROM sys.tables t JOIN sys.schemas s ON s.schema_id=t.schema_id WHERE s.name='sad';"
