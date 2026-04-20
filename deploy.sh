#!/bin/bash
# ──────────────────────────────────────────────
# Setup Sniper — Deploy to Hostinger VPS
# Matches your ChartSignl deployment pattern.
#
# Usage: ./deploy/deploy.sh
# ──────────────────────────────────────────────
set -euo pipefail

SERVER="root@167.88.43.61"
REMOTE_DIR="/root/setup-sniper"

echo "═══ Setup Sniper — Deploying to $SERVER ═══"

# 1. Sync code to server (exclude dev artifacts)
echo "→ Syncing code..."
rsync -avz --delete \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.git' \
    --exclude '.env' \
    --exclude 'watchlist.json' \
    --exclude 'alerts.log' \
    --exclude '.venv' \
    --exclude 'node_modules' \
    ./ "${SERVER}:${REMOTE_DIR}/"

# 2. Rebuild and restart container
echo "→ Rebuilding container..."
ssh "$SERVER" "cd ${REMOTE_DIR}/deploy && docker-compose down && docker-compose up -d --build"

# 3. Verify
echo "→ Verifying..."
sleep 3
ssh "$SERVER" "docker ps --filter name=setup-sniper --format '{{.Names}}\t{{.Status}}'"

echo ""
echo "═══ Deploy complete ═══"
echo "  Logs:    ssh $SERVER 'docker logs -f setup-sniper'"
echo "  Cron:    ssh $SERVER 'docker exec setup-sniper cat /app/logs/cron.log'"
echo "  Manual:  ssh $SERVER 'docker exec setup-sniper python main.py test'"
echo ""
