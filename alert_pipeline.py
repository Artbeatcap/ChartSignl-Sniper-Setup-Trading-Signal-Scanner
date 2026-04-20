"""
alert_pipeline.py
-----------------
Setup Sniper — Alert Pipeline.

Orchestrates the ConvictionScorer across a universe of candidates and applies
the noise-cutting rules:
  - Dedup: one alert per ticker, multi-setup hits consolidated
  - Cooldown: 30-min per-ticker cooldown with upgrade override
  - Hourly cap: rolling 60-min window, hard cap (default 5)
  - Per-day ticker cap: max 3 alerts per ticker per session
  - Near-miss logging: candidates that scored 0.45-0.59 logged for tuning

This module is PURE LOGIC. No network, no persistence, no scheduling.
Wire it into your scanner loop at the pipeline layer.

Usage:
    pipeline = AlertPipeline(PipelineConfig())
    alerts = pipeline.process_candidates(
        candidates=[...TickerFeatures],
        current_time=datetime.now(),
    )
    for a in alerts:
        send_to_slack(a.format_text())
"""

from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from conviction_score import (
    ConvictionScorer,
    Direction,
    SetupResult,
    TickerFeatures,
)


# ============================================================================
# CONFIG
# ============================================================================

@dataclass
class PipelineConfig:
    """All tunable parameters live here. Start tight, loosen with evidence."""

    # Firing threshold (passed through to scorer)
    fires_threshold: float = 0.60

    # Rolling hourly cap
    max_alerts_per_hour: int = 5
    hourly_window_minutes: int = 60
    allow_a_plus_override: bool = True  # A+ alerts bypass hourly cap — protects highest-conviction pings

    # Per-ticker cooldown
    per_ticker_cooldown_minutes: int = 30
    upgrade_score_threshold: float = 0.15  # Score must jump by this to re-alert
    max_alerts_per_ticker_per_day: int = 3

    # Near-miss logging
    near_miss_floor: float = 0.45  # Anything 0.45-0.59 gets logged as near-miss
    near_miss_buffer_size: int = 200

    # Ranking tie-breaks (when at cap)
    use_rvol_tiebreak: bool = True


# ============================================================================
# ALERT DATA MODEL
# ============================================================================

@dataclass
class Alert:
    """A single consolidated, ship-ready alert."""

    alert_id: str
    ticker: str
    timestamp: datetime
    primary_setup: SetupResult
    supporting_setups: list[SetupResult]  # Other firing setups, highest first
    is_upgrade: bool = False
    previous_grade: Optional[str] = None  # For upgrade context

    # Snapshot of features at alert time (for alert context, not re-evaluation)
    price: float = 0.0
    rvol: float = 1.0
    atr_daily: float = 0.0

    @property
    def score(self) -> float:
        return self.primary_setup.normalized_score

    @property
    def grade(self) -> str:
        return self.primary_setup.conviction_grade

    @property
    def direction(self) -> Direction:
        return self.primary_setup.direction

    def format_compact(self) -> str:
        """One-liner for dense feeds."""
        direction = self.direction.value.upper()
        upgrade = " ⬆UPGRADE" if self.is_upgrade else ""
        support = f" +{len(self.supporting_setups)}" if self.supporting_setups else ""
        return (
            f"[{self.grade}]{upgrade} {self.ticker} {direction} · "
            f"{self.primary_setup.setup_name}{support} · "
            f"${self.price:.2f} · RVOL {self.rvol:.1f}x"
        )

    def format_text(self) -> str:
        """Full alert — Slack/push notification ready."""
        direction = self.direction.value.upper()
        t = self.timestamp.strftime("%H:%M:%S")

        header = f"⚡ [{self.grade}] {self.ticker} {direction}"
        if self.is_upgrade:
            header += f" ⬆ UPGRADE (from {self.previous_grade})"

        lines = [
            header,
            f"📋 {self.primary_setup.setup_name}  ·  score {self.score:.2f}  ·  {t} ET",
        ]

        if self.supporting_setups:
            supporting = ", ".join(
                f"{s.setup_name} [{s.conviction_grade}]"
                for s in self.supporting_setups
            )
            lines.append(f"🔀 Also firing: {supporting}")

        atr_frag = f" · ATR ${self.atr_daily:.2f}" if self.atr_daily > 0 else ""
        lines.append(f"💰 ${self.price:.2f} · RVOL {self.rvol:.1f}x{atr_frag}")

        # Top 5 reasons — full list is in the dict form
        reasons = self.primary_setup.reasons_fired[:5]
        if reasons:
            lines.append(f"✅ {' · '.join(reasons)}")

        lines.append(f"🎯 Entry: {self.primary_setup.entry_hint}")
        lines.append(f"🛑 Exit:  {self.primary_setup.exit_hint}")

        return "\n".join(lines)

    def format_dict(self) -> dict:
        """Structured form for JSON/DB/API delivery."""
        return {
            "alert_id": self.alert_id,
            "ticker": self.ticker,
            "timestamp": self.timestamp.isoformat(),
            "grade": self.grade,
            "score": self.score,
            "direction": self.direction.value,
            "is_upgrade": self.is_upgrade,
            "previous_grade": self.previous_grade,
            "primary_setup": {
                "id": self.primary_setup.setup_id.value,
                "name": self.primary_setup.setup_name,
                "reasons_fired": self.primary_setup.reasons_fired,
                "entry_hint": self.primary_setup.entry_hint,
                "exit_hint": self.primary_setup.exit_hint,
                "confluence": f"{self.primary_setup.confluence_points}/{self.primary_setup.max_points}",
            },
            "supporting_setups": [
                {
                    "id": s.setup_id.value,
                    "name": s.setup_name,
                    "grade": s.conviction_grade,
                    "score": s.normalized_score,
                }
                for s in self.supporting_setups
            ],
            "context": {
                "price": self.price,
                "rvol": self.rvol,
                "atr_daily": self.atr_daily,
            },
        }


