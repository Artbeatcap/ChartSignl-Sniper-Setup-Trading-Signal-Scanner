"""
Setup Sniper — Watchlist Persistence
Stores nightly scan results → morning premarket check reads them.
Simple JSON file-based storage. Upgrade to Redis/SQLite later if needed.
"""

import json
import os
import logging
from datetime import datetime
from typing import Optional

from config import CONFIG

logger = logging.getLogger(__name__)


def _watchlist_path() -> str:
    return CONFIG.watchlist_file


def save_watchlist(runners: list[dict]) -> None:
    """
    Save nightly scan results.
    Each runner dict should contain:
        ticker, pct_change, volume, avg_volume, rvol,
        close, high, low, open, atr, scan_date
    """
    payload = {
        "scan_date": datetime.now().strftime("%Y-%m-%d"),
        "scan_time": datetime.now().isoformat(),
        "count": len(runners),
        "runners": runners,
    }

    path = _watchlist_path()
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)

    logger.info(f"Saved {len(runners)} runners to {path}")


def load_watchlist() -> Optional[dict]:
    """
    Load the most recent watchlist.
    Returns None if file doesn't exist or is stale (>1 day old).
    """
    path = _watchlist_path()
    if not os.path.exists(path):
        logger.warning(f"No watchlist file found at {path}")
        return None

    with open(path, "r") as f:
        data = json.load(f)

    # Staleness check — watchlist should be from yesterday's close
    scan_date = data.get("scan_date", "")
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now().replace(hour=0, minute=0, second=0) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")

    # Allow today (if re-running) or yesterday (normal flow)
    if scan_date not in (today, yesterday):
        logger.warning(f"Watchlist is stale (from {scan_date}). Skipping.")
        return None

    logger.info(f"Loaded {data['count']} runners from {scan_date}")
    return data


def append_premarket_analysis(ticker: str, analysis: dict) -> None:
    """
    Append premarket analysis results to the watchlist for logging/review.
    """
    path = _watchlist_path()
    if not os.path.exists(path):
        return

    with open(path, "r") as f:
        data = json.load(f)

    for runner in data.get("runners", []):
        if runner["ticker"] == ticker:
            runner["premarket_analysis"] = analysis
            break

    with open(path, "w") as f:
        json.dump(data, f, indent=2)
