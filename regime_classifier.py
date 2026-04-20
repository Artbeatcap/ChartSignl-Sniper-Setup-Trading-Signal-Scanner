"""
regime_classifier.py
--------------------
Setup Sniper — Market Regime Classifier.

Classifies the overall market regime ONCE pre-open each day and sticks with
that decision through the session. Feeds into the ConvictionScorer's regime
alignment penalty — Rule 1 from the playbook ("no countertrend when trend
isn't finished") becomes automatic.

Six regimes (matching conviction_score.MarketRegime):
  PANIC       — VIX > 30, chaos
  STRONG_BEAR — SPY < daily 9EMA AND supertrend red AND VIX > 20
  BEAR        — SPY < daily 9EMA
  NEUTRAL     — Mixed signals, at 9EMA, or low confidence
  BULL        — SPY > daily 9EMA
  STRONG_BULL — SPY > daily 9EMA AND supertrend green AND VIX < 15

Design notes:
  - Decision tree, not scoring — interpretable "why" for every classification
  - Confidence reported separately (0–1 signal agreement)
  - Sticky: classify pre-open, don't flip on intraday noise
  - Transition detection: flags when regime changes day-over-day
  - PANIC is an override (VIX > 30 overrides everything)

This module is PURE LOGIC. No network, no clock dependency.

Usage:
    classifier = RegimeClassifier()
    inputs = RegimeInputs(spy_close=..., vix_current=..., ...)
    decision = classifier.classify(inputs, timestamp=now)
    # decision.regime goes into TickerFeatures.market_regime
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from conviction_score import Direction, MarketRegime


# ============================================================================
# CONFIG
# ============================================================================

@dataclass
class RegimeConfig:
    """Tunable thresholds for regime classification."""

    # Absolute overrides
    panic_vix_threshold: float = 30.0          # VIX above this → always PANIC

    # Strong regime conditions
    strong_bull_vix_ceiling: float = 15.0      # VIX must be below this for STRONG_BULL
    strong_bear_vix_floor: float = 20.0        # VIX must be above this for STRONG_BEAR

    # Minimum confidence to commit to a strong regime (else downgrade)
    strong_regime_min_confidence: float = 0.80

    # Intraday change thresholds — only flip regime if these are exceeded
    intraday_spy_change_threshold: float = 0.03   # 3% SPY move intraday
    intraday_vix_change_threshold: float = 0.30   # 30% VIX spike intraday


# ============================================================================
# INPUTS & OUTPUTS
# ============================================================================

@dataclass
class RegimeInputs:
    """
    Daily market snapshot, taken pre-open. Uses yesterday's close for SPY
    since today's close doesn't exist yet. VIX can be live pre-market.
    """
    # Core price signals (yesterday's close + EMAs through yesterday)
    spy_close: float
    spy_ema_9_daily: float
    spy_ema_65_daily: float
    spy_ema_200_daily: float
    spy_supertrend_direction: Direction  # LONG=green, SHORT=red, on daily

    # Trend strength
    spy_20d_pct_change: float = 0.0     # positive = trending up

    # VIX
    vix_current: float = 0.0
    vix_pct_change_14d: float = 0.0     # for spike detection
    vix_pct_change_3d: float = 0.0      # for crash detection

    # Optional breadth (richer regime signal when available)
    pct_stocks_above_50dma: Optional[float] = None

    # Intraday updates (only used by update_intraday_if_needed)
    spy_intraday_pct_change: float = 0.0


@dataclass
class RegimeDecision:
    """Full classification result with reasoning."""
    regime: MarketRegime
    confidence: float                   # 0-1, how strongly signals agree
    timestamp: datetime

    # Interpretability
    reasons: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)

    # Transition tracking
    previous_regime: Optional[MarketRegime] = None
    is_transition: bool = False

    @property
    def confidence_grade(self) -> str:
        if self.confidence >= 0.85:
            return "HIGH"
        if self.confidence >= 0.65:
            return "MEDIUM"
        return "LOW"

    @property
    def bias(self) -> str:
        """Short-hand bias for alert suppression logic."""
        if self.regime in {MarketRegime.STRONG_BULL, MarketRegime.BULL}:
            return "bullish"
        if self.regime in {MarketRegime.STRONG_BEAR, MarketRegime.BEAR, MarketRegime.PANIC}:
            return "bearish"
        return "neutral"

    def format_morning_brief(self) -> str:
        """Pre-open regime brief for journal / dashboard / Slack."""
        transition_note = ""
        if self.is_transition and self.previous_regime:
            transition_note = (
                f"\n⚠️  REGIME CHANGE: {self.previous_regime.value.upper()} "
                f"→ {self.regime.value.upper()} overnight"
            )

        lines = [
            f"🌅 MORNING REGIME BRIEF — {self.timestamp.strftime('%a %b %d %Y')}",
            f"   Regime:     {self.regime.value.upper()}  ({self.confidence_grade} confidence, {self.confidence:.0%})",
            f"   Bias:       {self.bias.upper()}",
            f"   Reasons:",
        ]
        for r in self.reasons:
            lines.append(f"      · {r}")

        lines.append(f"   Signals:")
        for k, v in self.signals.items():
            if isinstance(v, float):
                if "pct" in k or "change" in k:
                    lines.append(f"      · {k}: {v:+.1%}")
                else:
                    lines.append(f"      · {k}: {v:.2f}")
            else:
                lines.append(f"      · {k}: {v}")

        if transition_note:
            lines.append(transition_note)

        # Setup implications — direct playbook mapping
        lines.append("\n   📋 Setup Implications:")
        if self.regime == MarketRegime.PANIC:
            lines.append("      → Setups 1 (VIX Spike) and 10 (Black Swan) on HIGH ALERT")
            lines.append("      → Countertrend longs need 2x confluence")
        elif self.regime in {MarketRegime.STRONG_BEAR, MarketRegime.BEAR}:
            lines.append("      → Favor Setups 3, 5, 6, 7, 8 (shorts)")
            lines.append("      → SUPPRESS long-bias alerts except Setup 9 (Dead Cat)")
        elif self.regime == MarketRegime.NEUTRAL:
            lines.append("      → No regime-aligned setups; trade selectively")
            lines.append("      → Mostly mean-reversion; avoid trend-based entries")
        elif self.regime == MarketRegime.BULL:
            lines.append("      → Setup 7 (Exhaustion) plays need strong confluence")
            lines.append("      → Earnings fades (3, 4) still valid — ticker-specific")
        elif self.regime == MarketRegime.STRONG_BULL:
            lines.append("      → SUPPRESS Setup 5 (Gap Up Reversal) — needs bear regime")
            lines.append("      → Setup 2 (VIX Crash) heads-up — watch for complacency top")

        return "\n".join(lines)


# ============================================================================
# CLASSIFIER
# ============================================================================

class RegimeClassifier:
    """
    Stateful classifier — remembers yesterday's regime for transition detection.
    Call classify() once per session, pre-open, then check intraday_update.
    """

    def __init__(self, config: Optional[RegimeConfig] = None):
        self.config = config or RegimeConfig()
        self._last_decision: Optional[RegimeDecision] = None

    # ------------------------------------------------------------------
    # Core classification
    # ------------------------------------------------------------------

    def classify(
        self,
        inputs: RegimeInputs,
        timestamp: datetime,
    ) -> RegimeDecision:
        """Run the decision tree, compute confidence, detect transition."""
        signals = self._collect_signals(inputs)

        # Priority 1: PANIC override
        if inputs.vix_current > self.config.panic_vix_threshold:
            decision = RegimeDecision(
                regime=MarketRegime.PANIC,
                confidence=1.0,
                timestamp=timestamp,
                reasons=[f"VIX {inputs.vix_current:.1f} > {self.config.panic_vix_threshold} (PANIC override)"],
                signals=signals,
            )
            return self._finalize(decision)

        above_9 = inputs.spy_close > inputs.spy_ema_9_daily
        below_9 = inputs.spy_close < inputs.spy_ema_9_daily
        st_green = inputs.spy_supertrend_direction == Direction.LONG
        st_red = inputs.spy_supertrend_direction == Direction.SHORT

        # Priority 2: STRONG regimes — all primary signals must align
        if above_9 and st_green and inputs.vix_current < self.config.strong_bull_vix_ceiling:
            confidence, reasons = self._score_bullish_confidence(inputs, signals)
            if confidence >= self.config.strong_regime_min_confidence:
                decision = RegimeDecision(
                    regime=MarketRegime.STRONG_BULL,
                    confidence=confidence,
                    timestamp=timestamp,
                    reasons=reasons,
                    signals=signals,
                )
                return self._finalize(decision)
            # Fall through to BULL if confidence too low

        if below_9 and st_red and inputs.vix_current > self.config.strong_bear_vix_floor:
            confidence, reasons = self._score_bearish_confidence(inputs, signals)
            if confidence >= self.config.strong_regime_min_confidence:
                decision = RegimeDecision(
                    regime=MarketRegime.STRONG_BEAR,
                    confidence=confidence,
                    timestamp=timestamp,
                    reasons=reasons,
                    signals=signals,
                )
                return self._finalize(decision)
            # Fall through to BEAR

        # Priority 3: Base BULL / BEAR
        if above_9:
            confidence, reasons = self._score_bullish_confidence(inputs, signals)
            return self._finalize(RegimeDecision(
                regime=MarketRegime.BULL,
                confidence=confidence,
                timestamp=timestamp,
                reasons=reasons,
                signals=signals,
            ))
        if below_9:
            confidence, reasons = self._score_bearish_confidence(inputs, signals)
            return self._finalize(RegimeDecision(
                regime=MarketRegime.BEAR,
                confidence=confidence,
                timestamp=timestamp,
                reasons=reasons,
                signals=signals,
            ))

        # Priority 4: At 9EMA or perfectly balanced
        return self._finalize(RegimeDecision(
            regime=MarketRegime.NEUTRAL,
            confidence=0.5,
            timestamp=timestamp,
            reasons=["SPY at 9EMA — no clear directional bias"],
            signals=signals,
        ))

    # ------------------------------------------------------------------
    # Intraday update (rare, for major shocks only)
    # ------------------------------------------------------------------

    def should_update_intraday(self, inputs: RegimeInputs) -> bool:
        """
        Returns True only if an intraday shock is large enough to warrant
        re-classifying mid-session. Keeps regime sticky by default.

        Triggers only on genuine intraday events — NOT on pre-session
        conditions that were already baked into the morning classification.
        """
        # Large intraday SPY move
        if abs(inputs.spy_intraday_pct_change) >= self.config.intraday_spy_change_threshold:
            return True
        # VIX crossed into PANIC territory since open (regime was not already PANIC)
        if inputs.vix_current > self.config.panic_vix_threshold and (
            self._last_decision is None
            or self._last_decision.regime != MarketRegime.PANIC
        ):
            return True
        return False

    def update_intraday_if_needed(
        self,
        inputs: RegimeInputs,
        timestamp: datetime,
    ) -> Optional[RegimeDecision]:
        """Call periodically during the session. Returns new decision only
        if regime changed; None otherwise."""
        if not self.should_update_intraday(inputs):
            return None
        new_decision = self.classify(inputs, timestamp)
        return new_decision

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def current_regime(self) -> Optional[MarketRegime]:
        return self._last_decision.regime if self._last_decision else None

    @property
    def last_decision(self) -> Optional[RegimeDecision]:
        return self._last_decision

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _collect_signals(self, inputs: RegimeInputs) -> dict[str, Any]:
        """Raw signal snapshot for the decision record."""
        signals = {
            "spy_close": inputs.spy_close,
            "spy_ema_9_daily": inputs.spy_ema_9_daily,
            "spy_ema_65_daily": inputs.spy_ema_65_daily,
            "spy_ema_200_daily": inputs.spy_ema_200_daily,
            "spy_vs_9ema_pct": (inputs.spy_close - inputs.spy_ema_9_daily) / inputs.spy_ema_9_daily,
            "spy_vs_200ema_pct": (inputs.spy_close - inputs.spy_ema_200_daily) / inputs.spy_ema_200_daily,
            "spy_supertrend": inputs.spy_supertrend_direction.value,
            "spy_20d_pct_change": inputs.spy_20d_pct_change,
            "vix_current": inputs.vix_current,
            "vix_pct_change_14d": inputs.vix_pct_change_14d,
        }
        if inputs.pct_stocks_above_50dma is not None:
            signals["pct_stocks_above_50dma"] = inputs.pct_stocks_above_50dma
        return signals

    def _score_bullish_confidence(
        self,
        inputs: RegimeInputs,
        signals: dict[str, Any],
    ) -> tuple[float, list[str]]:
        """Count signals agreeing with bullish thesis. Returns (conf, reasons)."""
        checks = []
        reasons = []

        above_9 = inputs.spy_close > inputs.spy_ema_9_daily
        checks.append(above_9)
        if above_9:
            reasons.append(f"SPY above daily 9EMA ({signals['spy_vs_9ema_pct']:+.1%})")

        above_65 = inputs.spy_close > inputs.spy_ema_65_daily
        checks.append(above_65)
        if above_65:
            reasons.append("SPY above daily 65EMA")

        above_200 = inputs.spy_close > inputs.spy_ema_200_daily
        checks.append(above_200)
        if above_200:
            reasons.append(f"SPY above daily 200EMA ({signals['spy_vs_200ema_pct']:+.1%})")

        st_green = inputs.spy_supertrend_direction == Direction.LONG
        checks.append(st_green)
        if st_green:
            reasons.append("Daily supertrend green")

        vix_calm = inputs.vix_current < 20
        checks.append(vix_calm)
        if vix_calm:
            reasons.append(f"VIX calm at {inputs.vix_current:.1f}")

        trend_up = inputs.spy_20d_pct_change > 0
        checks.append(trend_up)
        if trend_up:
            reasons.append(f"SPY 20d trend up ({inputs.spy_20d_pct_change:+.1%})")

        # Breadth bonus (optional)
        if inputs.pct_stocks_above_50dma is not None:
            breadth_strong = inputs.pct_stocks_above_50dma > 0.60
            checks.append(breadth_strong)
            if breadth_strong:
                reasons.append(
                    f"Strong breadth: {inputs.pct_stocks_above_50dma:.0%} above 50DMA"
                )

        confidence = sum(checks) / len(checks) if checks else 0.5
        return confidence, reasons

    def _score_bearish_confidence(
        self,
        inputs: RegimeInputs,
        signals: dict[str, Any],
    ) -> tuple[float, list[str]]:
        checks = []
        reasons = []

        below_9 = inputs.spy_close < inputs.spy_ema_9_daily
        checks.append(below_9)
        if below_9:
            reasons.append(f"SPY below daily 9EMA ({signals['spy_vs_9ema_pct']:+.1%})")

        below_65 = inputs.spy_close < inputs.spy_ema_65_daily
        checks.append(below_65)
        if below_65:
            reasons.append("SPY below daily 65EMA")

        below_200 = inputs.spy_close < inputs.spy_ema_200_daily
        checks.append(below_200)
        if below_200:
            reasons.append(f"SPY below daily 200EMA ({signals['spy_vs_200ema_pct']:+.1%})")

        st_red = inputs.spy_supertrend_direction == Direction.SHORT
        checks.append(st_red)
        if st_red:
            reasons.append("Daily supertrend red")

        vix_elevated = inputs.vix_current > 20
        checks.append(vix_elevated)
        if vix_elevated:
            reasons.append(f"VIX elevated at {inputs.vix_current:.1f}")

        trend_down = inputs.spy_20d_pct_change < 0
        checks.append(trend_down)
        if trend_down:
            reasons.append(f"SPY 20d trend down ({inputs.spy_20d_pct_change:+.1%})")

        if inputs.pct_stocks_above_50dma is not None:
            breadth_weak = inputs.pct_stocks_above_50dma < 0.40
            checks.append(breadth_weak)
            if breadth_weak:
                reasons.append(
                    f"Weak breadth: only {inputs.pct_stocks_above_50dma:.0%} above 50DMA"
                )

        confidence = sum(checks) / len(checks) if checks else 0.5
        return confidence, reasons

    def _finalize(self, decision: RegimeDecision) -> RegimeDecision:
        """Detect transition vs prior day, store as last decision, return."""
        if self._last_decision is not None:
            decision.previous_regime = self._last_decision.regime
            decision.is_transition = (
                self._last_decision.regime != decision.regime
            )
        self._last_decision = decision
        return decision


# ============================================================================
# HELPERS
# ============================================================================

def apply_regime_to_features(features, decision: RegimeDecision) -> None:
    """Mutate TickerFeatures.market_regime with the classified regime."""
    features.market_regime = decision.regime


# ============================================================================
# DEMO
# ============================================================================

if __name__ == "__main__":
    classifier = RegimeClassifier()

    # -------------- Scenario 1: Monday, STRONG_BULL --------------
    day1 = datetime(2026, 4, 13, 9, 0)
    strong_bull = RegimeInputs(
        spy_close=525.00,
        spy_ema_9_daily=518.00,
        spy_ema_65_daily=505.00,
        spy_ema_200_daily=480.00,
        spy_supertrend_direction=Direction.LONG,
        spy_20d_pct_change=0.045,
        vix_current=13.2,
        vix_pct_change_14d=-0.08,
        pct_stocks_above_50dma=0.72,
    )
    d1 = classifier.classify(strong_bull, day1)
    print(d1.format_morning_brief())

    # -------------- Scenario 2: Tuesday, flat-ish day → BULL --------------
    day2 = datetime(2026, 4, 14, 9, 0)
    flat_bull = RegimeInputs(
        spy_close=522.00,
        spy_ema_9_daily=520.00,
        spy_ema_65_daily=510.00,
        spy_ema_200_daily=480.00,
        spy_supertrend_direction=Direction.LONG,
        spy_20d_pct_change=0.012,
        vix_current=17.5,
        vix_pct_change_14d=0.10,
        pct_stocks_above_50dma=0.55,
    )
    d2 = classifier.classify(flat_bull, day2)
    print("\n" + d2.format_morning_brief())

    # -------------- Scenario 3: Wednesday, PANIC --------------
    day3 = datetime(2026, 4, 15, 9, 0)
    panic = RegimeInputs(
        spy_close=495.00,
        spy_ema_9_daily=519.00,
        spy_ema_65_daily=510.00,
        spy_ema_200_daily=481.00,
        spy_supertrend_direction=Direction.SHORT,
        spy_20d_pct_change=-0.055,
        vix_current=38.5,
        vix_pct_change_14d=1.20,   # VIX doubled
        pct_stocks_above_50dma=0.28,
    )
    d3 = classifier.classify(panic, day3)
    print("\n" + d3.format_morning_brief())

    # -------------- Scenario 4: Thursday, STRONG_BEAR --------------
    day4 = datetime(2026, 4, 16, 9, 0)
    strong_bear = RegimeInputs(
        spy_close=478.00,
        spy_ema_9_daily=505.00,
        spy_ema_65_daily=512.00,
        spy_ema_200_daily=482.00,
        spy_supertrend_direction=Direction.SHORT,
        spy_20d_pct_change=-0.075,
        vix_current=26.5,
        vix_pct_change_14d=0.65,
        pct_stocks_above_50dma=0.25,
    )
    d4 = classifier.classify(strong_bear, day4)
    print("\n" + d4.format_morning_brief())

    # -------------- Scenario 5: Friday, NEUTRAL (mixed signals) --------------
    day5 = datetime(2026, 4, 17, 9, 0)
    mixed = RegimeInputs(
        spy_close=485.00,
        spy_ema_9_daily=487.00,    # SPY just below 9EMA
        spy_ema_65_daily=500.00,
        spy_ema_200_daily=482.00,  # but still above 200EMA
        spy_supertrend_direction=Direction.SHORT,  # supertrend red
        spy_20d_pct_change=-0.020,
        vix_current=19.0,          # middling VIX
        vix_pct_change_14d=0.05,
        pct_stocks_above_50dma=0.48,
    )
    d5 = classifier.classify(mixed, day5)
    print("\n" + d5.format_morning_brief())

    # -------------- Intraday shock test --------------
    print("\n" + "=" * 72)
    print("INTRADAY SHOCK TEST")
    print("=" * 72)

    # Normal midday check — should NOT update
    normal_intraday = RegimeInputs(
        spy_close=485.00,
        spy_ema_9_daily=487.00,
        spy_ema_65_daily=500.00,
        spy_ema_200_daily=482.00,
        spy_supertrend_direction=Direction.SHORT,
        spy_20d_pct_change=-0.020,
        vix_current=19.5,
        vix_pct_change_14d=0.08,
        spy_intraday_pct_change=-0.008,  # -0.8% — noise
    )
    should = classifier.should_update_intraday(normal_intraday)
    print(f"SPY -0.8%, VIX +0.5: should_update = {should}  ✓ (minor noise, regime sticky)")

    # Flash crash — SHOULD update
    crash_intraday = RegimeInputs(
        spy_close=470.00,
        spy_ema_9_daily=487.00,
        spy_ema_65_daily=500.00,
        spy_ema_200_daily=482.00,
        spy_supertrend_direction=Direction.SHORT,
        spy_20d_pct_change=-0.020,
        vix_current=42.0,
        vix_pct_change_14d=0.95,
        spy_intraday_pct_change=-0.038,  # -3.8% flash crash
    )
    should = classifier.should_update_intraday(crash_intraday)
    print(f"SPY -3.8%, VIX +95%: should_update = {should}  ✓ (real shock, re-classify)")

    updated = classifier.update_intraday_if_needed(
        crash_intraday,
        datetime(2026, 4, 17, 11, 30),
    )
    if updated:
        print(f"\nIntraday re-classification: {updated.regime.value.upper()}")
        print(f"  Transition: {updated.previous_regime.value} → {updated.regime.value}")
        print(f"  Reason: {updated.reasons[0]}")

    print()
