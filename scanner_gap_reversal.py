"""
Setup 5: News Gap Up Reversal in Bearish Market
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

From your journal (Pages 38-39 AAPL tariff relief, 41-42 ARM):

  "Market in overall bearish regime... positive news causes big gap up...
   stock gaps into daily resistance... death candle at the open = trigger"

Two phases:
  1. MORNING SCAN: Find 5%+ gap-ups while SPY is bearish (below daily 9 EMA)
  2. INTRADAY CHECK: Watch for "death candle" (large red first 5-min bar)

If both conditions hit → short signal.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

from polygon_client import PolygonClient
from indicators import ema, supertrend, atr

logger = logging.getLogger(__name__)


@dataclass
class GapReversalAlert:
    """Alert from Setup 5."""

    ticker: str
    setup: str = "GAP UP REVERSAL (BEAR MARKET)"
    direction: str = "SHORT"
    stage: str = "SIGNAL"
    stage_number: int = 4
    message: str = ""
    gap_pct: float = 0.0
    current_price: float = 0.0
    spy_price: float = 0.0
    spy_ema_9: float = 0.0
    resistance_level: Optional[float] = None
    resistance_type: str = ""
    daily_atr: Optional[float] = None
    entry: str = ""
    exit_plan: str = (
        "Cover at prior support or PM low. This is a fade — take profits quickly."
    )
    confidence: str = ""


class GapReversalScanner:
    """
    Setup 5: Scan for gap-up reversals in bearish markets.

    Usage in intraday engine:
      1. Call check_market_regime() once per cycle to determine if SPY is bearish
      2. If bearish, call check_ticker() for each gap-up candidate
    """

    def __init__(self, client: PolygonClient):
        self.client = client
        self._alerted: set[str] = set()
        self._regime_bearish: Optional[bool] = None
        self._spy_price: float = 0.0
        self._spy_ema_9: float = 0.0

    def reset_session(self):
        self._alerted.clear()
        self._regime_bearish = None

    def check_market_regime(self) -> bool:
        """
        Determine if market is bearish: SPY below daily 9 EMA.
        Call once per poll cycle. Result cached for the cycle.
        """
        try:
            spy_bars = self.client.get_daily_bars("SPY", days_back=15)
            if not spy_bars or len(spy_bars) < 10:
                self._regime_bearish = False
                return False

            closes = np.array([b["c"] for b in spy_bars], dtype=float)
            ema_9 = ema(closes, 9)

            self._spy_price = float(closes[-1])
            self._spy_ema_9 = float(ema_9[-1]) if not np.isnan(ema_9[-1]) else 0

            self._regime_bearish = self._spy_price < self._spy_ema_9

            if self._regime_bearish:
                logger.debug(
                    f"  Market regime: BEARISH (SPY ${self._spy_price:.2f} < 9 EMA ${self._spy_ema_9:.2f})"
                )
            else:
                logger.debug(
                    f"  Market regime: BULLISH (SPY ${self._spy_price:.2f} > 9 EMA ${self._spy_ema_9:.2f})"
                )

            return self._regime_bearish

        except Exception as e:
            logger.error(f"Market regime check failed: {e}")
            self._regime_bearish = False
            return False

    def check_ticker(
        self,
        ticker: str,
        current_time: str,
        intraday_bars: list[dict],
        daily_atr: Optional[float] = None,
    ) -> Optional[GapReversalAlert]:
        """
        Check if a ticker qualifies for a gap-up reversal short.
        Only fires if market regime is bearish AND first candle is red.
        """
        if not self._regime_bearish:
            return None

        if ticker in self._alerted:
            return None

        # Need the first 5-min bar to be complete (after 9:35)
        if current_time < "09:35":
            return None

        if not intraday_bars or len(intraday_bars) < 1:
            return None

        # Get previous close for gap calculation
        prev = self.client.get_previous_close(ticker)
        if not prev:
            return None
        prev_close = prev.get("c", 0)
        if prev_close <= 0:
            return None

        current_price = intraday_bars[-1]["c"]
        open_price = intraday_bars[0]["o"]
        gap_pct = ((open_price - prev_close) / prev_close) * 100

        # Must be 5%+ gap UP
        if gap_pct < 5.0:
            return None

        # Check for "death candle" — first 5-min bar must be RED and large
        first_bar = intraday_bars[0]
        first_open = first_bar["o"]
        first_close = first_bar["c"]
        first_high = first_bar["h"]
        first_low = first_bar["l"]

        if first_close >= first_open:
            return None  # Not red

        # "Large" red bar = body is at least 50% of the bar's range
        bar_range = first_high - first_low
        body = first_open - first_close  # Positive since it's red
        if bar_range <= 0 or body / bar_range < 0.5:
            return None  # Not a convincing death candle

        # Check if gapping into daily resistance
        resistance_level, resistance_type = self._find_resistance(ticker, open_price)

        # All conditions met
        self._alerted.add(ticker)
        logger.info(
            f"  🔴 {ticker}: GAP UP REVERSAL — gap +{gap_pct:.1f}% into {resistance_type}, "
            f"death candle confirmed"
        )

        confidence = "HIGH" if resistance_level else "MEDIUM"
        if resistance_level:
            confidence += f" — gapped into {resistance_type} at ${resistance_level:.2f}"

        return GapReversalAlert(
            ticker=ticker,
            message=(
                f"Gap up +{gap_pct:.1f}% in bearish market. Death candle at open. SPY below 9 EMA."
            ),
            gap_pct=round(gap_pct, 1),
            current_price=round(current_price, 2),
            spy_price=round(self._spy_price, 2),
            spy_ema_9=round(self._spy_ema_9, 2),
            resistance_level=round(resistance_level, 2) if resistance_level else None,
            resistance_type=resistance_type,
            daily_atr=daily_atr,
            entry=(
                f"Short at ${current_price:.2f} (death candle confirmation). "
                f"Stop above first bar high ${first_high:.2f}."
            ),
            confidence=confidence,
        )

    def _find_resistance(self, ticker: str, gap_price: float) -> tuple[Optional[float], str]:
        """
        Check if the gap-up price is near daily resistance levels:
        supertrend, 200 EMA, or prior high.
        """
        try:
            bars = self.client.get_daily_bars(ticker, days_back=210)
            if not bars or len(bars) < 20:
                return None, ""

            closes = np.array([b["c"] for b in bars], dtype=float)
            highs = np.array([b["h"] for b in bars], dtype=float)
            lows = np.array([b["l"] for b in bars], dtype=float)

            # Check 200 EMA
            if len(closes) >= 200:
                ema_200 = ema(closes, 200)
                ema_200_val = float(ema_200[-1])
                if not np.isnan(ema_200_val):
                    # "Near" = within 2% of the level
                    if abs(gap_price - ema_200_val) / ema_200_val < 0.02:
                        return ema_200_val, "200 EMA"

            # Check supertrend
            if len(closes) >= 15:
                st_line, st_dir = supertrend(highs, lows, closes)
                st_val = float(st_line[-1])
                if not np.isnan(st_val) and st_dir[-1] == -1:  # Bearish supertrend
                    if abs(gap_price - st_val) / st_val < 0.02:
                        return st_val, "Supertrend"

            # Check prior high (20-day)
            prior_high = float(np.max(highs[-20:]))
            if abs(gap_price - prior_high) / prior_high < 0.02:
                return prior_high, "Prior 20d High"

            return None, ""

        except Exception:
            return None, ""

