"""
Setup 9: Dead Cat Bounce (Catalyst Bounce)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

From your journal (Pages 16-17, 32):
  "High risk, medium reward"
  "Most times new low sells have a bounce on first day
   then gap down lower AHs PM on low volume."

THIS IS A DAY TRADE ONLY. NOT A SWING.

Trigger:
  - Stock gaps down 20%+
  - Current price below 52-week low
  - Price crosses above 9 EMA between 9:35-9:45 AM
  - Target: first major resistance level

Runs inside the intraday engine. Checks all tickers on each
poll cycle during the 9:35-9:45 window.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

from polygon_client import PolygonClient
from indicators import ema

logger = logging.getLogger(__name__)


@dataclass
class DeadCatAlert:
    """Alert from Setup 9."""

    ticker: str
    setup: str = "DEAD CAT BOUNCE"
    direction: str = "LONG (day trade only)"
    stage: str = "SIGNAL"
    stage_number: int = 4
    message: str = ""
    gap_pct: float = 0.0
    current_price: float = 0.0
    ema_9: float = 0.0
    week_52_low: float = 0.0
    prev_close: float = 0.0
    daily_atr: Optional[float] = None
    entry: str = ""
    exit_plan: str = (
        "Sell at nearest major resistance. DAY TRADE ONLY — most bounce Day 1 then gap lower Day 2."
    )
    confidence: str = "MEDIUM — high risk, medium reward"


class DeadCatScanner:
    """
    Setup 9: Scan for dead cat bounce setups during the 9:35-9:45 window.
    Call check_ticker() for each ticker on each poll cycle.
    """

    def __init__(self, client: PolygonClient, ema_period: int = 9):
        self.client = client
        self.ema_period = ema_period
        self._alerted: set[str] = set()  # One alert per ticker per session

    def reset_session(self):
        self._alerted.clear()

    def check_ticker(
        self,
        ticker: str,
        current_time: str,
        intraday_bars: list[dict],
        daily_atr: Optional[float] = None,
    ) -> Optional[DeadCatAlert]:
        """
        Check if a ticker qualifies for a dead cat bounce.
        Only fires during the 9:35-9:45 AM window.
        """
        # Time gate: only 9:35-9:45 AM
        if current_time < "09:35" or current_time > "09:45":
            return None

        if ticker in self._alerted:
            return None

        if not intraday_bars or len(intraday_bars) < 2:
            return None

        # Get previous close for gap calculation
        prev = self.client.get_previous_close(ticker)
        if not prev:
            return None
        prev_close = prev.get("c", 0)
        if prev_close <= 0:
            return None

        current_price = intraday_bars[-1]["c"]
        gap_pct = ((current_price - prev_close) / prev_close) * 100

        # Must be a 20%+ gap DOWN
        if gap_pct > -20:
            return None

        # Check if below 52-week low
        week_52_low = self._get_52_week_low(ticker)
        if week_52_low is None:
            return None
        if current_price > week_52_low:
            return None

        # Check if price crossed above 9 EMA
        closes = np.array([b["c"] for b in intraday_bars], dtype=float)
        if len(closes) < self.ema_period:
            return None

        ema_9 = ema(closes, self.ema_period)
        ema_val = ema_9[-1]
        ema_prev = ema_9[-2] if len(ema_9) >= 2 else ema_val

        if np.isnan(ema_val) or np.isnan(ema_prev):
            return None

        # Cross above: previous bar was below EMA, current bar is above
        crossed_above = closes[-2] < ema_prev and closes[-1] > ema_val

        if not crossed_above:
            return None

        # All conditions met
        self._alerted.add(ticker)
        logger.info(
            f"  🟡 {ticker}: DEAD CAT BOUNCE — gap {gap_pct:.1f}%, crossed above 9 EMA at ${current_price:.2f}"
        )

        return DeadCatAlert(
            ticker=ticker,
            message=(
                f"Gap down {gap_pct:.1f}%, below 52wk low, crossed above 9 EMA at ${current_price:.2f}"
            ),
            gap_pct=round(gap_pct, 1),
            current_price=round(current_price, 2),
            ema_9=round(ema_val, 2),
            week_52_low=round(week_52_low, 2),
            prev_close=round(prev_close, 2),
            daily_atr=daily_atr,
            entry=f"Long at ${current_price:.2f} (9 EMA cross). Target: first resistance level.",
        )

    def _get_52_week_low(self, ticker: str) -> Optional[float]:
        """Fetch 52-week low from daily bars."""
        try:
            bars = self.client.get_daily_bars(ticker, days_back=260)
            if not bars or len(bars) < 50:
                return None
            lows = [b["l"] for b in bars]
            return min(lows)
        except Exception:
            return None

