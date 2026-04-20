"""
Setup 3: Earnings Miss Short Scanner
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Two phases:
  1. NIGHTLY SCAN
     → Find stocks that *just* reported earnings and MISSED (EPS + revenue)
     → Save to earnings-miss watchlist

  2. MORNING PREMARKET CHECK
     → For each watchlist ticker, check:
       - Price below 9 EMA on 5-min chart
       - Supertrend RED
     → Fire alert with levels + earnings context
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
from indicators import ema, supertrend, atr

logger = logging.getLogger(__name__)


@dataclass
class EarningsMissCandidate:
    """A stock that qualified from the earnings miss nightly scan."""
    ticker: str
    company_name: str
    report_date: str  # YYYY-MM-DD
    report_time: str  # HH:MM:SS
    eps_surprise_percent: float
    revenue_surprise_percent: float
    close_price: float
    dollar_volume: float
    daily_atr: Optional[float]
    scan_date: str  # YYYY-MM-DD


@dataclass
class EarningsMissAlert:
    """Fully qualified alert for Setup 3."""
    ticker: str
    setup: str = "EARNINGS MISS SHORT"
    direction: str = "SHORT"
    # Key levels
    pm_high: float = 0.0
    pm_low: float = 0.0
    ema_9_current: float = 0.0
    daily_atr: Optional[float] = None
    # Earnings context
    company_name: str = ""
    report_date: str = ""
    report_time: str = ""
    eps_surprise_percent: float = 0.0
    revenue_surprise_percent: float = 0.0
    # State
    pm_price: float = 0.0
    supertrend_direction: str = ""  # "RED" or "GREEN"
    below_9ema: bool = False
    # Meta
    entry: str = ""
    exit_plan: str = ""
    confidence: str = ""
    scan_date: str = ""


class EarningsMissScanner:
    """Setup 3: Earnings Miss Short."""

    def __init__(self, config: ScannerConfig):
        self.config = config
        self.ec = config.earnings_miss
        self.client = PolygonClient(config.api)
        self.watchlist_path = Path(config.earnings_watchlist_file)
        self.fmp = None
        if getattr(config, "fmp", None) and config.fmp.enabled:
            try:
                from fmp_client import FmpClient
                self.fmp = FmpClient(config.fmp.api_key, config.fmp.timeout_seconds)
                logger.info("Earnings source: FMP (primary) + Benzinga (fallback)")
            except Exception as e:
                logger.error(f"Failed to initialize FMP client, falling back to Benzinga only: {e}")
        else:
            logger.info("Earnings source: Benzinga only (set FMP_API_KEY for FMP support)")

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 1: NIGHTLY SCAN
    # ═══════════════════════════════════════════════════════════════════

    def run_nightly_scan(self) -> list[EarningsMissCandidate]:
        """
        Find recent earnings *miss* names that meet liquidity thresholds.
        """
        logger.info("═══ NIGHTLY SCAN: Earnings Miss Short ═══")
        today = datetime.now().date()
        start_date = (today - timedelta(days=self.ec.report_date_lookback_days)).strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")

        # Prefer FMP (earlier posting) when available, fall back to Benzinga/Polygon
        if self.fmp is not None:
            earnings = self.fmp.get_recent_earnings_misses(
                lookback_days=self.ec.report_date_lookback_days,
                eps_surprise_threshold=self.ec.eps_surprise_percent_max,
                revenue_surprise_threshold=self.ec.revenue_surprise_percent_max,
            )
            logger.info(f"FMP returned {len(earnings)} earnings misses between {start_date} and {end_date}")
        else:
            params = {
                "date.gte": start_date,
                "date.lte": end_date,
                "eps_surprise_percent.lte": str(self.ec.eps_surprise_percent_max),
                "revenue_surprise_percent.lte": str(self.ec.revenue_surprise_percent_max),
                "limit": 5000,
            }
            earnings = self.client.get_earnings(params)
            logger.info(f"Benzinga/Polygon returned {len(earnings)} earnings records between {start_date} and {end_date}")

        candidates: list[EarningsMissCandidate] = []
        scan_date = today.strftime("%Y-%m-%d")

        for e in earnings:
            ticker = e.get("ticker")
            if not ticker:
                continue

            # Quick sanity check on surprise percentages (both must be <= threshold)
            eps_sp = float(e.get("eps_surprise_percent", 0.0) or 0.0)
            rev_sp = float(e.get("revenue_surprise_percent", 0.0) or 0.0)
            if eps_sp > self.ec.eps_surprise_percent_max or rev_sp > self.ec.revenue_surprise_percent_max:
                continue

            # Liquidity check via previous close (fast) + intraday volume from snapshot if needed
            prev = self.client.get_previous_close(ticker)
            if not prev:
                continue

            close = float(prev.get("c", 0.0) or 0.0)
            volume = float(prev.get("v", 0.0) or 0.0)
            dollar_volume = close * volume

            if close < self.ec.min_price or close > self.ec.max_price:
                continue
            if dollar_volume < self.ec.min_dollar_volume:
                continue

            # ATR for context
            daily_bars = self.client.get_daily_bars(ticker, days_back=20)
            if len(daily_bars) >= 15:
                highs = np.array([b["h"] for b in daily_bars], dtype=float)
                lows = np.array([b["l"] for b in daily_bars], dtype=float)
                closes = np.array([b["c"] for b in daily_bars], dtype=float)
                atr_vals = atr(highs, lows, closes, period=14)
                last_atr = float(atr_vals[-1]) if not np.isnan(atr_vals[-1]) else None
            else:
                last_atr = None

            candidate = EarningsMissCandidate(
                ticker=ticker,
                company_name=e.get("company_name", ""),
                report_date=e.get("date", ""),
                report_time=e.get("time", ""),
                eps_surprise_percent=round(eps_sp, 1),
                revenue_surprise_percent=round(rev_sp, 1),
                close_price=round(close, 2),
                dollar_volume=round(dollar_volume, 0),
                daily_atr=round(last_atr, 2) if last_atr else None,
                scan_date=scan_date,
            )
            candidates.append(candidate)
            logger.info(
                f"  ✓ {ticker}: EPS {eps_sp:.1f}%, Rev {rev_sp:.1f}% miss, "
                f"close ${close:.2f}, $vol {dollar_volume/1e6:.1f}M"
            )

        self._save_watchlist(candidates)
        logger.info(f"═══ NIGHTLY SCAN COMPLETE: {len(candidates)} earnings-miss names on watchlist ═══")
        return candidates

    # ═══════════════════════════════════════════════════════════════════
    # PHASE 2: MORNING PREMARKET CHECK
    # ═══════════════════════════════════════════════════════════════════

    def run_morning_check(self) -> list[EarningsMissAlert]:
        """Evaluate watchlist against premarket 9 EMA + Supertrend."""
        logger.info("═══ MORNING CHECK: Earnings Miss Short ═══")

        watchlist = self._load_watchlist()
        if not watchlist:
            logger.info("Earnings miss watchlist is empty.")
            return []

        alerts: list[EarningsMissAlert] = []
        for candidate in watchlist:
            ticker = candidate["ticker"]
            logger.info(f"  Checking {ticker}...")
            alert = self._evaluate_premarket(candidate)
            if alert:
                alerts.append(alert)
                logger.info(f"    🔴 ALERT: {ticker} — earnings miss short setup")
            else:
                logger.info(f"    ○ {ticker} — conditions not met")

        logger.info(f"═══ MORNING CHECK COMPLETE: {len(alerts)} alerts ═══")
        return alerts

    def _evaluate_premarket(self, candidate: dict) -> Optional[EarningsMissAlert]:
        """Core premarket evaluation for a single earnings-miss ticker."""
        ticker = candidate["ticker"]
        today = datetime.now().strftime("%Y-%m-%d")

        bars = self.client.get_intraday_bars(ticker, minutes=self.ec.ema_timeframe_minutes, date=today, extended_hours=True)
        if not bars or len(bars) < self.ec.ema_period + 1:
            return None

        closes = np.array([b["c"] for b in bars], dtype=float)
        highs = np.array([b["h"] for b in bars], dtype=float)
        lows = np.array([b["l"] for b in bars], dtype=float)

        current_price = float(closes[-1])
        pm_high = float(np.max(highs))
        pm_low = float(np.min(lows))

        # Supertrend direction
        if len(closes) >= self.ec.supertrend.period + 2:
            _, st_dir = supertrend(
                highs,
                lows,
                closes,
                period=self.ec.supertrend.period,
                multiplier=self.ec.supertrend.multiplier,
            )
            is_red = st_dir[-1] == -1
        else:
            is_red = True

        # 9 EMA filter
        ema_9 = ema(closes, self.ec.ema_period)
        ema_9_current = float(ema_9[-1]) if not np.isnan(ema_9[-1]) else current_price
        below_9ema = current_price < ema_9_current

        if not (is_red and below_9ema):
            return None

        daily_atr_val = candidate.get("daily_atr")

        # Entry/exit text
        entry = f"Short on break below PM low ${pm_low:.2f}"
        if daily_atr_val:
            target = round(current_price - daily_atr_val, 2)
            exit_plan = f"Cover by 10:00 AM ET. ATR target: ${target:.2f} (1 ATR below current)"
        else:
            exit_plan = "Cover by 10:00 AM ET."

        # Confidence scoring from surprise + trend confluence
        confidence_factors = []
        if candidate.get("eps_surprise_percent", 0) < -5:
            confidence_factors.append("big EPS miss")
        if candidate.get("revenue_surprise_percent", 0) < -5:
            confidence_factors.append("big revenue miss")
        if is_red and below_9ema:
            confidence_factors.append("trend aligned")

        confidence = "HIGH" if len(confidence_factors) >= 2 else "MEDIUM"

        return EarningsMissAlert(
            ticker=ticker,
            pm_high=pm_high,
            pm_low=pm_low,
            ema_9_current=round(ema_9_current, 2),
            daily_atr=daily_atr_val,
            company_name=candidate.get("company_name", ""),
            report_date=candidate.get("report_date", ""),
            report_time=candidate.get("report_time", ""),
            eps_surprise_percent=candidate.get("eps_surprise_percent", 0.0),
            revenue_surprise_percent=candidate.get("revenue_surprise_percent", 0.0),
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

    def _save_watchlist(self, candidates: list[EarningsMissCandidate]):
        data = [asdict(c) for c in candidates]
        self.watchlist_path.write_text(json.dumps(data, indent=2))
        logger.info(f"Earnings miss watchlist saved: {self.watchlist_path} ({len(data)} tickers)")

    def _load_watchlist(self) -> list[dict]:
        if not self.watchlist_path.exists():
            return []
        try:
            data = json.loads(self.watchlist_path.read_text())
            if data:
                scan_date = data[0].get("scan_date", "")
                today = datetime.now().strftime("%Y-%m-%d")
                yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                if scan_date not in (today, yesterday):
                    logger.warning(f"Earnings miss watchlist is stale (from {scan_date}). Run nightly scan again.")
                    return []
            return data
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Earnings miss watchlist file corrupt: {e}")
            return []

