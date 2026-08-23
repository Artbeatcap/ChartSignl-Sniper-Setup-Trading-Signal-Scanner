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

### Add Alpaca keys (Windows)

From the repo root, with SSH access to the VPS:

```powershell
.\edit-remote-env.ps1
```

It prompts for `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` (secret is hidden), upserts `/root/setup-sniper/.env` on the server, recreates the container, and prints `set`/`missing` — never the values.

```powershell
.\edit-remote-env.ps1 -Editor   # nano the remote .env yourself
.\edit-remote-env.ps1 -Status   # check whether keys are present
```

Host defaults to `167.88.43.61` (or `$env:SNIPER_HOST`).

`docker logs setup-sniper` should show `Alpaca news: configured`. Without the keys the scanner still boots; the news gate emits `unconfirmed`.
