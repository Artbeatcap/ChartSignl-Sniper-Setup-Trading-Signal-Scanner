"""
Setup 8: ORL + VWAP Reject + 9 EMA — Intraday State Machine
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Stage progression (per ticker):
  Stage 0: WAITING        — Collecting opening range (09:30-09:45)
  Stage 1: ORL_BREAK      — Price closed below ORL
  Stage 2: TREND_CONFIRMED — 9 EMA < VWAP AND 9 EMA sloping down
  Stage 3: SETUP_ACTIVE   — Watching for VWAP reject or EMA tap
  Stage 4: ENTRY_SIGNAL   — Rejection confirmed → FIRE ALERT

  INVALIDATED             — Price reclaimed both 9 EMA AND VWAP → dead

Each ticker is tracked independently. State resets each session.
"""
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np

from config import RangeConfig
from indicators import ema, ema_slope, vwap, opening_range, is_bearish_candle
from range_floors import compute_levels, log_floor_decision

logger = logging.getLogger(__name__)


class Stage(Enum):
    WAITING = 0           # Collecting opening range
    ORL_BREAK = 1         # Price closed below ORL
    TREND_CONFIRMED = 2   # 9 EMA < VWAP, slope down
    SETUP_ACTIVE = 3      # Watching for rejection entry
    ENTRY_SIGNAL = 4      # 🔴 FIRE ALERT
    INVALIDATED = -1      # Price reclaimed EMA + VWAP


@dataclass
class TickerState:
    """Tracks a single ticker's progression through Setup 8 stages."""
    ticker: str
    stage: Stage = Stage.WAITING
    orh: float = 0.0
    orl: float = 0.0
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_1r: Optional[float] = None
    daily_atr: Optional[float] = None
    last_alert_stage: Optional[Stage] = None  # Prevent duplicate alerts per stage
    stop_was_widened: bool = False
    floor_source: str = ""

    # Bar data accumulated during the session
    opens: list = field(default_factory=list)
    highs: list = field(default_factory=list)
    lows: list = field(default_factory=list)
    closes: list = field(default_factory=list)
    volumes: list = field(default_factory=list)


@dataclass
class IntradayAlert:
    """Alert output from Setup 8."""
    ticker: str
    setup: str = "ORL + VWAP + 9EMA"
    direction: str = "SHORT"
    stage: str = ""
    stage_number: int = 0
    message: str = ""
    # Levels (populated progressively)
    orl: float = 0.0
    orh: float = 0.0
    current_price: float = 0.0
    ema_9: float = 0.0
    vwap_level: float = 0.0
    ema_slope_val: float = 0.0
    daily_atr: Optional[float] = None
    # Entry details (only on Stage 4)
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_1r: Optional[float] = None
    exit_plan: str = ""
    confidence: str = ""
    stop_was_widened: bool = False
    floor_source: str = ""