@dataclass
class NearMiss:
    """A candidate that scored close to firing — logged for tuning."""
    ticker: str
    timestamp: datetime
    setup_name: str
    score: float
    missing_reasons: list[str]


@dataclass
class TickerAlertState:
    """Per-ticker rolling state within a session."""
    ticker: str
    last_alert_time: Optional[datetime] = None
    last_alert_score: float = 0.0
    last_alert_grade: str = ""
    alerts_today: int = 0


# ============================================================================
# PIPELINE
# ============================================================================

class AlertPipeline:
    """
    Stateful, single-session pipeline. Call reset_session() at each trading
    day's start (or let the auto-detect handle it when the date changes).

    The pipeline tracks:
      - _alert_history: rolling deque of all alerts (for hourly cap)
      - _ticker_states: per-ticker cooldown + upgrade tracking
      - _near_misses: tuning/debugging feed
      - _session_date: auto-resets per-ticker daily counts on date change
    """

    def __init__(self, config: Optional[PipelineConfig] = None):
        self.config = config or PipelineConfig()
        self._alert_history: deque[Alert] = deque()
        self._ticker_states: dict[str, TickerAlertState] = {}
        self._near_misses: deque[NearMiss] = deque(
            maxlen=self.config.near_miss_buffer_size
        )
        self._session_date: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def reset_session(self, new_date: datetime) -> None:
        """Clear per-day state. Keep cooldown / upgrade logic reasonable
        across the reset by wiping ticker states entirely."""
        self._ticker_states.clear()
        self._near_misses.clear()
        self._session_date = new_date.date()
        # Keep alert_history so rolling hourly cap still works across boundary

    def _maybe_auto_reset(self, now: datetime) -> None:
        if self._session_date is None:
            self._session_date = now.date()
            return
        if now.date() != self._session_date:
            self.reset_session(now)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def process_candidates(
        self,
        candidates: list[TickerFeatures],
        current_time: datetime,
    ) -> list[Alert]:
        """
        Evaluate all candidates, apply noise-cutting, emit ranked alerts.

        Returns ONLY the alerts that should be sent to the user. Any
        suppressed or near-miss candidates are logged internally and
        accessible via get_near_misses() / get_suppressed_summary().
        """
        self._maybe_auto_reset(current_time)
        self._prune_history(current_time)

        scorer = ConvictionScorer(
            current_time=current_time,
            fires_threshold=self.config.fires_threshold,
        )

        # 1. Score every candidate against every enabled setup
        scored: list[tuple[TickerFeatures, list[SetupResult]]] = []
        for feat in candidates:
            all_results = scorer.evaluate_all(feat)
            scored.append((feat, all_results))

        # 2. Build one consolidated "candidate alert" per ticker
        candidate_alerts: list[Alert] = []
        for feat, results in scored:
            fires = [r for r in results if r.fires]

            # Log near-misses regardless of fire status
            self._log_near_misses(feat, results, current_time)

            if not fires:
                continue

            primary = fires[0]  # Already sorted by score desc
            supporting = fires[1:]

            candidate_alerts.append(Alert(
                alert_id=str(uuid.uuid4())[:8],
                ticker=feat.ticker,
                timestamp=current_time,
                primary_setup=primary,
                supporting_setups=supporting,
                price=feat.price,
                rvol=feat.rvol,
                atr_daily=feat.atr_daily,
            ))

        # 3. Filter by cooldown (with upgrade override)
        after_cooldown: list[Alert] = []
        for alert in candidate_alerts:
            allowed, is_upgrade, prev_grade = self._check_cooldown_and_upgrade(
                alert, current_time
            )
            if allowed:
                alert.is_upgrade = is_upgrade
                alert.previous_grade = prev_grade
                after_cooldown.append(alert)

        # 4. Rank by score, tie-break by RVOL then ticker
        after_cooldown.sort(
            key=lambda a: (
                -a.score,
                -a.rvol if self.config.use_rvol_tiebreak else 0,
                a.ticker,
            )
        )

        # 5. Apply hourly cap
        final_alerts = self._apply_hourly_cap(after_cooldown, current_time)

        # 6. Record state for emitted alerts
        for alert in final_alerts:
            self._record_alert(alert, current_time)

        return final_alerts

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _prune_history(self, now: datetime) -> None:
        """Drop alerts older than the hourly window — the deque stays lean."""
        cutoff = now - timedelta(minutes=self.config.hourly_window_minutes)
        while self._alert_history and self._alert_history[0].timestamp < cutoff:
            self._alert_history.popleft()

    def _check_cooldown_and_upgrade(
        self, alert: Alert, now: datetime
    ) -> tuple[bool, bool, Optional[str]]:
        """
        Returns (allowed, is_upgrade, previous_grade).

        Logic:
          - No prior alert for ticker: allowed, not an upgrade.
          - Prior alert, outside cooldown: allowed, not an upgrade.
          - Prior alert, inside cooldown, score jumped 0.15+: allowed as UPGRADE.
          - Prior alert, inside cooldown, score flat/lower: BLOCKED.
          - Any: if daily cap hit, BLOCKED.
        """
        state = self._ticker_states.get(alert.ticker)

        # Daily cap is absolute
        if state and state.alerts_today >= self.config.max_alerts_per_ticker_per_day:
            return False, False, None

        if state is None or state.last_alert_time is None:
            return True, False, None

        cooldown_end = state.last_alert_time + timedelta(
            minutes=self.config.per_ticker_cooldown_minutes
        )
        if now >= cooldown_end:
            return True, False, None

        # Inside cooldown — only allow as upgrade
        score_jump = alert.score - state.last_alert_score
        if score_jump >= self.config.upgrade_score_threshold:
            return True, True, state.last_alert_grade

        return False, False, None

    def _apply_hourly_cap(
        self, ranked_alerts: list[Alert], now: datetime
    ) -> list[Alert]:
        """
        Assume history already pruned. Admit alerts until cap hit.
        With allow_a_plus_override, A+ alerts can exceed cap.
        """
        emitted: list[Alert] = []
        in_window = len(self._alert_history)

        for alert in ranked_alerts:
            at_cap = (in_window + len(emitted)) >= self.config.max_alerts_per_hour
            if at_cap:
                if self.config.allow_a_plus_override and alert.grade == "A+":
                    emitted.append(alert)
                    continue
                # Otherwise suppressed
                continue
            emitted.append(alert)

        return emitted

    def _record_alert(self, alert: Alert, now: datetime) -> None:
        self._alert_history.append(alert)
        state = self._ticker_states.setdefault(
            alert.ticker, TickerAlertState(ticker=alert.ticker)
        )
        state.last_alert_time = now
        state.last_alert_score = alert.score
        state.last_alert_grade = alert.grade
        state.alerts_today += 1

    def _log_near_misses(
        self,
        feat: TickerFeatures,
        results: list[SetupResult],
        now: datetime,
    ) -> None:
        """Capture setups that almost fired for tuning visibility."""
        for r in results:
            s = r.normalized_score
            if (
                self.config.near_miss_floor <= s < self.config.fires_threshold
                and r.base_conditions_met
                and r.time_window_valid
            ):
                self._near_misses.append(NearMiss(
                    ticker=feat.ticker,
                    timestamp=now,
                    setup_name=r.setup_name,
                    score=s,
                    missing_reasons=r.reasons_failed[:3],
                ))

    # ------------------------------------------------------------------
    # Introspection (for dashboards, debug, tuning)
    # ------------------------------------------------------------------

    def hourly_alert_count(self, now: datetime) -> int:
        self._prune_history(now)
        return len(self._alert_history)

    def ticker_on_cooldown(self, ticker: str, now: datetime) -> bool:
        state = self._ticker_states.get(ticker)
        if state is None or state.last_alert_time is None:
            return False
        cooldown_end = state.last_alert_time + timedelta(
            minutes=self.config.per_ticker_cooldown_minutes
        )
        return now < cooldown_end

    def get_near_misses(self, limit: int = 50) -> list[NearMiss]:
        """Most recent first."""
        return list(self._near_misses)[-limit:][::-1]

    def get_alert_history(self, limit: int = 50) -> list[Alert]:
        return list(self._alert_history)[-limit:][::-1]

    def stats(self, now: datetime) -> dict:
        self._prune_history(now)
        return {
            "alerts_in_hour_window": len(self._alert_history),
            "cap": self.config.max_alerts_per_hour,
            "tickers_on_cooldown": sum(
                1 for t in self._ticker_states
                if self.ticker_on_cooldown(t, now)
            ),
            "tickers_alerted_today": len(self._ticker_states),
            "near_miss_count": len(self._near_misses),
        }


