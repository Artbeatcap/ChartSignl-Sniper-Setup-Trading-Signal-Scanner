# Setup Sniper

A Python CLI trading-signal scanner. It pulls US equity market data from a
Polygon.io-compatible endpoint (Massive.com) and evaluates short-setup scanners,
then delivers alerts to console / Discord / Slack / email.

There is no web UI and no database — scan commands run via `main.py` (and cron
in the Docker deployment).

## Cursor Cloud specific instructions

- Runtime is system Python 3.12. Invoke everything with `python3`. Dependencies
  are installed with `pip install --break-system-packages -r requirements.txt`
  (`requests`, `numpy`, `python-dotenv`).
- **Required secret:** `MASSIVE_API_KEY` (Massive.com / Polygon.io). The Python
  client calls `https://api.polygon.io` directly. ChartSignl MCP has its own
  server-side credentials and **cannot** supply this env var to the scanner.
- Add the key as a **Runtime Secret** named exactly `MASSIVE_API_KEY` in the
  [Cloud Agents dashboard](https://cursor.com/dashboard/cloud-agents) (Secrets
  tab). Do not put the value in git, `environment.json`, or chat. After adding
  it, start a **new** Cloud Agent — existing runs do not pick up new secrets.
- `scripts/materialize_env.py` (Cloud `start` hook) copies injected secrets into
  a gitignored `.env` (mode 0600) so `python-dotenv` and Docker Compose see them.
- Optional integrations (Discord, Slack, email, FMP, Anthropic) disable when
  unset. Same Secrets tab, same env names as `.env.template`.
- Hello-world check: `python3 main.py test`. Other scans are time-of-day aware
  and often return no candidates outside US hours even with a valid key.
- There are no automated tests in the repo. Do not "fix" pre-existing ruff
  warnings in untouched source as part of unrelated work.
- Generated runtime files (`data/`, `logs/`, watchlist JSON contents) are local
  state. Docker Compose is the production cron stack, not required for Cloud
  Agent scans.
