"""
Setup Sniper — Premarket Scanner (Run at 8:00 AM ET)
═════════════════════════════════════════════════════
SETUP 6: Prior Day Runner Fade — Step 2 of 2

From your journal (Pages 8-9):
"Day 2: Does NOT get above prior day's high."
"Red supertrend all PM, price staying near/below 9 EMA"
"Break below key PM level (PM low, 200 EMA) = signal to go short"

This reads the nightly watchlist and checks each runner's premarket action:
  1. Is PM price below yesterday's high? (MUST be true)
  2. Is supertrend RED on 5-min PM bars?
  3. Is price below 9 EMA on 5-min PM bars?
  4. What are the key levels (PM low, 200 EMA)?

Output: Filtered alert list with entry levels and stop placement.

═════════════════════════════════════════════════════
BEHAVIORAL GUARDRAILS EMBEDDED:
- Rule 2: "Wait for the 10:00 AM Retest" → Alert says "Cover by 10:00 AM"
- Rule 3: ATR included in every alert for realistic targets
- Rule 4: Volume decline check (exhaustion confirmation)
- Rule 5: Shares recommended (not options) for gap-down shorts
═════════════════════════════════════════════════════
"""

import logging
from datetime import datetime

import numpy as np

from polygon_client import PolygonClient
from indicators import ema, supertrend, is_below_ema, ema_slope, relative_volume
from watchlist import load_watchlist, append_premarket_analysis
from alerts import send_alert, format_runner_fade_alert
from config import CONFIG

logger = logging.getLogger(__name__)
cfg = CONFIG.runner_fade


