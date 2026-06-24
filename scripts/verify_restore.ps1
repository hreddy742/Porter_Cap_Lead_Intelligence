#Requires -Version 5.1
<#
.SYNOPSIS
    Restore a porter_leads backup into an isolated verification database
    and confirm key table row counts are readable.

.DESCRIPTION
    Never touches the active porter_leads database.
    Restores into porter_leads_restore_verify, checks all key tables,
    then drops the verification database in a try/finally block so
    cleanup always runs even if a step fails.

.PARAMETER BackupFile
    Full path to a .sql backup file. Defaults to the newest file in backups/.

.EXAMPLE
    .\scripts\verify_restore.ps1
    .\scripts\verify_restore.ps1 -BackupFile backups\porter_leads_20260611_120000.sql
#>

[CmdletBinding()]
param(
    [string]$BackupFile = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$COMPOSE_SERVICE = "db"
$DB_USER         = "porter"
$VERIFY_DB       = "porter_leads_restore_verify"
$ACTIVE_DB       = "porter_leads"
$CONTAINER_TMP   = "/tmp/porter_leads_verify.sql"
$PROJECT_ROOT    = Split-Path $PSScriptRoot -Parent
$BACKUP_DIR      = Join-Path $PROJECT_ROOT "backups"

# Hard safety guard: VERIFY_DB must never equal the active database name
if ($VERIFY_DB -eq $ACTIVE_DB) {
    Write-Host "FATAL: VERIFY_DB equals ACTIVE_DB ('$ACTIVE_DB'). Aborting." -ForegroundColor Red
    exit 1
}

function Write-Step([string]$msg) { Write-Host "[verify] $msg" }

# Run SQL against a specific database; returns trimmed stdout. Throws on failure.
function Invoke-Psql([string]$database, [string]$sql) {
    $out = docker compose exec -T $COMPOSE_SERVICE psql -U $DB_USER -d $database -t -A -c $sql
    if ($LASTEXITCODE -ne 0) { throw "psql failed on '$database': $sql" }
    return ($out | Out-String).Trim()
}

# Run management SQL against the postgres maintenance database.
function Invoke-PsqlAdmin([string]$sql) {
    docker compose exec -T $COMPOSE_SERVICE psql -U $DB_USER -d postgres -t -A -c $sql | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "psql admin failed: $sql" }
}

$verifyDbCreated = $false
$exitCode = 1

