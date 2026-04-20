"""
Technical Indicators
Pure-Python + numpy implementations. No TA-Lib dependency = easier setup in Cursor.
Every indicator here is used across multiple setups, so this is shared infrastructure.
"""
import numpy as np
from typing import Optional


def ema(closes: np.ndarray, period: int) -> np.ndarray:
    """
    Exponential Moving Average.
    Your anchor indicator — 9 EMA on the 5-min chart drives almost every setup.
    """
    if len(closes) < period:
        return np.full_like(closes, np.nan, dtype=float)

    alpha = 2.0 / (period + 1)
    result = np.empty_like(closes, dtype=float)
    result[:period - 1] = np.nan
    # Seed with SMA of first `period` values
    result[period - 1] = np.mean(closes[:period])
    for i in range(period, len(closes)):
        result[i] = alpha * closes[i] + (1 - alpha) * result[i - 1]
    return result


def atr(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int = 14,
) -> np.ndarray:
    """
    Average True Range.
    Rule 3 from your guardrails: "Know how far it's likely to move."
    Included in every alert for position sizing + target setting.
    """
    if len(highs) < 2:
        return np.full_like(highs, np.nan, dtype=float)
    if len(highs) < period:
        return np.full(len(highs), np.nan, dtype=float)

    tr = np.empty(len(highs), dtype=float)
    tr[0] = highs[0] - lows[0]
    for i in range(1, len(highs)):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    # Wilder's smoothing (same as most charting platforms)
    atr_vals = np.empty_like(tr)
    atr_vals[:period - 1] = np.nan
    atr_vals[period - 1] = np.mean(tr[:period])
    for i in range(period, len(tr)):
        atr_vals[i] = (atr_vals[i - 1] * (period - 1) + tr[i]) / period
    return atr_vals


def supertrend(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int = 10,
    multiplier: float = 3.0,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Supertrend Indicator.
    Returns (supertrend_line, direction) where:
      direction[i] = 1  → GREEN (bullish)
      direction[i] = -1 → RED (bearish)

    Used in Setups 3, 6, 7, 8 for trend confirmation.
    Setup 6 specifically needs: "Red supertrend all PM"
    """
    n = len(closes)
    atr_vals = atr(highs, lows, closes, period)

    upper_band = np.empty(n, dtype=float)
    lower_band = np.empty(n, dtype=float)
    st_line = np.empty(n, dtype=float)
    direction = np.empty(n, dtype=float)

    hl2 = (highs + lows) / 2.0

    for i in range(n):
        upper_band[i] = hl2[i] + multiplier * atr_vals[i]
        lower_band[i] = hl2[i] - multiplier * atr_vals[i]

    # Initialize
    direction[0] = 1
    st_line[0] = lower_band[0]

    for i in range(1, n):
        if np.isnan(atr_vals[i]):
            direction[i] = direction[i - 1]
            st_line[i] = st_line[i - 1]
            continue

        # Band clamping (standard supertrend logic)
        if lower_band[i] > 0 and lower_band[i - 1] > 0:
            lower_band[i] = max(lower_band[i], lower_band[i - 1]) if closes[i - 1] > lower_band[i - 1] else lower_band[i]
        if upper_band[i] > 0 and upper_band[i - 1] > 0:
            upper_band[i] = min(upper_band[i], upper_band[i - 1]) if closes[i - 1] < upper_band[i - 1] else upper_band[i]

        # Direction flip
        if direction[i - 1] == 1:  # Was bullish
            if closes[i] < lower_band[i]:
                direction[i] = -1
                st_line[i] = upper_band[i]
            else:
                direction[i] = 1
                st_line[i] = lower_band[i]
        else:  # Was bearish
            if closes[i] > upper_band[i]:
                direction[i] = 1
                st_line[i] = lower_band[i]
            else:
                direction[i] = -1
                st_line[i] = upper_band[i]

    return st_line, direction


def ema_slope(ema_values: np.ndarray, lookback: int = 3) -> np.ndarray:
    """
    Slope of the EMA over `lookback` bars.
    Negative slope = downtrend bias (used in Setup 8, useful as confirmer here).
    """
    slope = np.full_like(ema_values, np.nan, dtype=float)
    for i in range(lookback, len(ema_values)):
        if not np.isnan(ema_values[i]) and not np.isnan(ema_values[i - lookback]):
            slope[i] = ema_values[i] - ema_values[i - lookback]
    return slope


def relative_volume(volumes: np.ndarray, lookback: int = 20) -> Optional[float]:
    """
    Current volume relative to N-day average.
    Rule 4: Volume confirms exhaustion. 5x+ is the runner threshold.
    """
    if len(volumes) < lookback + 1:
        return None
    avg_vol = np.mean(volumes[-lookback - 1:-1])
    if avg_vol == 0:
        return None
    return float(volumes[-1] / avg_vol)


# ─── PHASE 2: Intraday Indicators ─────────────────────────────────────

def vwap(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    volumes: np.ndarray,
) -> np.ndarray:
    """
    Volume Weighted Average Price (intraday, resets each session).
    Setup 8: 9 EMA must be below VWAP for short bias confirmation.
    """
    typical_price = (highs + lows + closes) / 3.0
    cum_tp_vol = np.cumsum(typical_price * volumes)
    cum_vol = np.cumsum(volumes)
    # Avoid division by zero
    vwap_vals = np.where(cum_vol > 0, cum_tp_vol / cum_vol, np.nan)
    return vwap_vals


def opening_range(
    highs: np.ndarray,
    lows: np.ndarray,
    bar_count: int = 3,
) -> tuple[float, float]:
    """
    Opening Range High/Low from first N bars.
    Default: 3 bars of 5-min = 09:30-09:45 ET.
    Setup 8 step 1: "Mark ORH/ORL from 09:30-09:45"

    Returns (orh, orl).
    """
    if len(highs) < bar_count:
        return float(np.max(highs)), float(np.min(lows))
    orh = float(np.max(highs[:bar_count]))
    orl = float(np.min(lows[:bar_count]))
    return orh, orl


def is_bearish_candle(
    open_price: float,
    high: float,
    low: float,
    close: float,
) -> bool:
    """
    Bearish candle: red close with upper wick.
    Setup 8 step 4: "pullback to 9 EMA → bearish candle (upper wick + red close)"
    """
    if close >= open_price:
        return False  # Not red
    body = abs(close - open_price)
    upper_wick = high - max(open_price, close)
    # Upper wick should be meaningful relative to body
    return upper_wick > body * 0.3 if body > 0 else upper_wick > 0