class OrlVwapScanner:
    """
    Setup 8 state machine. Call `process_bar()` for each new 5-min bar.
    Returns an alert if the ticker advanced to a new stage.
    """

    def __init__(
        self,
        opening_range_bars: int = 3,
        ema_period: int = 9,
        slope_lookback: int = 3,
        range_config: Optional[RangeConfig] = None,
    ):
        self.opening_range_bars = opening_range_bars
        self.ema_period = ema_period
        self.slope_lookback = slope_lookback
        self.range_config = range_config or RangeConfig()
        self.states: dict[str, TickerState] = {}

    def reset_session(self):
        """Call at start of each trading day."""
        self.states.clear()
        logger.info("Setup 8: Session state reset")

    def process_bar(
        self,
        ticker: str,
        open_price: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        daily_atr_value: Optional[float] = None,
    ) -> Optional[IntradayAlert]:
        """
        Feed a new 5-min bar. Returns an alert if stage advanced.
        Call this every 5 minutes for each ticker.
        """
        # Get or create state
        if ticker not in self.states:
            self.states[ticker] = TickerState(ticker=ticker)
        state = self.states[ticker]

        if daily_atr_value:
            state.daily_atr = daily_atr_value

        # Append bar data
        state.opens.append(open_price)
        state.highs.append(high)
        state.lows.append(low)
        state.closes.append(close)
        state.volumes.append(volume)

        bar_count = len(state.closes)

        # Skip if invalidated
        if state.stage == Stage.INVALIDATED:
            return None
        # Skip if already fired entry signal
        if state.stage == Stage.ENTRY_SIGNAL:
            return None

        # Convert to numpy for indicator calculations
        closes_arr = np.array(state.closes, dtype=float)
        highs_arr = np.array(state.highs, dtype=float)
        lows_arr = np.array(state.lows, dtype=float)
        volumes_arr = np.array(state.volumes, dtype=float)
        opens_arr = np.array(state.opens, dtype=float)

        # ─── STAGE 0: Collect opening range ────────────────────────
        if state.stage == Stage.WAITING:
            if bar_count >= self.opening_range_bars:
                state.orh, state.orl = opening_range(highs_arr, lows_arr, self.opening_range_bars)
                logger.debug(f"  {ticker}: Opening range set — ORH ${state.orh:.2f}, ORL ${state.orl:.2f}")

                # Check if already below ORL on the bar that completes the range
                if close < state.orl:
                    state.stage = Stage.ORL_BREAK
                    return self._build_alert(state, closes_arr, highs_arr, lows_arr, volumes_arr)
                else:
                    # Stay at WAITING but with OR defined — will check on next bar
                    state.stage = Stage.WAITING  # Still waiting for break
            return None

        # From here, OR is defined. Check for ORL break on every bar.

        # ─── INVALIDATION CHECK (any stage) ─────────────────────────
        # If price reclaims both 9 EMA and VWAP → setup dead
        if bar_count >= self.ema_period:
            ema_9 = ema(closes_arr, self.ema_period)
            vwap_vals = vwap(highs_arr, lows_arr, closes_arr, volumes_arr)
            ema_val = ema_9[-1]
            vwap_val = vwap_vals[-1]

            if not np.isnan(ema_val) and not np.isnan(vwap_val):
                if close > ema_val and close > vwap_val and state.stage.value >= 1:
                    logger.debug(
                        f"  {ticker}: INVALIDATED — price ${close:.2f} above both EMA ${ema_val:.2f} and VWAP ${vwap_val:.2f}"
                    )
                    state.stage = Stage.INVALIDATED
                    return None

        # ─── STAGE 0→1: ORL Break ──────────────────────────────────
        if state.stage == Stage.WAITING and state.orl > 0:
            if close < state.orl:
                state.stage = Stage.ORL_BREAK
                return self._build_alert(state, closes_arr, highs_arr, lows_arr, volumes_arr)
            return None

        # ─── STAGE 1→2: Trend Confirmed ────────────────────────────
        if state.stage == Stage.ORL_BREAK:
            if bar_count >= self.ema_period:
                ema_9 = ema(closes_arr, self.ema_period)
                vwap_vals = vwap(highs_arr, lows_arr, closes_arr, volumes_arr)
                slope_vals = ema_slope(ema_9, self.slope_lookback)

                ema_val = ema_9[-1]
                vwap_val = vwap_vals[-1]
                slope_val = slope_vals[-1] if not np.isnan(slope_vals[-1]) else 0

                if ema_val < vwap_val and slope_val < 0:
                    state.stage = Stage.TREND_CONFIRMED
                    return self._build_alert(state, closes_arr, highs_arr, lows_arr, volumes_arr)
            return None

        # ─── STAGE 2→3: Setup Active (watching for rejection) ──────
        if state.stage == Stage.TREND_CONFIRMED:
            # Auto-advance to SETUP_ACTIVE — we're now watching
            state.stage = Stage.SETUP_ACTIVE
            # Don't alert here — it's the same bar conceptually
            # Fall through to check for immediate rejection

        # ─── STAGE 3→4: Entry Signal ───────────────────────────────
        if state.stage == Stage.SETUP_ACTIVE:
            ema_9 = ema(closes_arr, self.ema_period)
            vwap_vals = vwap(highs_arr, lows_arr, closes_arr, volumes_arr)

            ema_val = ema_9[-1]
            vwap_val = vwap_vals[-1]

            if np.isnan(ema_val) or np.isnan(vwap_val):
                return None

            # Check for VWAP rejection: price touched VWAP and closed back below
            vwap_reject = high >= vwap_val and close < vwap_val

            # Check for EMA tap: price touched 9 EMA + bearish candle
            ema_tap = high >= ema_val and is_bearish_candle(open_price, high, low, close)

            if vwap_reject or ema_tap:
                trigger = "VWAP reject" if vwap_reject else "9 EMA tap + bearish candle"

                entry_price = close
                recent_swing_high = float(np.max(highs_arr[-5:])) if len(highs_arr) >= 5 else high
                raw_stop = max(recent_swing_high, ema_val) + 0.02
                risk = raw_stop - entry_price
                raw_target = entry_price - risk
                lv = compute_levels(
                    entry_price=entry_price,
                    structural_stop_price=raw_stop,
                    structural_target_price=raw_target,
                    price=float(close),
                    daily_atr=state.daily_atr,
                    ticker=ticker,
                    direction="short",
                    config=self.range_config,
                )
                log_floor_decision(lv["log_entry"])
                if lv["skip"]:
                    logger.info(f"  Suppressed {ticker}: {lv['skip_reason']}")
                    return None

                state.stage = Stage.ENTRY_SIGNAL
                state.entry_price = entry_price
                state.stop_price = lv["stop"]
                state.target_1r = lv["target"]
                state.stop_was_widened = lv["stop_was_widened"]
                state.floor_source = lv["floor_source"]

                logger.info(f"  🔴 {ticker}: ENTRY SIGNAL — {trigger} at ${close:.2f}")
                return self._build_alert(state, closes_arr, highs_arr, lows_arr, volumes_arr, trigger=trigger)

        return None

    def _build_alert(
        self,
        state: TickerState,
        closes: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        volumes: np.ndarray,
        trigger: str = "",
    ) -> Optional[IntradayAlert]:
        """Build an alert if the stage advanced past the last alert."""
        if state.last_alert_stage == state.stage:
            return None  # Already alerted for this stage
        state.last_alert_stage = state.stage

        # Compute current indicator values
        ema_9 = ema(closes, self.ema_period)
        vwap_vals = vwap(highs, lows, closes, volumes)
        slope_vals = ema_slope(ema_9, self.slope_lookback)

        ema_val = float(ema_9[-1]) if not np.isnan(ema_9[-1]) else 0
        vwap_val = float(vwap_vals[-1]) if not np.isnan(vwap_vals[-1]) else 0
        slope_val = float(slope_vals[-1]) if len(slope_vals) > 0 and not np.isnan(slope_vals[-1]) else 0

        # Stage-specific message
        messages = {
            Stage.ORL_BREAK: f"ORL break confirmed (${state.orl:.2f}). Watching for trend confirmation.",
            Stage.TREND_CONFIRMED: "9 EMA < VWAP, slope down. Setup active — watching for rejection.",
            Stage.ENTRY_SIGNAL: f"ENTRY SIGNAL — {trigger} at ${closes[-1]:.2f}",
        }

        alert = IntradayAlert(
            ticker=state.ticker,
            stage=state.stage.name,
            stage_number=state.stage.value,
            message=messages.get(state.stage, state.stage.name),
            orl=state.orl,
            orh=state.orh,
            current_price=float(closes[-1]),
            ema_9=round(ema_val, 2),
            vwap_level=round(vwap_val, 2),
            ema_slope_val=round(slope_val, 4),
            daily_atr=state.daily_atr,
        )

        # Only populate entry details on ENTRY_SIGNAL
        if state.stage == Stage.ENTRY_SIGNAL:
            alert.entry_price = state.entry_price
            alert.stop_price = state.stop_price
            alert.target_1r = state.target_1r
            alert.stop_was_widened = state.stop_was_widened
            alert.floor_source = state.floor_source
            alert.exit_plan = (
                "T1=1R → move stop to BE. Trail above 9 EMA. "
                "Exit if price reclaims EMA + VWAP."
            )
            alert.confidence = "HIGH — full checklist confirmed"

        return alert

