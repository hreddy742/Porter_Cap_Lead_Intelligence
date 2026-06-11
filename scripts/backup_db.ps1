#Requires -Version 5.1
<#
.SYNOPSIS
    Create a timestamped pg_dump backup of the porter_leads database.

.DESCRIPTION
    Runs pg_dump inside the running Postgres Docker container (avoiding
    PowerShell UTF-16 encoding corruption), then copies the dump to the
    host backups/ directory via docker cp.

.EXAMPLE
    .\scripts\backup_db.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$COMPOSE_SERVICE = "db"
$DB_USER         = "porter"
$DB_NAME         = "porter_leads"
$PROJECT_ROOT    = Split-Path $PSScriptRoot -Parent
$BACKUP_DIR      = Join-Path $PROJECT_ROOT "backups"
$CONTAINER_TMP   = "/tmp/porter_leads_backup.sql"

function Write-Step([string]$msg) { Write-Host "[backup] $msg" }

try {
    # Ensure backups/ exists (normally already present; guard for fresh clones)
    if (-not (Test-Path $BACKUP_DIR)) {
        New-Item -ItemType Directory -Path $BACKUP_DIR | Out-Null
    }

    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $filename  = "porter_leads_$timestamp.sql"
    $hostPath  = Join-Path $BACKUP_DIR $filename

    # ── Locate running container ──────────────────────────────────────────────
    Write-Step "Locating container for service '$COMPOSE_SERVICE'..."
    $containerId = (docker compose ps -q $COMPOSE_SERVICE | Select-Object -First 1)
    if (-not $containerId) {
        throw "No running container found for service '$COMPOSE_SERVICE'. Run: docker compose up db -d"
    }
    $containerId = $containerId.Trim()
    Write-Step "Container: $containerId"

    # ── Dump inside container → /tmp ──────────────────────────────────────────
    Write-Step "Running pg_dump inside container..."
    docker compose exec -T $COMPOSE_SERVICE pg_dump -U $DB_USER -d $DB_NAME -f $CONTAINER_TMP
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed (exit $LASTEXITCODE)" }

    # ── Copy to host (binary copy; no encoding conversion) ────────────────────
    Write-Step "Copying dump to host: $hostPath"
    docker cp "${containerId}:${CONTAINER_TMP}" $hostPath
    if ($LASTEXITCODE -ne 0) { throw "docker cp failed (exit $LASTEXITCODE)" }

    # ── Verify output ─────────────────────────────────────────────────────────
    if (-not (Test-Path $hostPath)) { throw "Backup file was not created: $hostPath" }
    $fileSize = (Get-Item $hostPath).Length
    if ($fileSize -eq 0) { throw "Backup file is empty: $hostPath" }

    Write-Step "SUCCESS"
    Write-Host ""
    Write-Host "  Backup file : $hostPath"
    Write-Host "  Size        : $fileSize bytes"
    Write-Host ""
    exit 0
}
catch {
    Write-Host "[backup] ERROR: $_" -ForegroundColor Red
    exit 1
}