# ============================================================================
# DEMO
# ============================================================================

if __name__ == "__main__":
    from conviction_score import LiquidityTier, MarketRegime

    def make_candidate(
        ticker, price, setup_target="setup_3", **overrides
    ) -> TickerFeatures:
        """Builds a minimally-complete TickerFeatures tilted toward a setup."""
        base = TickerFeatures(
            ticker=ticker,
            timestamp=datetime(2026, 4, 18, 9, 47),
            price=price,
            previous_close=price * 1.08,  # Down 8% default
            session_open=price * 1.02,
            session_high=price * 1.03,
            session_low=price * 0.98,
            premarket_high=price * 1.05,
            premarket_low=price * 0.97,
            ema_9_5min=price * 1.01,  # Price below 9EMA
            ema_65_5min=price * 1.015,
            ema_200_5min=price * 1.02,
            ema_9_slope_5min=-0.3,
            ema_9_daily=price * 1.03,
            ema_65_daily=price * 1.05,
            ema_200_daily=price * 1.08,
            supertrend_5min_direction=Direction.SHORT,
            supertrend_daily_direction=Direction.SHORT,
            vwap=price * 1.012,
            atr_daily=price * 0.025,
            session_volume=10_000_000,
            session_dollar_volume=price * 10_000_000,
            rvol=4.0,
            liquidity_tier=LiquidityTier.TIER_A,
            week_52_high=price * 1.3,
            week_52_low=price * 0.9,
            prior_day_high=price * 1.06,
            prior_day_low=price * 0.95,
            nearest_daily_resistance=price * 1.02,
            gap_pct=-0.08,
            first_5min_candle_red=True,
            reclaimed_premarket_high=False,
            market_regime=MarketRegime.BEAR,
            vix=22.0,
        )
        if setup_target == "setup_3":
            base.earnings_today = True
            base.earnings_surprise_pct = -0.12
            base.earnings_double_miss = True
        for k, v in overrides.items():
            setattr(base, k, v)
        return base

    # Build a realistic candidate mix
    candidates = [
        # 1. Strong earnings short
        make_candidate("UNH", 485.00),
        # 2. Another earnings short
        make_candidate("TGT", 124.00),
        # 3. Runner fade (Tier B liquidity)
        make_candidate(
            "GSIW", 3.20,
            setup_target="setup_6",
            liquidity_tier=LiquidityTier.TIER_B,
            prior_day_pct_change=1.25,  # 125% prior day
            rvol=15.0,
        ),
        # 4. Earnings short but weak confluence (should still fire, lower grade)
        make_candidate(
            "PDD", 98.00,
            rvol=2.0,
            market_regime=MarketRegime.NEUTRAL,
        ),
        # 5. Earnings miss but reclaimed PM high — base conditions fail, won't fire
        make_candidate("SMCI", 42.00, reclaimed_premarket_high=True),
        # 6. Would fire but already capped (6th alert)
        make_candidate("UPS", 140.00),
        # 7. Would fire but already capped
        make_candidate("PINS", 28.00),
    ]

    config = PipelineConfig(
        max_alerts_per_hour=5,
        allow_a_plus_override=True,  # now the default — A+ alerts always get through
    )
    pipeline = AlertPipeline(config)

    # ===== Tick 1: 9:47 AM — first processing =====
    t1 = datetime(2026, 4, 18, 9, 47)
    print("\n" + "=" * 72)
    print(f"TICK 1 — {t1.time()} — processing {len(candidates)} candidates")
    print("=" * 72)
    alerts_1 = pipeline.process_candidates(candidates, t1)
    print(f"\n{len(alerts_1)} alerts fired (cap: {config.max_alerts_per_hour}):\n")
    for a in alerts_1:
        print(a.format_compact())
    print(f"\nStats: {pipeline.stats(t1)}")

    # ===== Tick 2: 9:52 AM — same candidates, cooldown should block =====
    t2 = datetime(2026, 4, 18, 9, 52)
    # Re-stamp candidates to t2
    for c in candidates:
        c.timestamp = t2
    alerts_2 = pipeline.process_candidates(candidates, t2)
    print("\n" + "=" * 72)
    print(f"TICK 2 — {t2.time()} — same candidates, 5 min later")
    print("=" * 72)
    print(f"\n{len(alerts_2)} alerts fired (cooldown should block everyone):\n")
    for a in alerts_2:
        print(a.format_compact())
    if not alerts_2:
        print("  (all on cooldown, as expected — no noise)")

    # ===== Tick 3: 9:55 AM — UNH score jumps, should UPGRADE =====
    t3 = datetime(2026, 4, 18, 9, 55)
    # Boost UNH's confluence so its score jumps enough to trigger upgrade
    candidates[0].timestamp = t3
    # (Already A+ in initial config, so let's fabricate a scenario where
    # PDD goes from B to A+ instead — more realistic)
    candidates[3].rvol = 6.0
    candidates[3].market_regime = MarketRegime.BEAR
    candidates[3].timestamp = t3

    alerts_3 = pipeline.process_candidates(candidates, t3)
    print("\n" + "=" * 72)
    print(f"TICK 3 — {t3.time()} — PDD confluence strengthened (RVOL 2→6, regime→bear)")
    print("=" * 72)
    print(f"\n{len(alerts_3)} alerts fired:\n")
    for a in alerts_3:
        print(a.format_compact())

    # ===== Full formatted output for the top alert =====
    if alerts_1:
        print("\n" + "=" * 72)
        print("FULL ALERT FORMAT (for Slack/push):")
        print("=" * 72)
        print(alerts_1[0].format_text())

    # ===== Near-misses =====
    print("\n" + "=" * 72)
    print("NEAR-MISSES (scored 0.45-0.59 — useful for tuning):")
    print("=" * 72)
    near = pipeline.get_near_misses(limit=10)
    if near:
        for nm in near:
            print(f"  {nm.ticker:6} {nm.setup_name:35} score={nm.score:.2f}")
            print(f"         missing: {', '.join(nm.missing_reasons)}")
    else:
        print("  (no near-misses recorded)")

    print("\n" + "=" * 72)
    print("FINAL PIPELINE STATS")
    print("=" * 72)
    print(pipeline.stats(t3))
    print()
