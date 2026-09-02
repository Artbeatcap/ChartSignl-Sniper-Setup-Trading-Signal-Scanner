"""
Intraday Scanner — Phase 2+3 Engine
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Persistent process that runs during market hours (9:25 AM - 4:05 PM ET).
Polls live 5-min bars every 5 minutes and evaluates:

  - Setup 8: ORL + VWAP + 9 EMA (state machine per ticker)
  - VIX Monitor: Setups 1, 2 & 10 (background threshold checks)
  - Setup 5: Gap Up Reversal in Bear Market
  - Setup 9: Dead Cat Bounce (Day Trade Only)

Behavioral guardrails baked in:
  - Rule 2: Patience timer — delays non-critical alerts 15 min after open
  - Rule 3: ATR included in every entry alert

Usage:
  python main.py intraday          # Run during market hours
  python main.py intraday --once   # Single poll cycle (for testing)
"""
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

from config import ScannerConfig
from polygon_client import PolygonClient
from indicators import atr
from scanner_orl_vwap import OrlVwapScanner, IntradayAlert
from scanner_vix import VixMonitor
from scanner_dead_cat import DeadCatScanner
from scanner_gap_reversal import GapReversalScanner
from scanner_ribbon_break import (
    RibbonBreakScanner,
    filter_premarket_bars,
    filter_rth_bars,
)
from alerts import AlertManager
from catalyst_briefing import CatalystBriefing
from universe_builder import UniverseBuilder

logger = logging.getLogger(__name__)


