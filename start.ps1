$ErrorActionPreference = "Continue"
$ProjectRoot = $PSScriptRoot

function Write-Step { param($msg)
    Write-Host ""
    Write-Host "=> $msg" -ForegroundColor Cyan
}
function Write-Ok { param($msg)
    Write-Host "   OK: $msg" -ForegroundColor Green
}
function Write-Warn { param($msg)
    Write-Host "   WARN: $msg" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Porter Capital Lead Intelligence Platform" -ForegroundColor White
Write-Host "Starting all services..." -ForegroundColor Gray
Write-Host ""

# Step 1 - Docker Desktop
Write-Step "Step 1 - Checking Docker Desktop"
$docker = Get-Process "Docker Desktop" -ErrorAction SilentlyContinue
if (-not $docker) {
    Write-Warn "Docker Desktop not running. Starting it..."
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    Write-Host "   Waiting 30 seconds for Docker to start..." -ForegroundColor Gray
    Start-Sleep -Seconds 30
} else {
    Write-Ok "Docker Desktop already running"
}

# Step 2 - Database
Write-Step "Step 2 - Starting PostgreSQL database"
Set-Location $ProjectRoot
docker compose up -d db 2>&1 | Out-Null
Write-Ok "Database container started"

Write-Host "   Waiting for database to be ready..." -ForegroundColor Gray
$waited = 0
$ready = $false
while (-not $ready -and $waited -lt 30) {
    Start-Sleep -Seconds 2
    $waited += 2
    $health = docker inspect porter-leads-db-1 --format "{{.State.Health.Status}}" 2>&1
    if ($health -eq "healthy") {
        $ready = $true
    }
}
Write-Ok "Database is ready"

# Step 3 - Migrations
Write-Step "Step 3 - Running database migrations"
python -m alembic upgrade head 2>&1 | Out-Null
Write-Ok "Migrations up to date"

# Step 4 - FastAPI backend
Write-Step "Step 4 - Starting FastAPI backend on port 8000"
$apiRunning = Test-NetConnection -ComputerName localhost -Port 8000 -WarningAction SilentlyContinue
if ($apiRunning.TcpTestSucceeded) {
    Write-Ok "API already running on port 8000"
} else {
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$ProjectRoot'; python -m uvicorn app.api.main:app --port 8000 --reload"
    Start-Sleep -Seconds 4
    Write-Ok "FastAPI backend started"
}

# Step 5 - Next.js frontend
Write-Step "Step 5 - Starting Next.js frontend on port 3000"
$frontendRunning = Test-NetConnection -ComputerName localhost -Port 3000 -WarningAction SilentlyContinue
if ($frontendRunning.TcpTestSucceeded) {
    Write-Ok "Frontend already running on port 3000"
} else {
    Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$ProjectRoot\frontend'; npm run dev"
    Write-Host "   Waiting 15 seconds for frontend to compile..." -ForegroundColor Gray
    Start-Sleep -Seconds 15
    Write-Ok "Next.js frontend started"
}

# Step 6 - SAM.gov enrichment
Write-Step "Step 6 - Running SAM.gov daily enrichment"
$env:OFAC_SDN_PATH = "$ProjectRoot\data\ofac_sdn.csv"
$harshaKey = ""
$kateKey = ""
if (Test-Path "$ProjectRoot\.env") {
    $envLines = Get-Content "$ProjectRoot\.env"
    foreach ($line in $envLines) {
        if ($line -match "^SAM_GOV_API_KEY_HARSHA=(.+)") { $harshaKey = $matches[1].Trim('"') }
        if ($line -match "^SAM_GOV_API_KEY_KATE=(.+)") { $kateKey = $matches[1].Trim('"') }
    }
}
if ($harshaKey -and $kateKey) {
    $env:SAM_GOV_API_KEY_HARSHA = $harshaKey
    $env:SAM_GOV_API_KEY_KATE = $kateKey
    python scripts/run_sam_enrichment.py 2>&1 | Out-Null
    Write-Ok "SAM.gov enrichment complete"
} else {
    Write-Warn "SAM.gov keys not found in .env - skipping enrichment"
}

# Step 7 - Open browser
Write-Step "Step 7 - Opening Porter Capital dashboard"
Start-Sleep -Seconds 2
Start-Process "http://localhost:3000/leads"
Write-Ok "Browser opened"

Write-Host ""
Write-Host "================================================" -ForegroundColor Gray
Write-Host " All services running" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Gray
Write-Host ""
Write-Host " Dashboard : http://localhost:3000/leads" -ForegroundColor White
Write-Host " API       : http://localhost:8000/api/health" -ForegroundColor White
Write-Host " API Docs  : http://localhost:8000/docs" -ForegroundColor White
Write-Host ""
Write-Host " To stop: run .\stop.ps1 or docker compose down" -ForegroundColor Gray
Write-Host ""
