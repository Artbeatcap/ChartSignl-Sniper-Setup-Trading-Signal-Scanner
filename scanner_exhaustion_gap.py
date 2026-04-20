"""
Setup 7: Exhaustion Gap Short Scanner
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Two phases:
  1. NIGHTLY SCAN
     → Use gainers snapshot + previous close to find gap-up names
     → Filter by gap %, liquidity, and relative volume
     → Save to exhaustion-gap watchlist

  2. MORNING PREMARKET CHECK
     → For each watchlist ticker, check:
       - Price below 9 EMA on 5-min chart
       - Supertrend RED
     → Fire alert with levels
"""
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np

from config import ScannerConfig
from polygon_client import PolygonClient
from indicators import ema, supertrend, atr, relative_volume

logger = logging.getLogger(__name__)


@dataclass
class ExhaustionGapCandidate:
    """A stock that qualified from the exhaustion gap nightly scan."""
    ticker: str
    gap_pct: float
    close_price: float
    open_price: float
    dollar_volume: float
    relative_vol: Optional[float]
    daily_atr: Optional[float]
    scan_date: str
    days_on_watch: int = 0        # Incremented each nightly scan
    first_seen_date: str = ""     # Date first added to watchlist (YYYY-MM-DD)


@dataclass
class ExhaustionGapAlert:
    """Fully qualified alert for Setup 7."""
    ticker: str
    setup: str = "EXHAUSTION GAP SHORT"
    direction: str = "SHORT"
    # Key levels
    prior_close: float = 0.0
    gap_open: float = 0.0
    pm_high: float = 0.0
    pm_low: float = 0.0
    ema_9_current: float = 0.0
    daily_atr: Optional[float] = None
    gap_pct: float = 0.0
    # State
    pm_price: float = 0.0
    supertrend_direction: str = ""
    below_9ema: bool = False
    # Meta
    entry: str = ""
    exit_plan: str = ""
    confidence: str = ""
    scan_date: str = ""


