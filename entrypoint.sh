#!/bin/bash
set -e

# Create log directory
mkdir -p /app/logs

# ─── CRITICAL: Cron doesn't inherit Docker env vars ───
# Dump all env vars to a file that cron jobs source before running.
printenv | grep -v "no_proxy" > /etc/environment

# If first arg is "cron", start cron daemon in foreground
if [ "$1" = "cron" ]; then
    echo "$(date) | Setup Sniper starting in scheduled mode"
    echo "$(date) | Nightly scan: 4:35 PM ET (Mon-Fri)"
    echo "$(date) | Ribbon watch (Setup 11): 3:45 PM ET (Mon-Fri)"
    echo "$(date) | Morning check: 8:00 AM + 8:30 fresh + 8:45 AM ET (Mon-Fri)"
    echo "$(date) | API Key: ${MASSIVE_API_KEY:0:6}...${MASSIVE_API_KEY: -4}"
    echo "$(date) | Discord: $([ -n "$DISCORD_WEBHOOK_URL" ] && echo 'configured' || echo 'not set')"

    if [ -f /app/deploy/crontab ]; then
        sed -i 's/\r$//' /app/deploy/crontab
        crontab /app/deploy/crontab
        echo "$(date) | Crontab installed from /app/deploy/crontab"
    fi

    exec cron -f
fi

exec python "$@"