try {
    # --- Resolve backup file ---------------------------------------------------
    if ($BackupFile -eq "") {
        Write-Step "No backup file specified - selecting newest file in backups/"
        $newest = Get-ChildItem -Path $BACKUP_DIR -Filter "*.sql" -ErrorAction SilentlyContinue |
                  Sort-Object LastWriteTime -Descending |
                  Select-Object -First 1
        if (-not $newest) {
            throw "No .sql files found in '$BACKUP_DIR'. Run backup_db.ps1 first."
        }
        $BackupFile = $newest.FullName
    }

    if (-not (Test-Path $BackupFile)) { throw "Backup file not found: $BackupFile" }
    $fileSize = (Get-Item $BackupFile).Length
    if ($fileSize -eq 0) { throw "Backup file is empty: $BackupFile" }
    Write-Step "Backup: $BackupFile"
    Write-Step "Size  : $fileSize bytes"

    # --- Locate running container ----------------------------------------------
    Write-Step "Locating container for service '$COMPOSE_SERVICE'..."
    $containerId = (docker compose ps -q $COMPOSE_SERVICE | Select-Object -First 1)
    if (-not $containerId) {
        throw "No running container for '$COMPOSE_SERVICE'. Run: docker compose up db -d"
    }
    $containerId = $containerId.Trim()
    Write-Step "Container: $containerId"

    # --- Terminate any existing connections to the verify DB ------------------
    # Harmless no-op if the DB does not exist; prevents DROP DATABASE blocking.
    Write-Step "Terminating any connections to '$VERIFY_DB'..."
    docker compose exec -T $COMPOSE_SERVICE psql -U $DB_USER -d postgres -t -A -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$VERIFY_DB' AND pid <> pg_backend_pid();" | Out-Null

    # --- Drop verify DB if it exists, then create fresh -----------------------
    Write-Step "Dropping '$VERIFY_DB' if it exists..."
    Invoke-PsqlAdmin "DROP DATABASE IF EXISTS $VERIFY_DB;"

    Write-Step "Creating '$VERIFY_DB'..."
    Invoke-PsqlAdmin "CREATE DATABASE $VERIFY_DB OWNER $DB_USER;"
    $verifyDbCreated = $true

    # --- Copy backup into container -------------------------------------------
    Write-Step "Copying backup into container..."
    docker cp $BackupFile "${containerId}:${CONTAINER_TMP}"
    if ($LASTEXITCODE -ne 0) { throw "docker cp into container failed" }

    # --- Restore --------------------------------------------------------------
    Write-Step "Restoring into '$VERIFY_DB'..."
    docker compose exec -T $COMPOSE_SERVICE psql -U $DB_USER -d $VERIFY_DB -f $CONTAINER_TMP -q
    if ($LASTEXITCODE -ne 0) { throw "psql restore failed" }
    Write-Step "Restore complete."

    # --- Verify key tables ----------------------------------------------------
    Write-Host ""
    Write-Host ("  {0,-38} {1,10}" -f "Table", "Row Count")
    Write-Host "  -------------------------------------------------------"

    $coreTables = @(
        "raw_source_events",
        "evidence_items",
        "companies",
        "lead_candidates",
        "source_runs",
        "alembic_version"
    )

    $allPassed = $true

    foreach ($tbl in $coreTables) {
        try {
            $count = Invoke-Psql $VERIFY_DB "SELECT count(*) FROM $tbl;"
            Write-Host ("  {0,-38} {1,10}" -f $tbl, $count)
        }
        catch {
            Write-Host ("  {0,-38}      ERROR" -f $tbl) -ForegroundColor Red
            $allPassed = $false
        }
    }

    # review_decisions is append-only; may or may not exist yet
    $rdExistsRaw = Invoke-Psql $VERIFY_DB "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'review_decisions');"
    if ($rdExistsRaw -eq "t") {
        try {
            $count = Invoke-Psql $VERIFY_DB "SELECT count(*) FROM review_decisions;"
            Write-Host ("  {0,-38} {1,10}" -f "review_decisions", $count)
        }
        catch {
            Write-Host ("  {0,-38}      ERROR" -f "review_decisions") -ForegroundColor Red
            $allPassed = $false
        }
    }
    else {
        Write-Host ("  {0,-38} {1,10}" -f "review_decisions", "(not found)")
    }

    Write-Host "  -------------------------------------------------------"
    Write-Host ""

    if (-not $allPassed) { throw "One or more table checks failed." }

    Write-Step "All checks passed."
    Write-Step "Active database '$ACTIVE_DB' was not touched."
    $exitCode = 0
}
catch {
    Write-Host "[verify] ERROR: $_" -ForegroundColor Red
    $exitCode = 1
}
finally {
    # Always drop the verification database, whether we succeeded or failed.
    if ($verifyDbCreated) {
        Write-Step "Cleanup: dropping '$VERIFY_DB'..."
        # Terminate connections to the verify DB only (never porter_leads)
        docker compose exec -T $COMPOSE_SERVICE psql -U $DB_USER -d postgres -t -A -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$VERIFY_DB' AND pid <> pg_backend_pid();" | Out-Null
        try {
            Invoke-PsqlAdmin "DROP DATABASE IF EXISTS $VERIFY_DB;"
            Write-Step "'$VERIFY_DB' dropped."
        }
        catch {
            Write-Host "[verify] WARNING: Could not drop '$VERIFY_DB': $_" -ForegroundColor Yellow
        }
    }
}

exit $exitCode