class ExhaustionGapScanner:
    """Setup 7: Exhaustion Gap Short."""

    def __init__(self, config: ScannerConfig):
        self.config = config
        self.gc = config.exhaustion_gap
        self.client = PolygonClient(config.api)
        self.watchlist_path = Path(config.exhaustion_watchlist_file)

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 1: NIGHTLY SCAN
    # ═══════════════════════════════════════════════════════════════════

    def run_nightly_scan(self) -> list[ExhaustionGapCandidate]:
        """
        Find gap-up runners using gainers + previous close and session open.
        """
        logger.info("═══ NIGHTLY SCAN: Exhaustion Gap Short ═══")
        today = datetime.now().strftime("%Y-%m-%d")

        gainers = self.client.get_gainers(include_otc=False)
        logger.info(f"  → {len(gainers)} tickers from gainers endpoint")

        candidates: list[ExhaustionGapCandidate] = []

        for g in gainers:
            c = self.try_build_from_snapshot_gainer(g, today)
            if c:
                candidates.append(c)
                logger.info(
                    f"  ✓ {c.ticker}: gap +{c.gap_pct}%, "
                    f"{c.relative_vol}x vol, "
                    f"${c.close_price:.2f} close"
                )

        self._save_watchlist(candidates)
        logger.info(f"═══ NIGHTLY SCAN COMPLETE: {len(candidates)} exhaustion gaps on watchlist ═══")
        return candidates

    def try_build_from_snapshot_gainer(
        self, g: dict, scan_date: Optional[str] = None
    ) -> Optional[ExhaustionGapCandidate]:
        """
        Build one ExhaustionGapCandidate from a Polygon snapshot/gainers row (day + prevDay).
        Returns None if thresholds are not met.
        """
        today = scan_date or datetime.now().strftime("%Y-%m-%d")
        ticker = g.get("ticker", "")
        day_data = g.get("day", {})
        prev_day = g.get("prevDay", {})

        prev_close = float(prev_day.get("c", 0.0) or 0.0)
        open_price = float(day_data.get("o", 0.0) or 0.0)
        close_price = float(day_data.get("c", 0.0) or 0.0)
        volume = float(day_data.get("v", 0.0) or 0.0)

        if not ticker or prev_close <= 0 or open_price <= 0:
            return None

        gap_pct = ((open_price - prev_close) / prev_close) * 100.0
        if gap_pct < self.gc.min_gap_up_pct:
            return None

        dollar_volume = close_price * volume
        if close_price < self.gc.min_price or close_price > self.gc.max_price:
            return None
        if dollar_volume < self.gc.min_dollar_volume:
            return None

        daily_bars = self.client.get_daily_bars(ticker, days_back=self.gc.volume_lookback_days + 10)
        if len(daily_bars) < self.gc.volume_lookback_days:
            return None

        volumes = np.array([b["v"] for b in daily_bars], dtype=float)
        highs = np.array([b["h"] for b in daily_bars], dtype=float)
        lows = np.array([b["l"] for b in daily_bars], dtype=float)
        closes = np.array([b["c"] for b in daily_bars], dtype=float)

        rel_vol = relative_volume(volumes, self.gc.volume_lookback_days)
        if rel_vol is None or rel_vol < self.gc.min_volume_multiplier:
            return None

        atr_vals = atr(highs, lows, closes, period=14)
        last_atr = float(atr_vals[-1]) if not np.isnan(atr_vals[-1]) else None

        return ExhaustionGapCandidate(
            ticker=ticker,
            gap_pct=round(gap_pct, 1),
            close_price=round(close_price, 2),
            open_price=round(open_price, 2),
            dollar_volume=round(dollar_volume, 0),
            relative_vol=round(rel_vol, 1) if rel_vol else None,
            daily_atr=round(last_atr, 2) if last_atr else None,
            scan_date=today,
        )

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 2: MORNING PREMARKET CHECK
    # ═══════════════════════════════════════════════════════════════════

    def run_morning_check(self) -> list[ExhaustionGapAlert]:
        """Evaluate exhaustion gap watchlist versus premarket price action."""
        logger.info("═══ MORNING CHECK: Exhaustion Gap Short ═══")

        watchlist = self._load_watchlist()
        if not watchlist:
            logger.info("Exhaustion gap watchlist is empty.")
            return []

        alerts: list[ExhaustionGapAlert] = []
        for candidate in watchlist:
            ticker = candidate["ticker"]
            logger.info(f"  Checking {ticker}...")
            alert = self._evaluate_premarket(candidate)
            if alert:
                alerts.append(alert)
                logger.info(f"    🔴 ALERT: {ticker} — exhaustion gap short setup")
            else:
                logger.info(f"    ○ {ticker} — conditions not met")

        logger.info(f"═══ MORNING CHECK COMPLETE: {len(alerts)} alerts ═══")
        return alerts

    def _evaluate_premarket(self, candidate: dict) -> Optional[ExhaustionGapAlert]:
        ticker = candidate["ticker"]
        today = datetime.now().strftime("%Y-%m-%d")

        bars = self.client.get_intraday_bars(ticker, minutes=self.gc.ema_timeframe_minutes, date=today, extended_hours=True)
        if not bars or len(bars) < self.gc.ema_period + 1:
            return None

        closes = np.array([b["c"] for b in bars], dtype=float)
        highs = np.array([b["h"] for b in bars], dtype=float)
        lows = np.array([b["l"] for b in bars], dtype=float)

        current_price = float(closes[-1])
        pm_high = float(np.max(highs))
        pm_low = float(np.min(lows))

        # Supertrend direction
        if len(closes) >= self.gc.supertrend.period + 2:
            _, st_dir = supertrend(
                highs,
                lows,
                closes,
                period=self.gc.supertrend.period,
                multiplier=self.gc.supertrend.multiplier,
            )
            is_red = st_dir[-1] == -1
        else:
            is_red = True

        # 9 EMA filter
        ema_9 = ema(closes, self.gc.ema_period)
        ema_9_current = float(ema_9[-1]) if not np.isnan(ema_9[-1]) else current_price
        below_9ema = current_price < ema_9_current

        if not (is_red and below_9ema):
            return None

        daily_atr_val = candidate.get("daily_atr")

        prior_close = candidate.get("close_price", 0.0)
        gap_open = candidate.get("open_price", 0.0)
        gap_pct = candidate.get("gap_pct", 0.0)

        entry = f"Short on break below PM low ${pm_low:.2f} after gap-up open ${gap_open:.2f}"
        if daily_atr_val:
            target = round(current_price - daily_atr_val, 2)
            exit_plan = f"Cover by 10:00 AM ET. ATR target: ${target:.2f} (1 ATR below current)"
        else:
            exit_plan = "Cover by 10:00 AM ET."

        confidence_factors = []
        if gap_pct >= self.gc.min_gap_up_pct + 5:
            confidence_factors.append("large gap")
        if candidate.get("relative_vol", 0) and candidate["relative_vol"] >= self.gc.min_volume_multiplier * 2:
            confidence_factors.append("extreme volume")
        if is_red and below_9ema:
            confidence_factors.append("trend aligned")

        confidence = "HIGH" if len(confidence_factors) >= 2 else "MEDIUM"

        return ExhaustionGapAlert(
            ticker=ticker,
            prior_close=prior_close,
            gap_open=gap_open,
            pm_high=pm_high,
            pm_low=pm_low,
            ema_9_current=round(ema_9_current, 2),
            daily_atr=daily_atr_val,
            gap_pct=gap_pct,
            pm_price=round(current_price, 2),
            supertrend_direction="RED" if is_red else "GREEN",
            below_9ema=below_9ema,
            entry=entry,
            exit_plan=exit_plan,
            confidence=f"{confidence} ({', '.join(confidence_factors)})" if confidence_factors else confidence,
            scan_date=candidate.get("scan_date", ""),
        )

    # ═══════════════════════════════════════════════════════════════════
    # PERSISTENCE
    # ═══════════════════════════════════════════════════════════════════

    def _save_watchlist(self, new_candidates: list[ExhaustionGapCandidate]):
        """Merge new candidates into existing watchlist (don't overwrite)."""
        existing = self._load_watchlist()
        existing_tickers = {c.get("ticker") for c in existing}

        merged = list(existing)
        today_str = datetime.now().strftime("%Y-%m-%d")

        for c in new_candidates:
            c_dict = asdict(c) if hasattr(c, "__dataclass_fields__") else c
            ticker = c_dict.get("ticker")
            if ticker not in existing_tickers:
                c_dict["first_seen_date"] = c_dict.get("scan_date", today_str)
                c_dict["days_on_watch"] = 0
                merged.append(c_dict)
                logger.info(f"  + New: {ticker}")
            else:
                logger.debug(f"  = Already watching: {ticker}")

        self.watchlist_path.write_text(json.dumps(merged, indent=2))
        logger.info(
            f"Exhaustion watchlist: {len(merged)} total "
            f"({len(new_candidates)} new, {len(existing)} existing)"
        )

    def _load_watchlist(self) -> list[dict]:
        """Load watchlist, keeping candidates alive for up to max_watch_days."""
        if not self.watchlist_path.exists():
            return []
        try:
            data = json.loads(self.watchlist_path.read_text())
            today_str = datetime.now().strftime("%Y-%m-%d")
            max_days = 5

            active: list[dict] = []
            for candidate in data:
                first_seen = candidate.get("first_seen_date", candidate.get("scan_date", ""))
                if not first_seen:
                    continue
                try:
                    days_old = (
                        datetime.strptime(today_str, "%Y-%m-%d")
                        - datetime.strptime(first_seen, "%Y-%m-%d")
                    ).days
                except ValueError:
                    continue

                if days_old <= max_days:
                    candidate["days_on_watch"] = days_old
                    active.append(candidate)
                else:
                    logger.info(
                        f"  Dropping {candidate.get('ticker', '?')} — "
                        f"{days_old} days old (max {max_days})"
                    )

            return active
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Exhaustion gap watchlist file corrupt: {e}")
            return []

