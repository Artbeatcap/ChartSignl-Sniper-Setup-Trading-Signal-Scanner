# n8n production (ChartSignl VPS)

Reference for the stack that runs on the Hostinger VPS (`deploy.sh` server).

## Where it lives

- Compose (on server): `/root/ChartSignl/apps/backend/deploy/docker-compose.yml`
- Git mirror (sibling repo): `../../chartsignl/apps/backend/deploy/docker-compose.yml`
- Copy in this repo: [docker-compose.production.yml](docker-compose.production.yml) (same content as deployed)

## Data volume

Compose declares `n8n_data`; with project directory `deploy`, Docker creates the volume as **`deploy_n8n_data`**. Use that name for one-off backup commands.

## Backup

On the server:

```bash
TS=$(date +%Y%m%d_%H%M%S)
mkdir -p /root/backups/n8n
cp /root/ChartSignl/apps/backend/deploy/docker-compose.yml "/root/backups/n8n/docker-compose.yml.bak_$TS"
docker run --rm -v deploy_n8n_data:/data:ro -v /root/backups/n8n:/backup alpine \
  tar czf "/backup/n8n_deploy_data_$TS.tar.gz" -C /data .
```

## Update (n8n only, same compose project)

```bash
cd /root/ChartSignl/apps/backend/deploy
docker-compose pull n8n
docker-compose up -d n8n
docker exec n8n n8n --version
```

Use `docker-compose` on this host if `docker compose` is not available.

## Hardening

- **`N8N_USER_MANAGEMENT_JWT_SECRET`** is required in `deploy/.env` (stable random hex). It was added on the server if missing.
- **HTTP basic auth** (`N8N_BASIC_AUTH_*`) optional second layer if n8n login + webhooks + nginx rate limiting / fail2ban are not where you want them.
- **HTTPS**: reverse proxy for `n8n.chartsignl.com` should enforce TLS; verify with `curl -sI https://n8n.chartsignl.com/` (expect `200` when n8n is ready).

Public URL checklist: strong owner password, review unauthenticated webhook entry points, nginx rate limiting or fail2ban for brute force.

## Before `git add` (no secrets in tracked files)

From the ChartSignl repo root; matches should only be `${...}` in compose and placeholders in `.env.example`:

```bash
grep -E "JWT_SECRET|PASSWORD|API_KEY" \
  apps/backend/deploy/docker-compose.yml \
  apps/backend/.env.example
```

## Ongoing maintenance

| Trigger | Action |
|--------|--------|
| Monthly | `docker-compose pull n8n && docker-compose up -d n8n` (from `deploy/`) |
| Before pull | `tar` volume `deploy_n8n_data` (see Backup above) |
| After pull | `curl -sf https://n8n.chartsignl.com/healthz` (or open the UI) and run one test workflow |

## Logs note

Newer images may log that the Python task runner is missing in the container; for production, use n8n’s external task runner guidance if you rely on Python tasks.
