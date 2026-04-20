# Setup Sniper — Phase 1: Prior Day Runner Fade

> *"Be patient for setups like this. This is your bread and butter trades."*

## What This Does

Two automated scans that find **yesterday's 80%+ runners** and evaluate them in **premarket** for your A+ short setup:

```
4:30 PM ET → NIGHTLY SCAN → finds runners → saves watchlist
8:00 AM ET → MORNING CHECK → evaluates PM data → fires alerts
```

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your API key
cp .env.template .env
# Edit .env → add your Polygon.io API key

# 3. Source your env (or use python-dotenv)
export MASSIVE_API_KEY=your_key

# 4. Test connectivity
python main.py test

# 5. Run scans
python main.py nightly    # After market close
python main.py morning    # Premarket 8:00-9:15 AM ET
```

## Architecture

```
setup-sniper/
├── main.py                  # CLI entry point (nightly / morning / test)
├── config.py                # All thresholds and settings — no magic numbers
├── polygon_client.py        # Polygon.io API client (rate-limited)
├── indicators.py            # EMA, ATR, Supertrend — pure numpy
├── scanner_runner_fade.py   # Setup 6 logic: nightly scan + morning check
├── alerts.py                # Console + Discord + log file alerts
├── requirements.txt
├── .env.template
└── watchlist.json           # Auto-generated: nightly scan → morning check
```

## Scan Logic (from your journal)

### Nightly Scan
- Stock moved **80%+** in a single day
- Volume was **5x** the 20-day average
- Minimum **$5M dollar volume** (ensures shortability)
- Price between $1–$500

### Morning Premarket Check
For each nightly watchlist ticker:
1. ✅ PM price **below yesterday's high** (Day 2 NOT reclaiming)
2. ✅ Supertrend is **RED** in premarket
3. ✅ Price **below 9 EMA** (5-min)

If all three pass → **ALERT fires** with entry/exit levels.

### Alert Includes (Rule 3: ATR in every alert)
- PM high/low (entry levels)
- Yesterday's high (invalidation level)
- 9 EMA + 200 EMA daily (confluence levels)
- Daily ATR (position sizing + target)
- Entry: "Short on break below PM low or 200 EMA"
- Exit: "Cover by 10:00 AM ET"

## Scheduling (Cron / Task Scheduler)

### Mac/Linux (crontab)
```bash
crontab -e

# Nightly scan: 4:35 PM ET Mon-Fri
35 16 * * 1-5 cd /path/to/setup-sniper && /usr/bin/python3 main.py nightly >> scan.log 2>&1

# Morning check: 8:00 AM ET Mon-Fri
0 8 * * 1-5 cd /path/to/setup-sniper && /usr/bin/python3 main.py morning >> scan.log 2>&1
```

### Windows (Task Scheduler)
Create two tasks pointing to `python main.py nightly` and `python main.py morning` with appropriate triggers.

## Config You Need to Confirm

Open `config.py` and verify:

1. **Supertrend settings** — currently `period=10, multiplier=3.0`. What do you run on your charts?
2. **Rate limit** — set to 5/min (Polygon free tier). If you have a paid plan, increase `rate_limit_per_min`.
3. **Min pct change** — set to 80%. Your notes say 100%+ for the best setups but spec says 80%. Adjust as needed.

## Next: Phase 1 Expansion

After this is running, the next two scanners plug into the same framework:
- **Setup 3: Earnings Miss Short** (earnings calendar + PM 9 EMA check)
- **Setup 7: Exhaustion Gap Short** (multi-day parabolic tracking)
