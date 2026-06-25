Write-Host ""
Write-Host "Stopping Porter Capital services..." -ForegroundColor Cyan
docker compose down
Write-Host "Database stopped." -ForegroundColor Green
Write-Host "Close the FastAPI and Next.js terminal windows manually." -ForegroundColor Yellow
Write-Host ""
