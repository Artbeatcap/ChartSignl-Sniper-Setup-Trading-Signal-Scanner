FROM python:3.12-slim

WORKDIR /app

# Install cron (for scheduled scans)
RUN apt-get update && apt-get install -y --no-install-recommends cron && \
    rm -rf /var/lib/apt/lists/*

# Dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY *.py ./

# Schedule: deploy/crontab. Entrypoint runs `crontab /app/deploy/crontab` on start (survives rebuild).
RUN mkdir -p /app/deploy
COPY deploy/crontab /app/deploy/crontab
COPY deploy/crontab /etc/cron.d/setup-sniper
RUN chmod 0644 /etc/cron.d/setup-sniper

COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
  CMD python -c "from config import ScannerConfig; ScannerConfig(); print('ok')"

ENTRYPOINT ["/entrypoint.sh"]
CMD ["cron"]