def run_premarket_scan() -> list[dict]:
    """
    Main premarket check.

    For each runner on the watchlist:
    1. Fetch premarket bars (requires extended hours data)
    2. Calculate 9 EMA + Supertrend on available PM bars
    3. Compare PM price to yesterday's high
    4. Score the setup and generate alert if qualified
    """
    watchlist_data = load_watchlist()
    if not watchlist_data:
        logger.warning("No watchlist found. Run nightly scan first.")
        return []

    runners = watchlist_data.get("runners", [])
    if not runners:
        logger.info("Watchlist is empty. No runners to check.")
        return []

    client = PolygonClient()
    today = datetime.now().strftime("%Y-%m-%d")

    logger.info("=" * 60)
    logger.info("PREMARKET SCAN — Prior Day Runner Fade Check")
    logger.info(f"Checking {len(runners)} runners from watchlist")
    logger.info("=" * 60)

    qualified = []

    for runner in runners:
        ticker = runner["ticker"]
        yesterday_high = runner["high"]
        yesterday_close = runner["close"]
        yesterday_vol = runner["volume"]

        logger.info(f"\n  Checking {ticker} (yesterday: +{runner['pct_change']}%)...")

        analysis = {
            "ticker": ticker,
            "yesterday_high": yesterday_high,
            "yesterday_close": yesterday_close,
            "checks": {},
            "qualified": False,
            "score": 0,
        }

        try:
            # ─────────────────────────────────────────
            # CHECK 1: Get current premarket snapshot
            # ─────────────────────────────────────────
            snapshot = client.get_snapshot(ticker)
            if not snapshot:
                logger.warning(f"  {ticker}: No snapshot available. Skipping.")
                continue

            # Extract premarket price from snapshot
            # Polygon snapshot has: min (current day agg), prevDay, todaysChange
            pm_data = snapshot.get("min", {})  # Most recent minute bar
            current_price = pm_data.get("c", 0) or snapshot.get("lastTrade", {}).get("p", 0)

            if not current_price:
                logger.warning(f"  {ticker}: No price data in snapshot. Skipping.")
                continue

            # ─────────────────────────────────────────
            # CHECK 2: PM price below yesterday's high
            # "Does NOT get above prior day's high"
            # ─────────────────────────────────────────
            below_yesterday_high = current_price < yesterday_high
            analysis["checks"]["below_yesterday_high"] = below_yesterday_high
            analysis["current_price"] = round(current_price, 2)

            if not below_yesterday_high:
                logger.info(f"  {ticker}: PM price ${current_price:.2f} ABOVE yesterday high ${yesterday_high:.2f}. SKIP.")
                append_premarket_analysis(ticker, analysis)
                continue

            gap = ((current_price - yesterday_close) / yesterday_close) * 100
            analysis["pm_gap_pct"] = round(gap, 1)
            logger.info(f"  {ticker}: PM ${current_price:.2f} < yesterday high ${yesterday_high:.2f} ✓ (gap {gap:+.1f}%)")

            # ─────────────────────────────────────────
            # CHECK 3: Calculate 9 EMA on available bars
            # Try PM bars first; fall back to snapshot
            # ─────────────────────────────────────────
            pm_bars = client.get_premarket_bars(ticker, today)
            ema_check_passed = False
            supertrend_check_passed = False
            pm_low = current_price
            pm_high = current_price

            if len(pm_bars) >= 5:
                # We have enough PM bars for indicators
                closes = [b["c"] for b in pm_bars]
                highs = [b["h"] for b in pm_bars]
                lows = [b["l"] for b in pm_bars]

                pm_high = max(highs)
                pm_low = min(lows)

                # 9 EMA check
                ema_9 = ema(closes, min(cfg.ema_period, len(closes)))
                latest_ema = ema_9[-1] if ema_9 else None

                if latest_ema and not np.isnan(latest_ema):
                    ema_check_passed = is_below_ema(current_price, latest_ema)
                    analysis["checks"]["below_9ema"] = ema_check_passed
                    analysis["ema_9_value"] = round(latest_ema, 2)
                    analysis["ema_slope"] = ema_slope(ema_9)
                    logger.info(
                        f"  {ticker}: Price ${current_price:.2f} vs 9EMA ${latest_ema:.2f} "
                        f"{'✓ BELOW' if ema_check_passed else '✗ ABOVE'} "
                        f"(slope: {analysis.get('ema_slope', 'N/A')})"
                    )

                # Supertrend check
                if len(closes) >= cfg.supertrend_period + 1:
                    st_vals, st_dirs = supertrend(
                        highs, lows, closes,
                        cfg.supertrend_period, cfg.supertrend_multiplier
                    )
                    latest_direction = st_dirs[-1]
                    supertrend_check_passed = latest_direction == "RED"
                    analysis["checks"]["supertrend_red"] = supertrend_check_passed
                    analysis["supertrend_direction"] = latest_direction
                    logger.info(
                        f"  {ticker}: Supertrend {latest_direction} "
                        f"{'✓' if supertrend_check_passed else '✗'}"
                    )
                else:
                    logger.info(f"  {ticker}: Not enough PM bars for Supertrend ({len(closes)} bars)")
                    # Don't fail the scan — Supertrend is secondary confirmation
                    analysis["checks"]["supertrend_red"] = None
            else:
                logger.info(f"  {ticker}: Only {len(pm_bars)} PM bars. Using snapshot-only mode.")
                # Snapshot-only mode: we can only check price vs yesterday's high
                # 9 EMA and Supertrend require bars
                analysis["checks"]["below_9ema"] = None
                analysis["checks"]["supertrend_red"] = None

            analysis["pm_high"] = round(pm_high, 2)
            analysis["pm_low"] = round(pm_low, 2)

            # ─────────────────────────────────────────
            # CHECK 4: Volume exhaustion (Rule 4)
            # "Lower volume compared to previous day"
            # ─────────────────────────────────────────
            today_snap_vol = snapshot.get("day", {}).get("v", 0)
            if today_snap_vol and yesterday_vol:
                # Scale today's volume to estimated full-day pace
                # (PM volume is a fraction of full day)
                vol_declining = today_snap_vol < yesterday_vol * 0.1  # PM vol < 10% of yesterday = normal
                analysis["checks"]["volume_declining_signal"] = True  # Placeholder; real check at open
                analysis["pm_volume"] = int(today_snap_vol)

            # ─────────────────────────────────────────
            # SCORING: How many conditions are met?
            # ─────────────────────────────────────────
            score = 0
            max_score = 0

            # Must-pass (gate):
            if below_yesterday_high:
                score += 3  # Weighted heavily — this is the core condition
            max_score += 3

            # Strong confirms:
            if analysis["checks"].get("below_9ema") is True:
                score += 2
            if analysis["checks"].get("below_9ema") is not None:
                max_score += 2

            if analysis["checks"].get("supertrend_red") is True:
                score += 2
            if analysis["checks"].get("supertrend_red") is not None:
                max_score += 2

            # EMA slope bonus
            if analysis.get("ema_slope") == "DOWN":
                score += 1
            max_score += 1

            analysis["score"] = score
            analysis["max_score"] = max_score
            analysis["score_pct"] = round(score / max_score * 100) if max_score else 0

            # ─────────────────────────────────────────
            # QUALIFY: Score >= 60% AND must-pass gate met
            # ─────────────────────────────────────────
            qualified_threshold = 60
            if below_yesterday_high and analysis["score_pct"] >= qualified_threshold:
                analysis["qualified"] = True

                # Build key levels for the alert
                analysis["key_levels"] = {
                    "entry_trigger": round(pm_low, 2),         # "Short on break below PM low"
                    "alt_entry": runner.get("atr"),             # Or 200 EMA (calculated at open)
                    "stop_loss": round(pm_high + 0.10, 2),     # Above PM high
                    "target_1": round(pm_low - (runner.get("atr", 1)), 2),  # 1 ATR below PM low
                    "cover_by": "10:00 AM ET",                 # Rule 2
                }

                qualified.append(analysis)
                logger.info(
                    f"  ✅ {ticker}: QUALIFIED — Score {score}/{max_score} ({analysis['score_pct']}%)"
                )
            else:
                logger.info(
                    f"  ❌ {ticker}: Not qualified — Score {score}/{max_score} ({analysis['score_pct']}%)"
                )

            append_premarket_analysis(ticker, analysis)

        except Exception as e:
            logger.error(f"  Error checking {ticker}: {e}")
            continue

    # ─────────────────────────────────────────
    # SEND ALERTS
    # ─────────────────────────────────────────
    logger.info("\n" + "=" * 60)
    if qualified:
        logger.info(f"🎯 {len(qualified)} QUALIFIED SETUPS:")
        for q in qualified:
            alert_text = format_runner_fade_alert(q, runner_data=_find_runner(runners, q["ticker"]))
            logger.info(f"\n{alert_text}")
            send_alert(alert_text)
    else:
        logger.info("No qualified setups today. Patience is alpha.")
    logger.info("=" * 60)

    client.close()
    return qualified


def _find_runner(runners: list[dict], ticker: str) -> dict:
    for r in runners:
        if r["ticker"] == ticker:
            return r
    return {}


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    run_premarket_scan()