class IntradayEngine:
    """
    The Phase 2+3 engine. Manages the polling loop, ticker universe,
    bar fetching, and alert routing.

    Setups running:
      Phase 2: Setup 8 (ORL+VWAP+9EMA), Setup 1 (VIX Spike), Setup 2 (VIX Crash)
      Phase 3: Setup 9 (Dead Cat Bounce), Setup 5 (Gap Reversal), Setup 10 (Black Swan)
    """

    def __init__(self, config: ScannerConfig):
        self.config = config
        self.ic = config.intraday
        self.client = PolygonClient(config.api)

        self.universe_builder = UniverseBuilder(self.client, config.universe)
        self._universe_poll_cycle = 0
        try:
            self.universe_builder.build_universe(force=False)
        except Exception as e:
            logger.warning(f"Universe preload failed (intraday will retry): {e}")

        briefing = None
        if config.catalyst.available:
            briefing = CatalystBriefing(
                polygon_api_key=config.api.api_key,
                fmp_api_key=config.fmp.api_key,
                anthropic_api_key=config.catalyst.anthropic_api_key,
                model=config.catalyst.model,
            )
        self.alert_mgr = AlertManager(
            config.alerts,
            briefing=briefing,
            universe_builder=self.universe_builder,
            universe_top_n=config.universe.top_n,
        )

        # Phase 2 scanners
        self.orl_scanner = OrlVwapScanner(
            opening_range_bars=self.ic.opening_range_bars,
            ema_period=self.ic.ema_period,
            slope_lookback=self.ic.ema_slope_lookback,
            range_config=self.config.range_floor,
        )
        self.vix_monitor = VixMonitor(self.client, self.ic)

        # Phase 3 scanners
        self.dead_cat_scanner = DeadCatScanner(self.client)
        self.gap_reversal_scanner = GapReversalScanner(self.client)
        self.ribbon_scanner = RibbonBreakScanner(config)
        self._ribbon_primed: set[str] = set()
        self._ribbon_bar_counts: dict[str, int] = {}

        # Session state
        self._session_date: Optional[str] = None
        self._daily_atr_cache: dict[str, float] = {}
        self._bar_counts: dict[str, int] = {}  # Track bars seen per ticker

    # ═══════════════════════════════════════════════════════════════════
    # MAIN LOOP
    # ═══════════════════════════════════════════════════════════════════

    def run(self, single_cycle: bool = False):
        """
        Main loop. Runs until market close or keyboard interrupt.
        If single_cycle=True, runs one poll and exits (for testing).
        """
        logger.info("═══ INTRADAY ENGINE STARTING ═══")
        logger.info(f"  Tickers: {', '.join(self._get_tickers())}")
        logger.info(f"  Poll interval: {self.ic.poll_interval_seconds}s")
        logger.info(f"  Market hours: {self.ic.scan_start} - {self.ic.scan_end} ET")

        try:
            while True:
                now = datetime.now()
                current_time = now.strftime("%H:%M")
                today = now.strftime("%Y-%m-%d")

                # New session? Reset state
                if self._session_date != today:
                    self._new_session(today)

                # Check if within market hours
                if not self._is_market_hours(current_time):
                    if single_cycle:
                        logger.info("Outside market hours. Exiting (single cycle mode).")
                        return
                    # Wait and check again
                    logger.debug(f"Outside market hours ({current_time}). Sleeping 60s...")
                    time.sleep(60)
                    continue

                # ─── POLL CYCLE ─────────────────────────────────
                cycle_start = time.time()
                logger.info(f"─── Poll cycle at {now.strftime('%H:%M:%S')} ───")

                self._universe_poll_cycle += 1
                if self._universe_poll_cycle % 12 == 0:
                    try:
                        self.universe_builder.build_universe(force=True)
                        tier2_n = len(
                            self.universe_builder.get_tier2_candidates(refresh=False)
                        )
                        logger.info(
                            f"Universe refreshed (hourly): {tier2_n} Tier 2 candidates"
                        )
                    except Exception as e:
                        logger.warning(f"Universe refresh failed: {e}")

                tickers = self._get_tickers()
                alerts_fired = 0

                # 1. Process each ticker for Setup 8
                for ticker in tickers:
                    try:
                        alert = self._process_ticker(ticker, today)
                        if alert:
                            self._dispatch_intraday_alert(alert, current_time)
                            alerts_fired += 1
                    except Exception as e:
                        logger.error(f"Error processing {ticker}: {e}")

                # 1b. Setup 11 Stage 3 — only Stage 2 qualified ribbon names
                try:
                    alerts_fired += self._process_ribbon_break(today, current_time)
                except Exception as e:
                    logger.error(f"Ribbon break Stage 3 error: {e}")

                # 2. VIX monitor (Setups 1, 2, 10)
                try:
                    vix_alerts = self.vix_monitor.check()
                    for va in vix_alerts:
                        self._dispatch_vix_alert(va)
                        alerts_fired += 1
                except Exception as e:
                    logger.error(f"VIX monitor error: {e}")

                # 3. Setup 5: Gap Up Reversal in Bear Market
                try:
                    is_bearish = self.gap_reversal_scanner.check_market_regime()
                    if is_bearish:
                        for ticker in tickers:
                            bars = self.client.get_intraday_bars(ticker, minutes=5, date=today)
                            market_bars = self._filter_market_hours_bars(bars) if bars else []
                            if market_bars:
                                gap_alert = self.gap_reversal_scanner.check_ticker(
                                    ticker,
                                    current_time,
                                    market_bars,
                                    daily_atr=self._get_daily_atr(ticker),
                                )
                                if gap_alert:
                                    self._dispatch_phase3_alert(gap_alert)
                                    alerts_fired += 1
                except Exception as e:
                    logger.error(f"Gap reversal scanner error: {e}")

                # 4. Setup 9: Dead Cat Bounce (only 9:35-9:45 window)
                if "09:35" <= current_time <= "09:45":
                    for ticker in tickers:
                        try:
                            bars = self.client.get_intraday_bars(ticker, minutes=5, date=today)
                            market_bars = self._filter_market_hours_bars(bars) if bars else []
                            if market_bars:
                                dc_alert = self.dead_cat_scanner.check_ticker(
                                    ticker,
                                    current_time,
                                    market_bars,
                                    daily_atr=self._get_daily_atr(ticker),
                                )
                                if dc_alert:
                                    self._dispatch_phase3_alert(dc_alert)
                                    alerts_fired += 1
                        except Exception as e:
                            logger.error(f"Dead cat scanner error for {ticker}: {e}")

                elapsed = time.time() - cycle_start
                logger.info(
                    f"  Cycle complete: {len(tickers)} tickers, {alerts_fired} alerts, {elapsed:.1f}s"
                )

                if single_cycle:
                    return

                # Sleep until next cycle
                sleep_time = max(0, self.ic.poll_interval_seconds - elapsed)
                if sleep_time > 0:
                    logger.debug(f"  Sleeping {sleep_time:.0f}s until next cycle...")
                    time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("\n═══ INTRADAY ENGINE STOPPED (Ctrl+C) ═══")

    # ═══════════════════════════════════════════════════════════════════
    # TICKER PROCESSING
    # ═══════════════════════════════════════════════════════════════════

    def _process_ticker(self, ticker: str, today: str) -> Optional[IntradayAlert]:
        """
        Fetch latest 5-min bars for a ticker and feed them to Setup 8.
        Only sends NEW bars (tracks bar count per ticker).
        """
        # Fetch today's 5-min bars
        bars = self.client.get_intraday_bars(ticker, minutes=5, date=today)
        if not bars:
            return None

        # Filter to regular + market hours only (9:30 AM - 4:00 PM ET)
        # Polygon timestamps are in ms UTC
        market_bars = self._filter_market_hours_bars(bars)
        if not market_bars:
            return None

        prev_count = self._bar_counts.get(ticker, 0)
        current_count = len(market_bars)

        if current_count <= prev_count:
            return None  # No new bars

        self._bar_counts[ticker] = current_count

        # Get daily ATR for this ticker (cached per session)
        daily_atr = self._get_daily_atr(ticker)

        # Feed only NEW bars to the scanner
        new_bars = market_bars[prev_count:]
        alert = None
        for bar in new_bars:
            result = self.orl_scanner.process_bar(
                ticker=ticker,
                open_price=bar["o"],
                high=bar["h"],
                low=bar["l"],
                close=bar["c"],
                volume=bar["v"],
                daily_atr_value=daily_atr,
            )
            if result:
                alert = result  # Take the latest alert if multiple

        return alert

    def _process_ribbon_break(self, today: str, current_time: str) -> int:
        """
        Stage 3 execution for Setup 11. Gated on Stage 2 watchlist.
        Does not fire before 09:45 (opening range must complete).
        Returns number of alerts dispatched.
        """
        if current_time < "09:45":
            return 0
        names = self.ribbon_scanner.stage2_qualified()
        if not names:
            return 0

        fired = 0
        for watch in names:
            ticker = watch.ticker
            try:
                bars = self.client.get_intraday_bars(ticker, minutes=5, date=today)
                if not bars:
                    continue
                if ticker not in self._ribbon_primed:
                    self.ribbon_scanner.exec_engine.prime(watch, filter_premarket_bars(bars))
                    self._ribbon_primed.add(ticker)
                    self._ribbon_bar_counts[ticker] = 0

                rth = filter_rth_bars(bars)
                prev = self._ribbon_bar_counts.get(ticker, 0)
                if len(rth) <= prev:
                    continue
                new_bars = rth[prev:]
                self._ribbon_bar_counts[ticker] = len(rth)
                alert = None
                for bar in new_bars:
                    result = self.ribbon_scanner.exec_engine.process_rth_bar(
                        ticker=ticker,
                        open_price=bar["o"],
                        high=bar["h"],
                        low=bar["l"],
                        close=bar["c"],
                        volume=bar["v"],
                    )
                    if result:
                        alert = result
                if alert:
                    self._dispatch_intraday_alert(alert, current_time)
                    fired += 1
            except Exception as e:
                logger.error(f"Ribbon Stage 3 error for {ticker}: {e}")
        return fired

    def _filter_market_hours_bars(self, bars: list[dict]) -> list[dict]:
        """Filter bars to regular market hours (9:30-16:00 ET)."""
        filtered = []
        for bar in bars:
            ts_ms = bar.get("t", 0)
            if ts_ms == 0:
                continue
            # Convert to datetime (UTC)
            dt = datetime.utcfromtimestamp(ts_ms / 1000)

            # 9:30 AM ET = 13:30 UTC (EDT) through 16:00 ET = 20:00 UTC
            utc_market_open = 13 * 60 + 30   # 13:30 UTC = 9:30 ET (EDT)
            utc_market_close = 20 * 60       # 20:00 UTC = 16:00 ET (EDT)
            bar_utc_min = dt.hour * 60 + dt.minute

            if utc_market_open <= bar_utc_min < utc_market_close:
                filtered.append(bar)

        return filtered

    # ═══════════════════════════════════════════════════════════════════
    # ALERT DISPATCH
    # ═══════════════════════════════════════════════════════════════════

    def _dispatch_intraday_alert(self, alert: IntradayAlert, current_time: str):
        """
        Route an intraday alert based on stage.
        Lighter style: one-liners for stages 1-3, full table for stage 4.
        Rule 2: Patience timer — suppress non-entry alerts before 9:45 AM.
        """
        # Rule 2: Patience timer
        if current_time < "09:45" and alert.stage_number < 4:
            logger.debug(f"  {alert.ticker}: Stage {alert.stage} suppressed (patience timer)")
            return

        # Optionally enrich with catalyst context
        self.alert_mgr.enrich_alert(alert)

        if alert.stage_number < 4:
            # ─── LIGHTER: One-liner for progression stages ─────
            self._send_stage_update(alert)
        else:
            # ─── FULL TABLE: Entry signal ──────────────────────
            self._send_entry_signal(alert)

    def _send_stage_update(self, alert: IntradayAlert):
        """Send a short one-liner for stage progression."""
        now = datetime.now().strftime("%H:%M")
        line = f"⚡ {alert.ticker} — {alert.message}"

        logger.info(f"  {line}")

        # Discord
        if self.config.alerts.discord_webhook_url:
            payload = {"content": f"`{now}` {line}"}
            self.alert_mgr._send_discord(payload)

        # Slack
        if self.config.alerts.slack_webhook_url:
            payload = {"text": f"`{now}` {line}"}
            self.alert_mgr._send_slack(payload)

        # No email for stage updates — too noisy

    def _send_entry_signal(self, alert: IntradayAlert):
        """Send full alert with levels table for entry signals."""
        now = datetime.now().strftime("%H:%M")
        exit_display = alert.exit_plan
        if alert.stop_was_widened and alert.floor_source:
            exit_display = f"{alert.exit_plan} — {alert.floor_source}"

        # Build levels table
        levels = [
            f"{'Entry':<14} ${alert.entry_price:.2f}" if alert.entry_price else "",
            f"{'Stop':<14} ${alert.stop_price:.2f}" if alert.stop_price else "",
            f"{'Target (1R)':<14} ${alert.target_1r:.2f}" if alert.target_1r else "",
            f"{'ORL':<14} ${alert.orl:.2f}",
            f"{'ORH':<14} ${alert.orh:.2f}",
            f"{'9 EMA':<14} ${alert.ema_9:.2f}",
            f"{'VWAP':<14} ${alert.vwap_level:.2f}",
        ]
        if alert.daily_atr:
            levels.append(f"{'Daily ATR':<14} ${alert.daily_atr:.2f}")
        table = "\n".join(l for l in levels if l)

        # Console
        print(f"\n{'━' * 50}")
        print(f"  🔴 ENTRY SIGNAL — {alert.ticker} (SHORT)")
        print(f"  {alert.setup}")
        print(f"{'━' * 50}")
        print(f"  {alert.message}")
        print(f"\n{table}")
        print(f"\n  Exit: {exit_display}")
        print(f"  Confidence: {alert.confidence}")
        print(f"{'━' * 50}\n")

        # Discord
        if self.config.alerts.discord_webhook_url:
            payload = {
                "content": (
                    f"🔴 **ENTRY SIGNAL — {alert.ticker} (SHORT)**\n"
                    f"*{alert.setup} — {alert.message}*\n"
                    f"```\n{table}\n```\n"
                    f"**Exit:** {exit_display}\n"
                    f"**Confidence:** {alert.confidence}"
                )
            }
            self.alert_mgr._send_discord(payload)

        # Slack
        if self.config.alerts.slack_webhook_url:
            payload = {
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": f"🔴 ENTRY SIGNAL — {alert.ticker}",
                            "emoji": True,
                        },
                    },
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"*{alert.setup}*\n{alert.message}"},
                    },
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"```\n{table}\n```"},
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Exit:* {exit_display}\n*Confidence:* {alert.confidence}",
                        },
                    },
                ]
            }
            self.alert_mgr._send_slack(payload)

        # Email (entry signals only)
        if self.config.alerts.email_enabled:
            self.alert_mgr._send_email(
                subject=(
                    f"🔴 ENTRY — {alert.ticker} SHORT at ${alert.entry_price:.2f}"
                    if alert.entry_price
                    else f"🔴 ENTRY — {alert.ticker}"
                ),
                html=self._build_entry_email(alert, table),
            )

    def _dispatch_vix_alert(self, alert):
        """Send VIX regime alert — always full (these are rare and high-value)."""
        # Optional: enrich with macro/catalyst context as well
        self.alert_mgr.enrich_alert(alert)
        logger.info(f"  🌐 {alert.setup}: {alert.message}")

        content = (
            f"🌐 **{alert.setup}**\n"
            f"VIX: {alert.vix_current:.1f} ({alert.vix_pct_change:+.1f}% over {alert.lookback_days}d)\n"
            f"**Confidence:** {alert.confidence}\n"
            f"**Action:** {alert.entry}\n"
            f"**Note:** {alert.exit_plan}"
        )

        if self.config.alerts.discord_webhook_url:
            self.alert_mgr._send_discord({"content": content})
        if self.config.alerts.slack_webhook_url:
            self.alert_mgr._send_slack({"text": content})
        if self.config.alerts.email_enabled:
            self.alert_mgr._send_email(
                subject=f"🌐 {alert.setup} — VIX {alert.vix_current:.1f}",
                html=f"<pre>{content}</pre>",
            )

    def _dispatch_phase3_alert(self, alert):
        """
        Send Phase 3 alerts (Setup 5: Gap Reversal, Setup 9: Dead Cat Bounce).
        Always full — these are entry signals, not stage progressions.
        """
        # Optionally enrich with catalyst context
        self.alert_mgr.enrich_alert(alert)

        setup = getattr(alert, "setup", "ALERT")
        ticker = getattr(alert, "ticker", "???")
        direction = getattr(alert, "direction", "")
        message = getattr(alert, "message", "")
        entry = getattr(alert, "entry", "")
        exit_plan = getattr(alert, "exit_plan", "")
        confidence = getattr(alert, "confidence", "")

        # Build levels table
        levels = []
        if getattr(alert, "current_price", None):
            levels.append(f"{'Price':<16} ${alert.current_price:.2f}")
        if getattr(alert, "gap_pct", None) is not None:
            levels.append(f"{'Gap':<16} {alert.gap_pct:+.1f}%")
        if getattr(alert, "ema_9", None):
            levels.append(f"{'9 EMA':<16} ${alert.ema_9:.2f}")
        if getattr(alert, "week_52_low", None):
            levels.append(f"{'52wk Low':<16} ${alert.week_52_low:.2f}")
        if getattr(alert, "prev_close", None):
            levels.append(f"{'Prev Close':<16} ${alert.prev_close:.2f}")
        if getattr(alert, "spy_price", None):
            levels.append(f"{'SPY':<16} ${alert.spy_price:.2f}")
        if getattr(alert, "spy_ema_9", None):
            levels.append(f"{'SPY 9 EMA':<16} ${alert.spy_ema_9:.2f}")
        if getattr(alert, "resistance_level", None):
            levels.append(
                f"{'Resistance':<16} ${alert.resistance_level:.2f} ({alert.resistance_type})"
            )
        if getattr(alert, "daily_atr", None):
            levels.append(f"{'Daily ATR':<16} ${alert.daily_atr:.2f}")
        table = "\n".join(levels) if levels else ""

        # Emoji based on direction
        emoji = "🟡" if "LONG" in direction.upper() else "🔴"

        logger.info(f"  {emoji} {setup} — {ticker}: {message}")

        # Console
        print(f"\n{'━' * 50}")
        print(f"  {emoji} {setup} — {ticker}")
        print(f"  Direction: {direction}")
        print(f"{'━' * 50}")
        print(f"  {message}")
        if table:
            print(f"\n{table}")
        print(f"\n  Entry: {entry}")
        print(f"  Exit: {exit_plan}")
        print(f"  Confidence: {confidence}")
        print(f"{'━' * 50}\n")

        # Discord
        if self.config.alerts.discord_webhook_url:
            content = (
                f"{emoji} **{setup} — {ticker}**\n"
                f"*{direction}*\n"
                f"{message}\n"
            )
            if table:
                content += f"```\n{table}\n```\n"
            content += (
                f"**Entry:** {entry}\n**Exit:** {exit_plan}\n**Confidence:** {confidence}"
            )
            self.alert_mgr._send_discord({"content": content})

        # Slack
        if self.config.alerts.slack_webhook_url:
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": f"{emoji} {setup} — {ticker}",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{direction}*\n{message}"},
                },
            ]
            if table:
                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"```\n{table}\n```"},
                    }
                )
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*Entry:* {entry}\n*Exit:* {exit_plan}\n*Confidence:* {confidence}"
                        ),
                    },
                }
            )
            self.alert_mgr._send_slack({"blocks": blocks})

        # Email
        if self.config.alerts.email_enabled:
            table_html = (
                "<pre style='background:#0f3460;padding:12px;border-radius:4px;"
                "font-size:13px;color:#eee;'>{}</pre>".format(table)
                if table
                else ""
            )
            bg_color = "#2e1a00" if "LONG" in direction.upper() else "#1a1a2e"
            html = f"""
            <div style="font-family:-apple-system,sans-serif;max-width:500px;margin:0 auto;">
              <div style="background:{bg_color};color:#fff;padding:16px 20px;border-radius:8px 8px 0 0;">
                <h2 style="margin:0;font-size:18px;">{emoji} {setup}</h2>
                <h1 style="margin:4px 0 0;font-size:28px;">{ticker} — {direction}</h1>
              </div>
              <div style="background:#16213e;color:#eee;padding:16px 20px;">
                <p style="margin:0 0 12px;">{message}</p>
                {table_html}
              </div>
              <div style="background:#0f3460;color:#eee;padding:16px 20px;">
                <p style="margin:0 0 8px;"><strong style="color:#ffcc00;">Entry:</strong> {entry}</p>
                <p style="margin:0 0 8px;"><strong style="color:#ffcc00;">Exit:</strong> {exit_plan}</p>
                <p style="margin:0;"><strong style="color:#ffcc00;">Confidence:</strong> {confidence}</p>
              </div>
              <div style="background:#1a1a2e;color:#666;padding:12px 20px;border-radius:0 0 8px 8px;font-size:12px;text-align:center;">
                Setup Sniper — {datetime.now().strftime('%Y-%m-%d %H:%M ET')}
              </div>
            </div>
            """
            self.alert_mgr._send_email(
                subject=f"{emoji} {setup} — {ticker} ({direction})",
                html=html,
            )

    # ═══════════════════════════════════════════════════════════════════
    # HELPERS
    # ═══════════════════════════════════════════════════════════════════

    def _get_tickers(self) -> list[str]:
        """Build the ticker universe: core + custom + Phase 1 watchlist + Tier 2."""
        tickers = list(self.ic.all_core_tickers)

        if self.ic.include_phase1_watchlist:
            # Load today's Phase 1 watchlist candidates
            for wl_file in [
                "watchlist.json",
                "watchlist_earnings_miss.json",
                "watchlist_exhaustion_gap.json",
                "watchlist_ribbon_break.json",
            ]:
                wl_path = Path(self.config.watchlist_file).parent / wl_file
                if wl_path.exists():
                    try:
                        data = json.loads(wl_path.read_text())
                        rows = data
                        if isinstance(data, dict):
                            rows = data.get("runners") or data.get("names") or []
                        for c in rows:
                            t = c.get("ticker", "")
                            if t and t not in tickers:
                                tickers.append(t)
                    except Exception:
                        pass

        try:
            for t in self.universe_builder.get_tier2_candidates(refresh=False):
                if t and t not in tickers:
                    tickers.append(t)
        except Exception:
            pass

        return tickers

    def _get_daily_atr(self, ticker: str) -> Optional[float]:
        """Get daily ATR for a ticker (cached per session)."""
        if ticker in self._daily_atr_cache:
            return self._daily_atr_cache[ticker]

        try:
            bars = self.client.get_daily_bars(ticker, days_back=20)
            if bars and len(bars) >= 15:
                highs = np.array([b["h"] for b in bars], dtype=float)
                lows = np.array([b["l"] for b in bars], dtype=float)
                closes = np.array([b["c"] for b in bars], dtype=float)
                atr_vals = atr(highs, lows, closes, 14)
                val = float(atr_vals[-1]) if not np.isnan(atr_vals[-1]) else None
                self._daily_atr_cache[ticker] = val
                return val
        except Exception:
            pass

        self._daily_atr_cache[ticker] = None
        return None

    def _new_session(self, today: str):
        """Reset everything for a new trading day."""
        logger.info(f"═══ NEW SESSION: {today} ═══")
        self._session_date = today
        self._universe_poll_cycle = 0
        self._daily_atr_cache.clear()
        self._bar_counts.clear()
        # Phase 2
        self.orl_scanner.reset_session()
        self.vix_monitor.reset_session()
        # Phase 3
        self.dead_cat_scanner.reset_session()
        self.gap_reversal_scanner.reset_session()
        self.ribbon_scanner.exec_engine.reset_session()
        self._ribbon_primed.clear()
        self._ribbon_bar_counts.clear()

    def _is_market_hours(self, current_time: str) -> bool:
        """Check if current time is within scan window."""
        return self.ic.scan_start <= current_time <= self.ic.scan_end

    def _build_entry_email(self, alert: IntradayAlert, table: str) -> str:
        """HTML email for entry signals."""
        exit_text = alert.exit_plan
        if alert.stop_was_widened and alert.floor_source:
            exit_text = f"{alert.exit_plan} — {alert.floor_source}"
        return f"""
        <div style="font-family:-apple-system,sans-serif;max-width:500px;margin:0 auto;">
          <div style="background:#1a1a2e;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0;">
            <h2 style="margin:0;font-size:18px;">🔴 ENTRY SIGNAL</h2>
            <h1 style="margin:4px 0 0;font-size:28px;color:#ff4444;">{alert.ticker} — SHORT</h1>
          </div>
          <div style="background:#16213e;color:#eee;padding:16px 20px;">
            <p style="margin:0 0 12px;color:#ffcc00;">{alert.message}</p>
            <pre style="background:#0f3460;padding:12px;border-radius:4px;font-size:13px;color:#eee;">{table}</pre>
          </div>
          <div style="background:#0f3460;color:#eee;padding:16px 20px;">
            <p style="margin:0 0 8px;"><strong style="color:#ffcc00;">Exit:</strong> {exit_text}</p>
            <p style="margin:0;"><strong style="color:#ffcc00;">Confidence:</strong> {alert.confidence}</p>
          </div>
          <div style="background:#1a1a2e;color:#666;padding:12px 20px;border-radius:0 0 8px 8px;font-size:12px;text-align:center;">
            Setup Sniper — {datetime.now().strftime('%Y-%m-%d %H:%M ET')}
          </div>
        </div>
        """
