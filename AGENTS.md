# Setup Sniper

A Python CLI trading-signal scanner ("Setup Sniper"). It pulls US equity market data
from a Polygon.io-compatible endpoint (Massive.com) and evaluates short-setup
scanners (runner fade, earnings miss, exhaustion gap, intraday, VIX, regime), then
delivers alerts to console / Discord / Slack / email.

There is no web UI and no database — it is a set of scan commands run via `main.py`
(and scheduled with cron in the Docker deployment).

## Cursor Cloud specific instructions

- Runtime is system Python 3.12; there is no virtualenv. Invoke everything with
  `python3` (the `python` alias is not installed). Dependencies are installed with
  `pip install --break-system-packages -r requirements.txt` (only 3 libs:
  `requests`, `numpy`, `python-dotenv`).
- Configuration is env-driven (see `.env.template`, loaded via `python-dotenv`).
  Copy it to `.env` (`.env` is gitignored). `ScannerConfig()` raises `ValueError`
  at startup unless `MASSIVE_API_KEY` is set — this key is the only hard requirement
  to run any real command. All other keys/webhooks (Discord, Slack, email, FMP,
  Anthropic catalyst briefing) are optional and features silently disable when unset.
- `MASSIVE_API_KEY` is a Massive.com / Polygon.io API key. The client
  (`polygon_client.py`) calls `https://api.polygon.io` directly, so the key must be
  valid for Polygon endpoints. With a placeholder key the app runs but every request
  returns HTTP 401. NOTE: the `ChartSignl MCP` server available to the agent uses
  Massive credentials server-side and is unrelated to this env var — it cannot supply
  a key to the Python app.
- Commands (`python3 main.py <cmd>`): `test` (connectivity + indicator smoke test —
  best hello-world check), `nightly`, `morning`, `morning-fresh`, `intraday`
  (`--once` for a single poll cycle instead of an open-ended loop), `regime`, `full`.
  Run `python3 main.py` with no args to print full usage.
- Scanners are time-of-day aware and depend on live/premarket market data, so most
  scans return "no candidates" outside US market hours even with a valid key. Use
  `python3 main.py test` to verify the environment end-to-end regardless of clock.
- There are no automated tests and no configured linter in the repo. `ruff check`
  reports pre-existing warnings (unused imports/vars) in the untouched source; do not
  "fix" these as part of unrelated work.
- Generated runtime files (`data/`, `logs/`, `*.log`, `watchlist*.json` contents) are
  gitignored / local state. Docker deploy (`docker-compose.yml`) is for the scheduled
  cron production stack, not needed for local dev runs.
