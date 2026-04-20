"""
VIX Regime Monitor — Setups 1, 2 & 10
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Background monitor that checks VIX thresholds on each polling cycle.

Setup 1: VIX Spike Mean Reversion
  - VIX > 40 AND rose 30%+ in 14 days
  - "Buy UVXY puts, next month expiration, within 5 min of open"
  - 98% win rate over 25 years (your backtest)

Setup 2: VIX Crash Reversal
  - VIX dropped 30%+ in 3 sessions
  - "Expect SPY down day within 1-2 sessions"
  - 85-90% probability (your backtest)

Setup 10: Black Swan Reversal — "Load the Boat"
  - VIX > 35
  - SPY reversed 3%+ intraday from session low

These are rare but massive. The monitor runs passively and only
fires when thresholds hit — maybe once every few weeks.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

from polygon_client import PolygonClient
from config import IntradayConfig

logger = logging.getLogger(__name__)


@dataclass
class VixAlert:
    """Alert from VIX regime detection."""
    ticker: str = "VIX"
    setup: str = ""
    direction: str = ""
    message: str = ""
    vix_current: float = 0.0
    vix_pct_change: float = 0.0
    lookback_days: int = 0
    confidence: str = ""
    entry: str = ""
    exit_plan: str = ""
    stage: str = "SIGNAL"
    stage_number: int = 4  # Always a full signal (rare event)


class VixMonitor:
    """
    Passive VIX threshold monitor.
    Call `check()` on each polling cycle during market hours.
    """

    def __init__(self, client: PolygonClient, config: IntradayConfig):
        self.client = client
        self.config = config
        self._spike_alerted = False   # Only alert once per session
        self._crash_alerted = False
        self._blackswan_alerted = False

    def reset_session(self):
        """Reset alert flags at start of each trading day."""
        self._spike_alerted = False
        self._crash_alerted = False
        self._blackswan_alerted = False
        logger.info("VIX monitor: Session reset")

    def check(self) -> list[VixAlert]:
        """
        Check VIX levels. Returns 0-3 alerts.
        Called every polling cycle but only fires on threshold breach.
        """
        alerts: list[VixAlert] = []

        try:
            # Get current VIX
            vix_snap = self.client.get_snapshot("VIX:VIX")
            if not vix_snap:
                # Try alternative ticker
                vix_snap = self.client.get_snapshot("I:VIX")
            if not vix_snap:
                logger.debug("VIX snapshot unavailable")
                return alerts

            day_data = vix_snap.get("day", {})
            vix_current = day_data.get("c", day_data.get("l", 0))
            if not vix_current:
                return alerts

            # Get VIX daily bars for lookback
            vix_bars = self.client.get_daily_bars("VIX:VIX", days_back=20)
            if not vix_bars or len(vix_bars) < 4:
                # Try alternative
                vix_bars = self.client.get_daily_bars("I:VIX", days_back=20)
            if not vix_bars or len(vix_bars) < 4:
                logger.debug("VIX daily bars unavailable")
                return alerts

            closes = np.array([b["c"] for b in vix_bars], dtype=float)

            # ─── Setup 1: VIX Spike Mean Reversion ─────────────────
            if not self._spike_alerted:
                spike_alert = self._check_spike(vix_current, closes)
                if spike_alert:
                    alerts.append(spike_alert)
                    self._spike_alerted = True

            # ─── Setup 2: VIX Crash Reversal ───────────────────────
            if not self._crash_alerted:
                crash_alert = self._check_crash(vix_current, closes)
                if crash_alert:
                    alerts.append(crash_alert)
                    self._crash_alerted = True

            # ─── Setup 10: Black Swan Reversal ─────────────────────
            if not self._blackswan_alerted:
                bs_alert = self._check_blackswan(vix_current)
                if bs_alert:
                    alerts.append(bs_alert)
                    self._blackswan_alerted = True

        except Exception as e:
            logger.error(f"VIX monitor error: {e}")

        return alerts

    def _check_spike(self, vix_current: float, closes: np.ndarray) -> Optional[VixAlert]:
        """
        Setup 1: VIX > 40 AND rose 30%+ in 14 days.
        "98% of the time over 25 years, SPY rallies within 2 trading days."
        """
        if vix_current < self.config.vix_spike_threshold:
            return None

        # Check 14-day change
        lookback = min(self.config.vix_spike_lookback_days, len(closes) - 1)
        if lookback < 3:
            return None

        vix_14d_ago = closes[-(lookback + 1)]
        if vix_14d_ago <= 0:
            return None

        pct_change = ((vix_current - vix_14d_ago) / vix_14d_ago) * 100

        if pct_change >= self.config.vix_spike_pct:
            logger.info(f"  🟢 VIX SPIKE: {vix_current:.1f} (+{pct_change:.1f}% over {lookback}d)")
            return VixAlert(
                setup="VIX SPIKE MEAN REVERSION",
                direction="LONG (short vol)",
                message=(
                    f"VIX at {vix_current:.1f} — up {pct_change:.1f}% in {lookback} days. "
                    "98% chance SPY rallies within 2 trading days."
                ),
                vix_current=vix_current,
                vix_pct_change=round(pct_change, 1),
                lookback_days=lookback,
                confidence="VERY HIGH — 98% over 25yr backtest",
                entry="Buy UVXY puts (next month exp) within 5 min of open. Buy the ask.",
                exit_plan=(
                    "Hold for 2x, scale out. Set sell orders same day. "
                    "Rinse if VIX goes back up."
                ),
            )
        return None

    def _check_crash(self, vix_current: float, closes: np.ndarray) -> Optional[VixAlert]:
        """
        Setup 2: VIX dropped 30%+ in 3 sessions.
        "85-90% probability of a down day in SPY within 1-2 sessions."
        """
        lookback = min(self.config.vix_crash_lookback_days, len(closes) - 1)
        if lookback < 2:
            return None

        vix_peak = float(np.max(closes[-lookback - 1:]))
        if vix_peak <= 0:
            return None

        pct_change = ((vix_current - vix_peak) / vix_peak) * 100

        if pct_change <= self.config.vix_crash_pct:
            logger.info(
                f"  ⚠️ VIX CRASH: {vix_current:.1f} ({pct_change:.1f}% from peak {vix_peak:.1f})"
            )
            return VixAlert(
                setup="VIX CRASH REVERSAL",
                direction="SHORT SPY (short-term)",
                message=(
                    f"VIX crashed {pct_change:.1f}% from {vix_peak:.1f} to {vix_current:.1f} "
                    f"in {lookback} sessions. Expect SPY down day."
                ),
                vix_current=vix_current,
                vix_pct_change=round(pct_change, 1),
                lookback_days=lookback,
                confidence="HIGH — 85-90% over 25yr backtest",
                entry="Heads-up alert. Prepare short SPY position for next 1-2 sessions.",
                exit_plan="This is a preparedness alert, not an immediate trade.",
            )
        return None

    def _check_blackswan(self, vix_current: float) -> Optional[VixAlert]:
        """
        Setup 10: Black Swan Reversal — "Load the Boat"
        VIX > 35 AND SPY reversing 3%+ intraday (panic buying the dip).

        From your journal: "Buy QQQ calls (weekly or monthly, NOT 0DTE).
        Buy the ask. Avg in on pullback. 2nd bar pullback = entry.
        You actually still have 5-7 minutes to enter."
        """
        if vix_current < 35:
            return None

        try:
            spy_snap = self.client.get_snapshot("SPY")
            if not spy_snap:
                return None

            day = spy_snap.get("day", {})
            spy_open = day.get("o", 0)
            spy_close = day.get("c", 0)
            spy_low = day.get("l", 0)

            if spy_low <= 0 or spy_open <= 0:
                return None

            # Must be up 3%+ from intraday low (reversal happening)
            reversal_pct = ((spy_close - spy_low) / spy_low) * 100
            if reversal_pct < 3.0:
                return None

            from_open_pct = ((spy_close - spy_open) / spy_open) * 100

            logger.info(
                f"  🚀 BLACK SWAN REVERSAL: VIX {vix_current:.1f}, "
                f"SPY reversed {reversal_pct:.1f}% from low"
            )

            return VixAlert(
                setup="BLACK SWAN REVERSAL — LOAD THE BOAT",
                direction="LONG (buying the panic reversal)",
                message=(
                    f"VIX at {vix_current:.1f} (extreme fear) + SPY reversed {reversal_pct:.1f}% from intraday low. "
                    f"SPY {from_open_pct:+.1f}% from open."
                ),
                vix_current=vix_current,
                vix_pct_change=round(reversal_pct, 1),
                lookback_days=0,
                confidence="VERY HIGH — rare event, massive when it hits",
                entry=(
                    "Buy QQQ calls (weekly or monthly, NOT 0DTE). "
                    "Buy the ask. Avg in on 2nd bar pullback. "
                    "You have 5-7 min to enter even if you miss the first move."
                ),
                exit_plan="Ride the reversal. Scale out on strength. This is the big one.",
            )

        except Exception as e:
            logger.error(f"Black swan check failed: {e}")
            return None

"""
VIX Regime Monitor — Setups 1 & 2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Background monitor that checks VIX thresholds on each polling cycle.

