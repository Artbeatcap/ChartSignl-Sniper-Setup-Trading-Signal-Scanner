"""
Setup 6: Prior Day Runner Fade Scanner
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

TWO PHASES:
  1. NIGHTLY SCAN (run after 4:30 PM ET)
     → Find stocks that ran 80%+ on 5x volume
     → Save to watchlist for tomorrow

  2. MORNING PREMARKET CHECK (run 8:00-9:15 AM ET)
     → For each watchlist ticker, check:
       - PM price < yesterday's high
       - Supertrend RED in PM
       - Price below 9 EMA
     → Fire alert with levels

From your journal: "Be patient for setups like this.
This is your bread and butter trades."
"""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np

from config import ScannerConfig
from polygon_client import PolygonClient
from indicators import ema, supertrend, atr, relative_volume
from range_floors import compute_levels, log_floor_decision

logger = logging.getLogger(__name__)


@dataclass
class RunnerCandidate:
    """A stock that qualified in the nightly scan."""
    ticker: str
    close_price: float
    pct_change: float
    volume: int
    relative_vol: float
    dollar_volume: float
    yesterday_high: float
    yesterday_low: float
    daily_atr: Optional[float]
    scan_date: str  # YYYY-MM-DD
    ema_200_daily: Optional[float] = None


@dataclass
class MorningAlert:
    """Fully qualified alert ready to display."""
    ticker: str
    setup: str = "PRIOR DAY RUNNER FADE"
    direction: str = "SHORT"
    # Key levels
    yesterday_high: float = 0.0
    pm_high: float = 0.0
    pm_low: float = 0.0
    ema_9_current: float = 0.0
    ema_200_daily: Optional[float] = None
    daily_atr: Optional[float] = None
    # State
    pm_price: float = 0.0
    supertrend_direction: str = ""  # "RED" or "GREEN"
    below_9ema: bool = False
    below_yesterday_high: bool = False
    # Meta
    entry: str = ""
    exit_plan: str = ""
    confidence: str = ""
    scan_date: str = ""
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    stop_was_widened: bool = False
    floor_source: str = ""


