# Deploy notes

## Crontab after rebuild

`entrypoint.sh` runs `crontab /app/deploy/crontab` when the container starts in cron mode, so a fresh image from `docker-compose up --build` picks up the schedule without a manual `docker exec … crontab`.

If you bake a broken `deploy/crontab` or need to hot-patch, you can still install from the host:

```bash
sed -i 's/\r$//' /root/setup-sniper/deploy/crontab
docker cp /root/setup-sniper/deploy/crontab setup-sniper:/tmp/setup-sniper-crontab
docker exec setup-sniper crontab /tmp/setup-sniper-crontab
```

## Sync and rebuild (project root)

Compose expects to run from `/root/setup-sniper` (same directory as `Dockerfile` and `docker-compose.yml`).

```bash
cd /root/setup-sniper && docker-compose down && docker-compose up -d --build
```

`.env` is not copied from dev machines; configure it on the server.
