"""
Setup Sniper — Nightly Scanner (Run after 4:15 PM ET)
═══════════════════════════════════════════════════════
SETUP 6: Prior Day Runner Fade — Step 1 of 2

From your journal (Pages 8-9, GSIW example):
"Stock had 100%+ move previous day. Day 2: Does NOT get above prior day's high."
"Be patient for setups like this. This is your bread and butter trades."

This scan runs after close and finds stocks that:
  1. Gained 80%+ on the day
  2. Had 5x+ normal volume
  3. Meet minimum liquidity/price filters

Output: watchlist.json → consumed by scanner_premarket.py at 8 AM ET
"""

import logging
from datetime import datetime, timedelta

from polygon_client import PolygonClient
from indicators import atr as calc_atr, relative_volume
from watchlist import save_watchlist
from config import CONFIG

logger = logging.getLogger(__name__)
cfg = CONFIG.runner_fade


def run_nightly_scan(date: str = "") -> list[dict]:
    """
    Main nightly scan logic.

    Strategy: Use Polygon's grouped daily endpoint (all tickers in 1 call)
    to find today's big movers, then enrich with ATR for position sizing.

    Args:
        date: Override date (YYYY-MM-DD). Defaults to today.
    """
    client = PolygonClient()

    logger.info("=" * 60)
    logger.info("NIGHTLY SCAN — Prior Day Runner Detection")
    logger.info(f"Thresholds: {cfg.min_pct_change}%+ gain, {cfg.min_volume_multiple}x vol")
    logger.info("=" * 60)

    # ─────────────────────────────────────────────
    # STEP 1: Pull all daily bars (single API call)
    # ─────────────────────────────────────────────
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")

    logger.info(f"Fetching grouped daily bars for {date}...")
    all_bars = client.get_grouped_daily(date)

    if not all_bars:
        logger.warning("No bars returned. Market might be closed or API issue.")
        # Fallback: try gainers endpoint
        logger.info("Falling back to gainers snapshot endpoint...")
        all_bars = _fallback_gainers_scan(client)

    logger.info(f"Processing {len(all_bars)} tickers...")

    # ─────────────────────────────────────────────
    # STEP 2: Filter for runners
    # ─────────────────────────────────────────────
    runners = []
    for bar in all_bars:
        ticker = bar.get("T", bar.get("ticker", ""))
        open_price = bar.get("o", 0)
        high = bar.get("h", 0)
        low = bar.get("l", 0)
        close = bar.get("c", 0)
        volume = bar.get("v", 0)

        # Skip invalid data
        if not all([open_price, close, volume]):
            continue

        # Price filter
        if close < cfg.min_price or close > cfg.max_price:
            continue

        # Calculate % change
        pct_change = ((close - open_price) / open_price) * 100

        if pct_change < cfg.min_pct_change:
            continue

        runners.append({
            "ticker": ticker,
            "open": round(open_price, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(close, 2),
            "volume": int(volume),
            "pct_change": round(pct_change, 1),
            "date": date,
        })

    logger.info(f"Found {len(runners)} stocks with {cfg.min_pct_change}%+ gain")

    # ─────────────────────────────────────────────
    # STEP 3: Enrich with volume analysis + ATR
    # ─────────────────────────────────────────────
    enriched = []
    for runner in runners:
        ticker = runner["ticker"]
        logger.info(f"  Enriching {ticker} ({runner['pct_change']}% gain)...")

        try:
            # Get 30-day daily bars for avg volume + ATR calculation
            daily_bars = client.get_daily_bars(ticker, lookback_days=45)

            if len(daily_bars) < 15:
                logger.debug(f"  {ticker}: Not enough history ({len(daily_bars)} bars). Skipping.")
                continue

            # Calculate average volume (exclude today)
            volumes = [b["v"] for b in daily_bars[:-1] if b.get("v")]
            avg_vol = sum(volumes) / len(volumes) if volumes else 0

            # Volume filter: must be 5x+ normal
            rvol = relative_volume(runner["volume"], avg_vol)
            if rvol < cfg.min_volume_multiple:
                logger.debug(f"  {ticker}: RVOL {rvol:.1f}x < {cfg.min_volume_multiple}x threshold. Skipping.")
                continue

            # Avg volume floor (liquidity check)
            if avg_vol < cfg.min_avg_volume:
                logger.debug(f"  {ticker}: Avg vol {avg_vol:,.0f} < {cfg.min_avg_volume:,} floor. Skipping.")
                continue

            # Calculate ATR (Rule 3: include in every alert)
            highs = [b["h"] for b in daily_bars]
            lows = [b["l"] for b in daily_bars]
            closes = [b["c"] for b in daily_bars]
            atr_values = calc_atr(highs, lows, closes, cfg.atr_period)
            current_atr = atr_values[-1] if atr_values else None

            runner["avg_volume"] = int(avg_vol)
            runner["rvol"] = round(rvol, 1)
            runner["atr"] = round(current_atr, 2) if current_atr else None
            runner["atr_pct"] = (
                round((current_atr / runner["close"]) * 100, 1) if current_atr else None
            )
            runner["scan_date"] = date

            enriched.append(runner)
            logger.info(
                f"  ✓ {ticker}: +{runner['pct_change']}% | "
                f"RVOL {rvol:.1f}x | ATR ${current_atr:.2f} ({runner['atr_pct']}%)"
            )

        except Exception as e:
            logger.error(f"  Error enriching {ticker}: {e}")
            continue

    # ─────────────────────────────────────────────
    # STEP 4: Sort by conviction (pct_change * rvol)
    # ─────────────────────────────────────────────
    enriched.sort(key=lambda x: x["pct_change"] * x.get("rvol", 1), reverse=True)

    # ─────────────────────────────────────────────
    # STEP 5: Save to watchlist
    # ─────────────────────────────────────────────
    save_watchlist(enriched)

    logger.info("=" * 60)
    logger.info(f"NIGHTLY SCAN COMPLETE — {len(enriched)} runners on watchlist")
    for r in enriched:
        logger.info(
            f"  {r['ticker']:>6s}  +{r['pct_change']:>6.1f}%  "
            f"RVOL {r['rvol']:>5.1f}x  ATR ${r.get('atr', 0):>6.2f} ({r.get('atr_pct', 0)}%)"
        )
    logger.info("=" * 60)

    client.close()
    return enriched


def _fallback_gainers_scan(client: PolygonClient) -> list[dict]:
    """
    Fallback: Use the gainers snapshot endpoint if grouped daily fails.
    Less comprehensive but doesn't require a date parameter.
    """
    gainers = client.get_gainers()
    converted = []
    for g in gainers:
        day = g.get("day", {})
        converted.append({
            "T": g.get("ticker", ""),
            "o": day.get("o", 0),
            "h": day.get("h", 0),
            "l": day.get("l", 0),
            "c": day.get("c", 0),
            "v": day.get("v", 0),
        })
    return converted


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    run_nightly_scan()