class RunnerFadeScanner:
    """
    The A+ Alert Machine for Setup 6.
    """

    def __init__(self, config: ScannerConfig):
        self.config = config
        self.rc = config.runner_fade
        self.client = PolygonClient(config.api)
        self.watchlist_path = Path(config.watchlist_file)

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 1: NIGHTLY SCAN
    # ═══════════════════════════════════════════════════════════════════

    def run_nightly_scan(self) -> list[RunnerCandidate]:
        """
        Find today's massive runners.
        Run this after market close (4:30 PM ET or later).

        Strategy:
          1. Hit the gainers snapshot — fast, single API call
          2. Filter by pct change, volume, price, dollar volume
          3. For survivors, pull daily bars to compute relative volume + ATR + 200 EMA
          4. Save watchlist to disk

        Returns list of qualified candidates.
        """
        logger.info("═══ NIGHTLY SCAN: Prior Day Runner Fade ═══")
        today = datetime.now().strftime("%Y-%m-%d")

        # Step 1: Get today's top gainers
        logger.info("Fetching gainers snapshot...")
        gainers = self.client.get_gainers(include_otc=False)
        logger.info(f"  → {len(gainers)} tickers from gainers endpoint")

        candidates = []
        if not gainers:
            logger.warning("No gainers returned. Market may be closed or API issue.")
        else:
            # Step 2: Fast-filter on snapshot data
            for g in gainers:
                ticker = g.get("ticker", "")
                day_data = g.get("day", {})
                prev_day = g.get("prevDay", {})

                # Extract fields
                close = day_data.get("c", 0)
                prev_close = prev_day.get("c", 0)
                volume = day_data.get("v", 0)
                high = day_data.get("h", 0)
                low = day_data.get("l", 0)

                if prev_close <= 0:
                    continue

                pct_change = ((close - prev_close) / prev_close) * 100
                dollar_volume = close * volume if close and volume else 0

                # Apply filters
                if pct_change < self.rc.min_pct_change:
                    continue
                if close < self.rc.min_price or close > self.rc.max_price:
                    continue
                if dollar_volume < self.rc.min_dollar_volume:
                    continue

                candidates.append({
                    "ticker": ticker,
                    "close": close,
                    "pct_change": pct_change,
                    "volume": volume,
                    "high": high,
                    "low": low,
                    "dollar_volume": dollar_volume,
                })

        logger.info(f"  → {len(candidates)} passed initial filters (>{self.rc.min_pct_change}% move, >${self.rc.min_dollar_volume/1e6:.0f}M dollar vol)")

        # Step 3: Deep validation — relative volume + ATR + 200 EMA
        qualified = self._deep_validate_runner_candidates(candidates, today)

        # Step 4: Save watchlist
        self._save_watchlist(qualified)
        logger.info(f"═══ NIGHTLY SCAN COMPLETE: {len(qualified)} runners on watchlist ═══")
        return qualified

    def deep_validate_fast_candidates(self, fast_candidates: list[dict]) -> list[RunnerCandidate]:
        """
        Run deep validation (daily bars, rel vol, ATR, 200 EMA) on pre-shaped
        fast-filter dicts — e.g. from UniverseBuilder.screen_runners().
        Does not save the watchlist.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        return self._deep_validate_runner_candidates(fast_candidates, today)

    def _deep_validate_runner_candidates(
        self, candidates: list[dict], today: str
    ) -> list[RunnerCandidate]:
        qualified: list[RunnerCandidate] = []

        for c in candidates:
            ticker = c["ticker"]
            logger.info(f"  Validating {ticker}...")

            daily_bars = self.client.get_daily_bars(ticker, days_back=self.rc.volume_lookback_days + 10)
            if len(daily_bars) < self.rc.volume_lookback_days:
                logger.info(f"    ✗ {ticker}: insufficient history ({len(daily_bars)} bars)")
                continue

            volumes = np.array([b["v"] for b in daily_bars], dtype=float)
            closes = np.array([b["c"] for b in daily_bars], dtype=float)
            highs = np.array([b["h"] for b in daily_bars], dtype=float)
            lows = np.array([b["l"] for b in daily_bars], dtype=float)

            # Relative volume check
            rel_vol = relative_volume(volumes, self.rc.volume_lookback_days)
            if rel_vol is None or rel_vol < self.rc.min_volume_multiplier:
                logger.info(
                    f"    ✗ {ticker}: relative volume {rel_vol:.1f}x < {self.rc.min_volume_multiplier}x"
                )
                continue

            # ATR for the alert (Rule 3: always include ATR)
            atr_vals = atr(highs, lows, closes, period=14)
            daily_atr = float(atr_vals[-1]) if not np.isnan(atr_vals[-1]) else None

            # 200 EMA daily — key level for entry
            ema_200 = ema(closes, 200)
            ema_200_val = float(ema_200[-1]) if len(ema_200) >= 200 and not np.isnan(ema_200[-1]) else None

            candidate = RunnerCandidate(
                ticker=ticker,
                close_price=c["close"],
                pct_change=round(c["pct_change"], 1),
                volume=int(c["volume"]),
                relative_vol=round(rel_vol, 1),
                dollar_volume=round(c["dollar_volume"], 0),
                yesterday_high=c["high"],
                yesterday_low=c["low"],
                daily_atr=round(daily_atr, 2) if daily_atr else None,
                scan_date=today,
                ema_200_daily=round(ema_200_val, 2) if ema_200_val else None,
            )
            qualified.append(candidate)
            logger.info(
                f"    ✓ {ticker}: +{candidate.pct_change}%, "
                f"{candidate.relative_vol}x vol, "
                f"ATR ${candidate.daily_atr}"
            )

        return qualified

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 2: MORNING PREMARKET CHECK
    # ═══════════════════════════════════════════════════════════════════

    def run_morning_check(self) -> list[MorningAlert]:
        """
        Check watchlist tickers against premarket data.
        Run this between 8:00 - 9:15 AM ET.

        For each watchlist ticker:
          1. Pull 5-min premarket bars
          2. Compute 9 EMA + Supertrend on PM bars
          3. Check: PM price < yesterday's high?
          4. Check: Supertrend RED?
          5. Check: Price below 9 EMA?
          6. If all pass → fire alert with levels

        Returns list of qualified morning alerts.
        """
        logger.info("═══ MORNING CHECK: Prior Day Runner Fade ═══")

        watchlist = self._load_watchlist()
        if not watchlist:
            logger.warning("Watchlist is empty. Run nightly scan first.")
            return []

        logger.info(f"Checking {len(watchlist)} tickers from nightly scan...")
        alerts: list[MorningAlert] = []

        for candidate in watchlist:
            ticker = candidate["ticker"]
            yesterday_high = candidate["yesterday_high"]
            logger.info(f"  Checking {ticker} (yesterday's high: ${yesterday_high:.2f})...")

            alert = self._evaluate_premarket(candidate)
            if alert:
                alerts.append(alert)
                logger.info(f"    🔴 ALERT: {ticker} — all conditions met!")
            else:
                logger.info(f"    ○ {ticker} — conditions not met (yet)")

        logger.info(f"═══ MORNING CHECK COMPLETE: {len(alerts)} alerts ═══")
        return alerts

    def _evaluate_premarket(self, candidate: dict) -> Optional[MorningAlert]:
        """
        Core premarket evaluation for a single ticker.
        This is where the rubber meets the road.
        """
        ticker = candidate["ticker"]
        yesterday_high = candidate["yesterday_high"]
        today = datetime.now().strftime("%Y-%m-%d")

        # Fetch 5-min bars (includes premarket via extended hours)
        bars = self.client.get_intraday_bars(ticker, minutes=5, date=today, extended_hours=True)

        if not bars or len(bars) < 3:
            logger.debug(f"    {ticker}: insufficient PM bars ({len(bars) if bars else 0})")
            return None

        # Only use premarket bars (before 9:30 ET)
        # Polygon timestamps are in ms UTC. ET = UTC - 4 (EDT) or UTC - 5 (EST)
        # Premarket: 4:00 AM - 9:30 AM ET
        # For robustness, we use all available bars — the 9 EMA calculation
        # naturally weights recent price action regardless of session.
        closes = np.array([b["c"] for b in bars], dtype=float)
        highs = np.array([b["h"] for b in bars], dtype=float)
        lows = np.array([b["l"] for b in bars], dtype=float)

        current_price = closes[-1]
        pm_high = float(np.max(highs))
        pm_low = float(np.min(lows))

        # ─── CHECK 1: PM price below yesterday's high ───
        below_yesterday_high = current_price < yesterday_high
        if not below_yesterday_high:
            logger.debug(f"    {ticker}: PM price ${current_price:.2f} >= yesterday high ${yesterday_high:.2f}")
            return None

        # ─── CHECK 2: Supertrend is RED ───
        if len(closes) >= self.rc.supertrend.period + 2:
            st_line, st_dir = supertrend(
                highs, lows, closes,
                period=self.rc.supertrend.period,
                multiplier=self.rc.supertrend.multiplier,
            )
            is_red = st_dir[-1] == -1  # -1 = bearish/red
        else:
            # Not enough bars for supertrend — skip this check but log it
            logger.debug(f"    {ticker}: not enough bars for supertrend ({len(closes)}), checking EMA only")
            is_red = True  # Default to passing if not enough data (EMA is the stronger filter)

        if not is_red:
            logger.debug(f"    {ticker}: supertrend is GREEN (bullish) — no short")
            return None

        # ─── CHECK 3: Price below 9 EMA ───
        ema_9 = ema(closes, self.rc.ema_period)
        ema_9_current = float(ema_9[-1]) if not np.isnan(ema_9[-1]) else current_price
        below_9ema = current_price < ema_9_current

        if not below_9ema:
            logger.debug(f"    {ticker}: price ${current_price:.2f} >= 9 EMA ${ema_9_current:.2f}")
            return None

        # ─── ALL CHECKS PASSED — Build alert ───
        ema_200_daily = candidate.get("ema_200_daily")
        daily_atr_val = candidate.get("daily_atr")

        # Construct entry/exit context (from your journal)
        entry_parts = [f"Short on break below PM low ${pm_low:.2f}"]
        if ema_200_daily and current_price > ema_200_daily:
            entry_parts.append(f"or 200 EMA ${ema_200_daily:.2f}")
        entry = " ".join(entry_parts)

        structural_stop = max(yesterday_high, pm_high) + 0.02
        if daily_atr_val is not None:
            struct_target = current_price - daily_atr_val
        else:
            struct_target = current_price

        lv = compute_levels(
            entry_price=current_price,
            structural_stop_price=structural_stop,
            structural_target_price=struct_target,
            price=current_price,
            daily_atr=daily_atr_val,
            ticker=ticker,
            direction="short",
            config=self.config.range_floor,
        )
        log_floor_decision(lv["log_entry"])
        if lv["skip"]:
            logger.info(f"    Suppressed {ticker}: {lv['skip_reason']}")
            return None

        adj_stop = lv["stop"]
        adj_target = lv["target"]
        stop_was_widened = lv["stop_was_widened"]
        floor_source = lv["floor_source"]

        exit_plan = (
            f"Stop ${adj_stop:.2f}. Cover by {self.rc.exit_time}. "
            f"Target ${adj_target:.2f}."
        )

        # Confidence scoring
        confidence_factors = []
        if candidate.get("relative_vol", 0) >= 10:
            confidence_factors.append("extreme volume")
        if candidate.get("pct_change", 0) >= 100:
            confidence_factors.append("100%+ runner")
        if is_red and below_9ema and below_yesterday_high:
            confidence_factors.append("triple confirmation")
        confidence = "HIGH" if len(confidence_factors) >= 2 else "MEDIUM"

        return MorningAlert(
            ticker=ticker,
            yesterday_high=yesterday_high,
            pm_high=pm_high,
            pm_low=pm_low,
            ema_9_current=round(ema_9_current, 2),
            ema_200_daily=ema_200_daily,
            daily_atr=daily_atr_val,
            pm_price=round(current_price, 2),
            supertrend_direction="RED" if is_red else "GREEN",
            below_9ema=below_9ema,
            below_yesterday_high=below_yesterday_high,
            entry=entry,
            exit_plan=exit_plan,
            confidence=f"{confidence} ({', '.join(confidence_factors)})" if confidence_factors else confidence,
            scan_date=candidate.get("scan_date", ""),
            stop_price=adj_stop,
            target_price=adj_target,
            stop_was_widened=stop_was_widened,
            floor_source=floor_source,
        )

    # ═══════════════════════════════════════════════════════════════════
    # PERSISTENCE
    # ═══════════════════════════════════════════════════════════════════

    def _save_watchlist(self, candidates: list[RunnerCandidate]):
        """Save nightly scan results for the morning check."""
        data = [asdict(c) for c in candidates]
        self.watchlist_path.write_text(json.dumps(data, indent=2))
        logger.info(f"Watchlist saved: {self.watchlist_path} ({len(data)} tickers)")

    def _load_watchlist(self) -> list[dict]:
        """Load the watchlist from the nightly scan."""
        if not self.watchlist_path.exists():
            return []
        try:
            data = json.loads(self.watchlist_path.read_text())
            # Validate it's from today or yesterday (not stale)
            if data:
                scan_date = data[0].get("scan_date", "")
                today = datetime.now().strftime("%Y-%m-%d")
                yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                if scan_date not in (today, yesterday):
                    logger.warning(f"Watchlist is stale (from {scan_date}). Run nightly scan again.")
                    return []
            return data
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Watchlist file corrupt: {e}")
            return []
