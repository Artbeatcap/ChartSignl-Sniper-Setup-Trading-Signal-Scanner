"""
conviction_score.py — minimal shim for regime_classifier + alert_pipeline imports.

The full conviction engine is not shipped; alert_consolidator builds SetupResult
instances directly. ConvictionScorer.evaluate_all is a no-op stub.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Direction(Enum):
    LONG = "long"
    SHORT = "short"


class MarketRegime(Enum):
    PANIC = "panic"
    STRONG_BEAR = "strong_bear"
    BEAR = "bear"
    NEUTRAL = "neutral"
    BULL = "bull"
    STRONG_BULL = "strong_bull"


class LiquidityTier(Enum):
    TIER_A = "tier_a"
    TIER_B = "tier_b"
    BOTH = "both"
    NONE = "none"


class SetupId(Enum):
    VIX_SPIKE_MEAN_REVERSION = 1
    VIX_CRASH_REVERSAL = 2
    EARNINGS_MISS_SHORT = 3
    EARNINGS_BEAT_FADE_ATH = 4
    NEWS_GAP_UP_BEAR_MARKET = 5
    PRIOR_DAY_RUNNER_FADE = 6
    EXHAUSTION_GAP_SHORT = 7
    ORL_VWAP_9EMA = 8
    DEAD_CAT_BOUNCE = 9
    BLACK_SWAN_REVERSAL = 10
    RIBBON_BREAK_SHORT = 11


@dataclass
class TickerFeatures:
    """Placeholder — consolidator does not use process_candidates()."""

    ticker: str = ""
    timestamp: Optional[datetime] = None
    price: float = 0.0


@dataclass
class SetupResult:
    setup_id: SetupId
    setup_name: str
    direction: Direction
    ticker: str
    base_conditions_met: bool
    time_window_valid: bool
    confluence_points: int
    max_points: int
    regime_penalty: int = 0
    reasons_fired: list[str] = field(default_factory=list)
    reasons_failed: list[str] = field(default_factory=list)
    entry_hint: str = ""
    exit_hint: str = ""

    @property
    def fires(self) -> bool:
        return (
            self.base_conditions_met
            and self.time_window_valid
            and self.normalized_score >= 0.60
        )

    @property
    def normalized_score(self) -> float:
        if not self.base_conditions_met or not self.time_window_valid:
            return 0.0
        if self.max_points == 0:
            return 1.0 if self.base_conditions_met else 0.0
        net = max(0, self.confluence_points - self.regime_penalty)
        return min(1.0, net / self.max_points)

    @property
    def conviction_grade(self) -> str:
        s = self.normalized_score
        if not self.fires:
            return "—"
        if s >= 0.85:
            return "A+"
        if s >= 0.75:
            return "A"
        if s >= 0.65:
            return "B"
        return "C"


class ConvictionScorer:
    """Stub — AlertPipeline.process_candidates is not used in production path."""

    def __init__(self, *args, **kwargs):
        pass

    def evaluate_all(self, feat: TickerFeatures) -> list[SetupResult]:
        return []
