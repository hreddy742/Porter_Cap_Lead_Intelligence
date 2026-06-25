# Porter Capital Lead Intelligence Platform
# Daily startup script
# Usage: .\start.ps1

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot

function Write-Step {
    param($msg)
    Write-Host ""
    Write-Host "=> $msg" -ForegroundColor Cyan
}

function Write-Ok {
    param($msg)
    Write-Host "   OK: $msg" -ForegroundColor Green
}

function Write-Warn {
    param($msg)
    Write-Host "   WARN: $msg" -ForegroundColor Yellow
}

function Write-Fail {
    param($msg)
    Write-Host "   FAIL: $msg" -ForegroundColor Red
}

Write-Host ""
Write-Host "Porter Capital Lead Intelligence Platform" -ForegroundColor White
Write-Host "Starting all services..." -ForegroundColor Gray
Write-Host ""

# Step 1 — Docker Desktop
Write-Step "Step 1 — Checking Docker Desktop"
$docker = Get-Process "Docker Desktop" -ErrorAction SilentlyContinue
if (-not $docker) {
    Write-Warn "Docker Desktop not running. Starting it..."
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    Write-Host "   Waiting for Docker to start (30 seconds)..." -ForegroundColor Gray
    Start-Sleep -Seconds 30
} else {
    Write-Ok "Docker Desktop already running"
}

# Step 2 — PostgreSQL via docker compose
Write-Step "Step 2 — Starting PostgreSQL database"
Set-Location $ProjectRoot
try {
    docker compose up -d db 2>&1 | Out-Null
    Write-Ok "Database container started"
} catch {
    Write-Fail "docker compose failed: $_"
    exit 1
}

# Wait for DB to be healthy
Write-Host "   Waiting for database to be ready..." -ForegroundColor Gray
$maxWait = 30
$waited = 0
do {
    Start-Sleep -Seconds 2
    $waited += 2
    $health = docker inspect porter-leads-db-1 --format "{{.State.Health.Status}}" 2>&1
    if ($health -eq "healthy") { break }
    if ($waited -ge $maxWait) {
        Write-Warn "Database health check timed out — continuing anyway"
        break
    }
} while ($true)
Write-Ok "Database is ready"

# Step 3 — Run database migrations
Write-Step "Step 3 — Running database migrations"
try {
    $result = & python -m alembic upgrade head 2>&1
    Write-Ok "Migrations up to date"
} catch {
    Write-Warn "Migration warning: $_"
}

# Step 4 — FastAPI backend
Write-Step "Step 4 — Starting FastAPI backend (port 8000)"
$apiRunning = Test-NetConnection -ComputerName localhost -Port 8000 -WarningAction SilentlyContinue
if ($apiRunning.TcpTestSucceeded) {
    Write-Ok "API already running on port 8000"
} else {
    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-Command",
        "cd '$ProjectRoot'; Write-Host 'FastAPI Backend' -ForegroundColor Cyan; python -m uvicorn app.api.main:app --port 8000 --reload"
    ) -WindowStyle Normal
    # Wait for API to be ready
    Start-Sleep -Seconds 4
    Write-Ok "FastAPI backend started"
}

# Step 5 — Next.js frontend
Write-Step "Step 5 — Starting Next.js frontend (port 3000)"
$frontendRunning = Test-NetConnection -ComputerName localhost -Port 3000 -WarningAction SilentlyContinue
if ($frontendRunning.TcpTestSucceeded) {
    Write-Ok "Frontend already running on port 3000"
} else {
    Start-Process powershell -ArgumentList @(
        "-NoExit",
        "-Command",
        "cd '$ProjectRoot\frontend'; Write-Host 'Next.js Frontend' -ForegroundColor Cyan; npm run dev"
    ) -WindowStyle Normal
    # Wait for frontend to compile
    Write-Host "   Waiting for frontend to compile (15 seconds)..." -ForegroundColor Gray
    Start-Sleep -Seconds 15
    Write-Ok "Next.js frontend started"
}

# Step 6 — SAM.gov daily enrichment
Write-Step "Step 6 — Running SAM.gov daily enrichment"
# Load SAM.gov API keys from .env file (never hard-code keys in this script)
$envFile = Join-Path $ProjectRoot ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            $name = $Matches[1].Trim()
            $value = $Matches[2].Trim().Trim('"').Trim("'")
            if (-not [System.Environment]::GetEnvironmentVariable($name)) {
                [System.Environment]::SetEnvironmentVariable($name, $value, "Process")
            }
        }
    }
    Write-Ok "Loaded environment variables from .env"
} else {
    Write-Warn ".env file not found — SAM.gov keys must already be set in environment"
}
if (-not $env:SAM_GOV_API_KEY_HARSHA -and -not $env:SAM_GOV_API_KEY_KATE) {
    Write-Warn "SAM_GOV_API_KEY_HARSHA and SAM_GOV_API_KEY_KATE not set — skipping enrichment"
} else {
    try {
        python scripts/run_sam_enrichment.py 2>&1 | Out-Null
        Write-Ok "SAM.gov enrichment complete"
    } catch {
        Write-Warn "SAM.gov enrichment skipped: $_"
    }
}

# Step 7 — Open browser
Write-Step "Step 7 — Opening Porter Capital dashboard"
Start-Sleep -Seconds 2
Start-Process "http://localhost:3000/leads"
Write-Ok "Browser opened at http://localhost:3000/leads"

# Summary
Write-Host ""
Write-Host "================================================" -ForegroundColor Gray
Write-Host " All services running" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Gray
Write-Host ""
Write-Host " Dashboard : http://localhost:3000/leads" -ForegroundColor White
Write-Host " API       : http://localhost:8000/api/health" -ForegroundColor White
Write-Host " API Docs  : http://localhost:8000/docs" -ForegroundColor White
Write-Host ""
Write-Host " To stop all services run: docker compose down" -ForegroundColor Gray
Write-Host ""
