# ──────────────────────────────────────────────
# Setup Sniper — Deploy to Hostinger VPS (Windows)
# Matches your ChartSignl deployment pattern.
#
# Usage: .\deploy\deploy.ps1  (from project root)
# ──────────────────────────────────────────────

$ErrorActionPreference = "Stop"

$SERVER = "root@167.88.43.61"
$REMOTE_DIR = "/root/setup-sniper"

Write-Host ""
Write-Host "=== Setup Sniper - Deploying to $SERVER ===" -ForegroundColor Cyan

# Detect rsync (native, WSL, or fallback to tar/scp)
function Test-Rsync {
    try { Get-Command rsync -ErrorAction Stop | Out-Null; return "native" } catch {}
    try {
        $wslCheck = wsl which rsync 2>$null
        if ($wslCheck) { return "wsl" }
    } catch {}
    return "none"
}

$rsyncMode = Test-Rsync

if ($rsyncMode -eq "native") {
    Write-Host "-> Syncing code (rsync)..." -ForegroundColor Yellow
    rsync -avz --delete `
        --exclude '__pycache__' `
        --exclude '*.pyc' `
        --exclude '.git' `
        --exclude '.env' `
        --exclude 'watchlist.json' `
        --exclude 'alerts.log' `
        --exclude '.venv' `
        --exclude 'node_modules' `
        ./ "${SERVER}:${REMOTE_DIR}/"
}
elseif ($rsyncMode -eq "wsl") {
    Write-Host "-> Syncing code (WSL rsync)..." -ForegroundColor Yellow
    $wslPath = (wsl wslpath -u (Get-Location).Path)
    wsl rsync -avz --delete `
        --exclude '__pycache__' `
        --exclude '*.pyc' `
        --exclude '.git' `
        --exclude '.env' `
        --exclude 'watchlist.json' `
        --exclude 'alerts.log' `
        --exclude '.venv' `
        --exclude 'node_modules' `
        "${wslPath}/" "${SERVER}:${REMOTE_DIR}/"
}
else {
    Write-Host "-> Syncing code (tar/scp fallback)..." -ForegroundColor Yellow
    # Create tarball excluding dev artifacts
    tar -czf setup-sniper.tar.gz `
        --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' `
        --exclude='.env' --exclude='watchlist.json' --exclude='alerts.log' `
        --exclude='.venv' --exclude='node_modules' `
        -C . .
    scp setup-sniper.tar.gz "${SERVER}:/tmp/"
    ssh $SERVER "rm -rf ${REMOTE_DIR} && mkdir -p ${REMOTE_DIR} && tar -xzf /tmp/setup-sniper.tar.gz -C ${REMOTE_DIR} && rm /tmp/setup-sniper.tar.gz"
    Remove-Item setup-sniper.tar.gz -ErrorAction SilentlyContinue
}

# Rebuild container on server
Write-Host "-> Rebuilding container..." -ForegroundColor Yellow
ssh $SERVER "cd ${REMOTE_DIR}/deploy && docker-compose down && docker-compose up -d --build"

# Verify
Start-Sleep -Seconds 3
Write-Host "-> Verifying..." -ForegroundColor Yellow
ssh $SERVER "docker ps --filter name=setup-sniper --format '{{.Names}}`t{{.Status}}'"

Write-Host ""
Write-Host "=== Deploy complete ===" -ForegroundColor Green
Write-Host "  Logs:    ssh $SERVER 'docker logs -f setup-sniper'"
Write-Host "  Cron:    ssh $SERVER 'docker exec setup-sniper cat /app/logs/cron.log'"
Write-Host "  Manual:  ssh $SERVER 'docker exec setup-sniper python main.py test'"
Write-Host ""
