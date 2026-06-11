#Requires -Version 5.1
<#
.SYNOPSIS
    Smoke check for the Porter Leads staging environment.

.DESCRIPTION
    Verifies that:
      - db_staging container is running and healthy
      - psql can connect to porter_leads_staging
      - alembic_version table exists and has a row (migrations ran)
      - Core tables exist and row counts are readable
    Prints next-step commands for the operator.
    Does NOT call any external APIs.

.EXAMPLE
    .\scripts\staging_smoke_check.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$COMPOSE_FILE = Join-Path (Split-Path $PSScriptRoot -Parent) "docker-compose.staging.yml"
$DB_USER      = "porter"
$DB_NAME      = "porter_leads_staging"

function Write-Step([string]$msg) { Write-Host "[staging-smoke] $msg" }
function Write-Pass([string]$msg) { Write-Host "[staging-smoke] PASS: $msg" -ForegroundColor Green }
function Write-Fail([string]$msg) { Write-Host "[staging-smoke] FAIL: $msg" -ForegroundColor Red }

# Run SQL via docker exec on a known container ID (avoids compose env_file validation).
function Invoke-Psql([string]$cid, [string]$sql) {
    $out = docker exec $cid psql -U $DB_USER -d $DB_NAME -t -A -c $sql
    if ($LASTEXITCODE -ne 0) { throw "psql failed: $sql" }
    return ($out | Out-String).Trim()
}

$failed = $false

try {
    Write-Host ""
    Write-Host "Porter Leads -- Staging Smoke Check"
    Write-Host "===================================="
    Write-Host ""

    # -- 1. Container running -------------------------------------------------
    Write-Step "Checking db_staging container..."
    # Filter to lines that look like container IDs (hex chars); skips any compose warnings.
    $psOutput    = docker compose -f $COMPOSE_FILE ps -q db_staging
    $containerId = @($psOutput) | Where-Object { $_ -match '^[0-9a-f]' } | Select-Object -First 1
    if (-not $containerId) {
        Write-Fail "db_staging is not running."
        Write-Host "  Start it with: docker compose -f docker-compose.staging.yml up db_staging -d"
        exit 1
    }
    $containerId = $containerId.Trim()
    Write-Pass "db_staging container found: $containerId"

    # -- 2. pg_isready ---------------------------------------------------------
    Write-Step "Checking pg_isready..."
    docker exec $containerId pg_isready -U $DB_USER -d $DB_NAME | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "pg_isready returned non-zero. DB may still be starting."
        exit 1
    }
    Write-Pass "pg_isready OK"

    # -- 3. Alembic version ----------------------------------------------------
    Write-Step "Checking alembic_version (migrations must have run)..."
    $alembicExists = Invoke-Psql $containerId "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'alembic_version');"
    if ($alembicExists -ne "t") {
        Write-Fail "alembic_version table does not exist. Run: alembic upgrade head"
        $failed = $true
    }
    else {
        $version = Invoke-Psql $containerId "SELECT version_num FROM alembic_version LIMIT 1;"
        if (-not $version) {
            Write-Fail "alembic_version is empty. Run: alembic upgrade head"
            $failed = $true
        }
        else {
            Write-Pass "alembic_version = $version"
        }
    }

    # -- 4. Core table existence + row counts ----------------------------------
    Write-Host ""
    Write-Host ("  {0,-40} {1,10}" -f "Table", "Row Count")
    Write-Host "  -------------------------------------------------------"

    $coreTables = @(
        "raw_source_events",
        "evidence_items",
        "companies",
        "lead_candidates",
        "source_runs",
        "review_decisions"
    )

    foreach ($tbl in $coreTables) {
        $exists = Invoke-Psql $containerId "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = '$tbl');"
        if ($exists -ne "t") {
            Write-Host ("  {0,-40} {1,10}" -f $tbl, "MISSING") -ForegroundColor Red
            $failed = $true
        }
        else {
            $count = Invoke-Psql $containerId "SELECT count(*) FROM $tbl;"
            Write-Host ("  {0,-40} {1,10}" -f $tbl, $count)
        }
    }

    Write-Host "  -------------------------------------------------------"
    Write-Host ""

    # -- 5. Summary ------------------------------------------------------------
    if ($failed) {
        Write-Fail "One or more checks failed. See above."
        Write-Host ""
        Write-Host "  If tables are missing, run migrations:"
        Write-Host '  $env:DATABASE_URL = "postgresql://porter:localdev@localhost:5433/porter_leads_staging"'
        Write-Host "  alembic upgrade head"
        exit 1
    }

    Write-Pass "All checks passed."
    Write-Host ""
    Write-Host "  Next steps:"
    Write-Host "  -----------"
    Write-Host "  Start staging dashboard:"
    Write-Host "    docker compose -f docker-compose.staging.yml up dashboard_staging -d"
    Write-Host "    # Opens at http://localhost:8502"
    Write-Host ""
    Write-Host "  Run a small pipeline test (reads from USASpending -- see docs/STAGING.md first):"
    Write-Host '    $env:DATABASE_URL = "postgresql://porter:localdev@localhost:5433/porter_leads_staging"'
    Write-Host '    $env:PIPELINE_LOOKBACK_DAYS = "3"'
    Write-Host "    python scripts/run_pipeline.py"
    Write-Host ""
    Write-Host "  Tear down staging DB:"
    Write-Host "    docker compose -f docker-compose.staging.yml down"
    Write-Host "    # Add -v to also delete the staging volume (data loss)"
    Write-Host ""
    exit 0
}
catch {
    Write-Fail "$_"
    exit 1
}