Setup 1: VIX Spike Mean Reversion
  - VIX > 40 AND rose 30%+ in 14 days
  - "Buy UVXY puts, next month expiration, within 5 min of open"
  - 98% win rate over 25 years (your backtest)

Setup 2: VIX Crash Reversal
  - VIX dropped 30%+ in 3 sessions
  - "Expect SPY down day within 1-2 sessions"
  - 85-90% probability (your backtest)

These are rare but massive. The monitor runs passively and only
fires when thresholds hit — maybe once every 6 weeks.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np

from polygon_client import PolygonClient
from config import IntradayConfig

logger = logging.getLogger(__name__)


@dataclass
class VixAlert:
    """Alert from VIX regime detection."""
    ticker: str = "VIX"
    setup: str = ""
    direction: str = ""
    message: str = ""
    vix_current: float = 0.0
    vix_pct_change: float = 0.0
    lookback_days: int = 0
    confidence: str = ""
    entry: str = ""
    exit_plan: str = ""
    stage: str = "SIGNAL"
    stage_number: int = 4  # Always a full signal (rare event)


class VixMonitor:
    """
    Passive VIX threshold monitor.
    Call `check()` on each polling cycle during market hours.
    """

    def __init__(self, client: PolygonClient, config: IntradayConfig):
        self.client = client
        self.config = config
        self._spike_alerted = False   # Only alert once per session
        self._crash_alerted = False

    def reset_session(self):
        """Reset alert flags at start of each trading day."""
        self._spike_alerted = False
        self._crash_alerted = False
        logger.info("VIX monitor: Session reset")

    def check(self) -> list[VixAlert]:
        """
        Check VIX levels. Returns 0-2 alerts.
        Called every polling cycle but only fires on threshold breach.
        """
        alerts = []

        try:
            # Get current VIX
            vix_snap = self.client.get_snapshot("VIX:VIX")
            if not vix_snap:
                # Try alternative ticker
                vix_snap = self.client.get_snapshot("I:VIX")
            if not vix_snap:
                logger.debug("VIX snapshot unavailable")
                return alerts

            day_data = vix_snap.get("day", {})
            vix_current = day_data.get("c", day_data.get("l", 0))
            if not vix_current:
                return alerts

            # Get VIX daily bars for lookback
            vix_bars = self.client.get_daily_bars("VIX:VIX", days_back=20)
            if not vix_bars or len(vix_bars) < 4:
                # Try alternative
                vix_bars = self.client.get_daily_bars("I:VIX", days_back=20)
            if not vix_bars or len(vix_bars) < 4:
                logger.debug("VIX daily bars unavailable")
                return alerts

            closes = np.array([b["c"] for b in vix_bars], dtype=float)

            # ─── Setup 1: VIX Spike Mean Reversion ─────────────────
            if not self._spike_alerted:
                spike_alert = self._check_spike(vix_current, closes)
                if spike_alert:
                    alerts.append(spike_alert)
                    self._spike_alerted = True

            # ─── Setup 2: VIX Crash Reversal ───────────────────────
            if not self._crash_alerted:
                crash_alert = self._check_crash(vix_current, closes)
                if crash_alert:
                    alerts.append(crash_alert)
                    self._crash_alerted = True

        except Exception as e:
            logger.error(f"VIX monitor error: {e}")

        return alerts

    def _check_spike(self, vix_current: float, closes: np.ndarray) -> Optional[VixAlert]:
        """
        Setup 1: VIX > 40 AND rose 30%+ in 14 days.
        "98% of the time over 25 years, SPY rallies within 2 trading days."
        """
        if vix_current < self.config.vix_spike_threshold:
            return None

        # Check 14-day change
        lookback = min(self.config.vix_spike_lookback_days, len(closes) - 1)
        if lookback < 3:
            return None

        vix_14d_ago = closes[-(lookback + 1)]
        if vix_14d_ago <= 0:
            return None

        pct_change = ((vix_current - vix_14d_ago) / vix_14d_ago) * 100

        if pct_change >= self.config.vix_spike_pct:
            logger.info(f"  🟢 VIX SPIKE: {vix_current:.1f} (+{pct_change:.1f}% over {lookback}d)")
            return VixAlert(
                setup="VIX SPIKE MEAN REVERSION",
                direction="LONG (short vol)",
                message=(
                    f"VIX at {vix_current:.1f} — up {pct_change:.1f}% in {lookback} days. "
                    "98% chance SPY rallies within 2 days."
                ),
                vix_current=vix_current,
                vix_pct_change=round(pct_change, 1),
                lookback_days=lookback,
                confidence="VERY HIGH — 98% over 25yr backtest",
                entry="Buy UVXY puts (next month exp) within 5 min of open. Buy the ask.",
                exit_plan=(
                    "Hold for 2x, scale out. Set sell orders same day. "
                    "Rinse if VIX goes back up."
                ),
            )
        return None

    def _check_crash(self, vix_current: float, closes: np.ndarray) -> Optional[VixAlert]:
        """
        Setup 2: VIX dropped 30%+ in 3 sessions.
        "85-90% probability of a down day in SPY within 1-2 sessions."
        """
        lookback = min(self.config.vix_crash_lookback_days, len(closes) - 1)
        if lookback < 2:
            return None

        vix_peak = float(np.max(closes[-lookback - 1:]))
        if vix_peak <= 0:
            return None

        pct_change = ((vix_current - vix_peak) / vix_peak) * 100

        if pct_change <= self.config.vix_crash_pct:
            logger.info(f"  ⚠️ VIX CRASH: {vix_current:.1f} ({pct_change:.1f}% from peak {vix_peak:.1f})")
            return VixAlert(
                setup="VIX CRASH REVERSAL",
                direction="SHORT SPY (short-term)",
                message=(
                    f"VIX crashed {pct_change:.1f}% from {vix_peak:.1f} to {vix_current:.1f} "
                    f"in {lookback} sessions. Expect SPY down day."
                ),
                vix_current=vix_current,
                vix_pct_change=round(pct_change, 1),
                lookback_days=lookback,
                confidence="HIGH — 85-90% over 25yr backtest",
                entry="Heads-up alert. Prepare short SPY position for next 1-2 sessions.",
                exit_plan="This is a preparedness alert, not an immediate trade.",
            )
        return None

