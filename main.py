#!/usr/bin/env python3
"""
Setup Sniper — Trading Scanner
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Usage:
  python main.py nightly           # Run after market close (4:30 PM ET+)
  python main.py morning           # Run premarket (8:00 - 9:15 AM ET)
  python main.py morning-fresh     # 8:30 AM — premarket discovery (not on nightly list)
  python main.py catalyst-backfill # Replay Alpaca news vs catalyst_events.json
  python main.py intraday          # Run during market hours (9:25 AM - 4:05 PM ET)
  python main.py intraday --once   # Single poll cycle (for testing)
  python main.py regime            # Classify market regime → data/regime_today.json
  python main.py full              # Run nightly + morning (for dev)
  python main.py test              # Dry run to verify setup

Environment:
  MASSIVE_API_KEY=your_key         # Required
  APCA_API_KEY_ID=your_key         # Optional — Alpaca news (Setup 11 gate)
  APCA_API_SECRET_KEY=your_secret  # Optional — Alpaca news
  DISCORD_WEBHOOK_URL=url          # Optional
  SLACK_WEBHOOK_URL=url            # Optional
  INTRADAY_TICKERS=AAPL,TSLA       # Optional — extra tickers for Setup 8
"""
from dotenv import load_dotenv
load_dotenv()
import json
import os
import sys
import logging
from datetime import datetime
from typing import Optional

from config import ScannerConfig
from polygon_client import PolygonClient
from universe_builder import UniverseBuilder
from scanner_runner_fade import RunnerFadeScanner
from scanner_earnings_miss import EarningsMissScanner
from scanner_exhaustion_gap import ExhaustionGapScanner
from alerts import AlertManager
from alert_consolidator import AlertConsolidator
from catalyst_briefing import CatalystBriefing

# ─── Logging setup ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-5s │ %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def _make_consolidating_intraday_engine(config: ScannerConfig):
    from scanner_intraday import IntradayEngine

    class _ConsolidatingIntradayEngine(IntradayEngine):
        def __init__(self, cfg: ScannerConfig):
            super().__init__(cfg)
            self.alert_mgr.consolidator = AlertConsolidator(self.universe_builder)

        def _dispatch_vix_alert(self, alert):
            def deliver(a):
                super(_ConsolidatingIntradayEngine, self)._dispatch_vix_alert(a)

            self.alert_mgr.send_alerts_batch([alert], deliver_fn=deliver)

        def _dispatch_phase3_alert(self, alert):
            def deliver(a):
                super(_ConsolidatingIntradayEngine, self)._dispatch_phase3_alert(a)

            self.alert_mgr.send_alerts_batch([alert], deliver_fn=deliver)

        def _dispatch_intraday_alert(self, alert, current_time: str):
            if current_time < "09:45" and alert.stage_number < 4:
                return

            def deliver(a):
                super(_ConsolidatingIntradayEngine, self)._dispatch_intraday_alert(
                    a, current_time
                )

            self.alert_mgr.send_alerts_batch([alert], deliver_fn=deliver)

    return _ConsolidatingIntradayEngine(config)


BANNER = """
==================================================
              SETUP SNIPER
      Phase 1: Nightly + Morning Scans
      Phase 2: Intraday + VIX Monitor
      "Bread and butter trades"
=================================================="""


def run_nightly(config: ScannerConfig):
    """Nightly scans — run all setups that have a nightly phase."""
    briefing = None
    if config.catalyst.available:
        briefing = CatalystBriefing(
            polygon_api_key=config.api.api_key,
            fmp_api_key=config.fmp.api_key,
            anthropic_api_key=config.catalyst.anthropic_api_key,
            model=config.catalyst.model,
        )

    polygon_client = PolygonClient(config.api)
    universe_builder = UniverseBuilder(polygon_client, config.universe)
    universe = universe_builder.build_universe(force=True)
    logger.info(f"Scan universe: {len(universe)} tickers")

    alert_mgr = AlertManager(
        config.alerts,
        briefing=briefing,
        universe_builder=universe_builder,
        universe_top_n=config.universe.top_n,
    )

    # Setup 6: Prior Day Runner Fade + universe merge (catches non-top-20 gainers)
    universe_runners = universe_builder.screen_runners(
        min_pct=config.runner_fade.min_pct_change
    )
    runner_scanner = RunnerFadeScanner(config)
    runner_candidates = runner_scanner.run_nightly_scan()
    existing_runners = {c.ticker for c in runner_candidates}
    missed = [r for r in universe_runners if r["ticker"] not in existing_runners]
    if missed:
        tickers_m = [r["ticker"] for r in missed]
        logger.info(
            f"  🎯 Universe caught {len(missed)} additional runners "
            f"not in top gainers list: {tickers_m}"
        )
        extra = runner_scanner.deep_validate_fast_candidates(missed)
        if extra:
            by_ticker = {c.ticker: c for c in runner_candidates}
            for c in extra:
                by_ticker[c.ticker] = c
            runner_candidates = list(by_ticker.values())
            runner_scanner._save_watchlist(runner_candidates)

    alert_mgr.send_nightly_summary(runner_candidates)
    if not runner_candidates:
        logger.info("Setup 6: No runners qualified. Clean watchlist for tomorrow.")

    # Setup 3: Earnings Miss Short
    earnings_scanner = EarningsMissScanner(config)
    earnings_candidates = earnings_scanner.run_nightly_scan()
    if earnings_candidates:
        alert_mgr.send_nightly_summary(earnings_candidates)

    # Setup 7: Exhaustion Gap Short + universe-backed gap-ups
    gap_scanner = ExhaustionGapScanner(config)
    gap_candidates = gap_scanner.run_nightly_scan()

    scan_day = datetime.now().strftime("%Y-%m-%d")
    existing_gap = {c.ticker for c in gap_candidates}
    extras: list = []
    for g in universe_builder.screen_exhaustion_candidates(min_change_pct=10.0):
        t = g.get("ticker")
        if not t or t in existing_gap:
            continue
        c = gap_scanner.try_build_from_snapshot_gainer(g, scan_day)
        if c:
            extras.append(c)
            existing_gap.add(t)
    if extras:
        logger.info(f"  🎯 Universe added {len(extras)} exhaustion-gap candidates")
        gap_scanner._save_watchlist(extras)
        gap_candidates = gap_candidates + extras

    if gap_candidates:
        alert_mgr.send_nightly_summary(gap_candidates)

    return {
        "runner_fade": runner_candidates,
        "earnings_miss": earnings_candidates,
        "exhaustion_gap": gap_candidates,
    }


def run_regime(config: ScannerConfig) -> None:
    from regime_runner import run_regime as _run_regime

    _run_regime(config)


def run_morning(config: ScannerConfig):
    """Morning premarket checks — evaluate all setup watchlists."""
    briefing = None
    if config.catalyst.available:
        briefing = CatalystBriefing(
            polygon_api_key=config.api.api_key,
            fmp_api_key=config.fmp.api_key,
            anthropic_api_key=config.catalyst.anthropic_api_key,
            model=config.catalyst.model,
        )

    polygon_client = PolygonClient(config.api)
    universe_builder = UniverseBuilder(polygon_client, config.universe)
    universe_builder.build_universe(force=False)
    tier2 = universe_builder.get_tier2_candidates(refresh=False)
    logger.info(f"Tier 2 deep-scan candidates: {len(tier2)} tickers")

    consolidator = AlertConsolidator(universe_builder)
    alert_mgr = AlertManager(
        config.alerts,
        briefing=briefing,
        universe_builder=universe_builder,
        universe_top_n=config.universe.top_n,
        consolidator=consolidator,
    )

    total_alerts = []

    # Setup 6
    runner_scanner = RunnerFadeScanner(config)
    runner_alerts = runner_scanner.run_morning_check()
    total_alerts.extend(runner_alerts)

    # Setup 3
    earnings_scanner = EarningsMissScanner(config)
    earnings_alerts = earnings_scanner.run_morning_check()
    total_alerts.extend(earnings_alerts)

    # Setup 7
    gap_scanner = ExhaustionGapScanner(config)
    gap_alerts = gap_scanner.run_morning_check()
    total_alerts.extend(gap_alerts)

    alert_mgr.send_alerts_batch(total_alerts)

    if not total_alerts:
        logger.info("No setups triggered this morning. Patience is edge.")
        return total_alerts

    logger.info(f"\n{'=' * 40}")
    logger.info(f"  {len(total_alerts)} ALERT(S) FIRED")
    logger.info(f"  Tickers: {', '.join(a.ticker for a in total_alerts)}")
    logger.info(f"  Remember: Trade the A+ setups. Cover by 10:00 AM where applicable.")
    logger.info(f"{'=' * 40}\n")
    return total_alerts


def _load_phase1_excluded_tickers(config: ScannerConfig) -> list[str]:
    """
    Tickers already covered by Phase 1 (runner, earnings-miss, exhaustion watchlists).
    Morning-fresh skips these so alerts are not duplicated.
    """
    tickers: set[str] = set()
    for path in (
        config.watchlist_file,
        config.earnings_watchlist_file,
        config.exhaustion_watchlist_file,
    ):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"  Could not read watchlist {path}: {e}")
            continue
        if isinstance(data, dict) and "runners" in data:
            for c in data.get("runners", []):
                t = c.get("ticker")
                if t:
                    tickers.add(str(t).upper())
        elif isinstance(data, list):
            for c in data:
                if isinstance(c, dict):
                    t = c.get("ticker")
                    if t:
                        tickers.add(str(t).upper())
    return list(tickers)


def run_morning_fresh(config: ScannerConfig) -> None:
    """
    8:30 AM ET — Fresh premarket discovery scan.
    Finds new setups NOT on the nightly / Phase 1 watchlists.
    Runs independently alongside the existing 8:00/8:45 AM morning checks.
    """
    import requests
    from scanner_premarket_fresh import PremktFreshScanner
    from alerts_morning_fresh import (
        build_discord_morning_fresh,
        build_slack_morning_fresh,
        build_console_morning_fresh,
    )

    logger.info("─" * 60)
    logger.info("8:30 AM MORNING FRESH SCAN")

    excluded = _load_phase1_excluded_tickers(config)
    logger.info(f"  Excluding {len(excluded)} ticker(s) from Phase 1 watchlists")

    scanner = PremktFreshScanner(config)
    result = scanner.run(nightly_tickers=excluded)

    if config.alerts.console_output:
        print(build_console_morning_fresh(result))

    if config.alerts.discord_webhook_url:
        for payload in build_discord_morning_fresh(result):
            try:
                r = requests.post(
                    config.alerts.discord_webhook_url,
                    json=payload,
                    timeout=5,
                )
                if r.status_code not in (200, 204):
                    logger.warning(f"Discord returned {r.status_code}: {r.text[:100]}")
            except Exception as e:
                logger.error(f"Discord delivery failed: {e}")

    if config.alerts.slack_webhook_url:
        try:
            r = requests.post(
                config.alerts.slack_webhook_url,
                json=build_slack_morning_fresh(result),
                timeout=5,
            )
            if r.status_code not in (200, 204):
                logger.warning(f"Slack returned {r.status_code}: {r.text[:100]}")
        except Exception as e:
            logger.error(f"Slack delivery failed: {e}")

    movers = result.get("movers", [])
    logger.info(f"8:30 AM scan complete — {len(movers)} new mover(s) sent")


def _fmt_latency(sec: Optional[float]) -> str:
    if sec is None:
        return "—"
    sign = "-" if sec < 0 else ""
    sec = abs(sec)
    if sec < 120:
        return f"{sign}{sec:.0f}s"
    return f"{sign}{sec / 60:.1f}m"


def run_catalyst_backfill(config: ScannerConfig) -> None:
    """Replay Alpaca REST news against catalyst_events.json. Requires paper keys."""
    import asyncio

    from catalyst_news import AlpacaNewsFeed, backfill_events, load_catalyst_events

    if not config.alpaca_news.available:
        logger.warning(
            "catalyst-backfill skipped: set APCA_API_KEY_ID and APCA_API_SECRET_KEY "
            "(free Alpaca paper key, no funding). Massive news cannot replay history."
        )
        return

    events = load_catalyst_events()
    if not events:
        logger.error("catalyst_events.json missing or empty")
        return

    logger.info("Replaying Alpaca news for %d logged catalyst events…", len(events))
    feed = AlpacaNewsFeed(config.alpaca_news.key_id, config.alpaca_news.secret_key)
    rows = asyncio.run(
        backfill_events(feed, events, lookback_hours=config.setup11.lookback_hours)
    )

    print("")
    print(
        f"{'Ticker':<8} {'Date':<12} {'Heads':>6} {'Conf':>5} "
        f"{'Latency':>10} {'Clears 9:45':>12}  First confirming headline"
    )
    print("─" * 100)
    for row in rows:
        headline = (row.get("first_headline") or "—")[:56]
        print(
            f"{row['ticker']:<8} {row['event_date']:<12} {row['headlines']:>6} "
            f"{row['confirming']:>5} {_fmt_latency(row.get('latency_sec')):>10} "
            f"{'yes' if row.get('clears_0945') else 'no':>12}  {headline}"
        )
    print("")
    logger.info("catalyst-backfill complete — %d event(s)", len(rows))


def run_test(config: ScannerConfig):
    """Quick smoke test: verify API connectivity and config."""
    from polygon_client import PolygonClient

    logger.info("Running connectivity test...")
    client = PolygonClient(config.api)

    # Test 1: API key works
    logger.info("  1. Testing API key with SPY snapshot...")
    snap = client.get_snapshot("SPY")
    if snap:
        day = snap.get("day", {})
        logger.info(f"     ✓ SPY last price: ${day.get('c', 'N/A')}")
    else:
        logger.error("     ✗ Failed to fetch SPY snapshot. Check API key.")
        return

    # Test 2: Gainers endpoint
    logger.info("  2. Testing gainers endpoint...")
    gainers = client.get_gainers()
    logger.info(f"     ✓ Got {len(gainers)} gainers")
    if gainers:
        top = gainers[0]
        logger.info(f"     Top gainer: {top.get('ticker')} ({top.get('todaysChangePerc', 0):.1f}%)")

    # Test 3: Aggs endpoint
    logger.info("  3. Testing daily bars (AAPL, last 5 days)...")
    bars = client.get_daily_bars("AAPL", days_back=5)
    logger.info(f"     ✓ Got {len(bars)} daily bars for AAPL")

    # Test 4: Indicator computation
    logger.info("  4. Testing indicator math...")
    import numpy as np
    from indicators import ema, supertrend, atr
    test_closes = np.array(
        [100, 102, 101, 103, 105, 104, 106, 108, 107, 109, 110, 112, 113, 115, 114, 116, 118, 117],
        dtype=float,
    )
    test_highs = test_closes + 1
    test_lows = test_closes - 1
    ema_9 = ema(test_closes, 9)
    atr_14 = atr(test_highs, test_lows, test_closes, 14)
    logger.info(f"     ✓ EMA-9 last value: {ema_9[-1]:.2f}")

    logger.info("  5. Testing Alpaca news entitlement...")
    if not config.alpaca_news.available:
        logger.info(
            "     — APCA_API_KEY_ID / APCA_API_SECRET_KEY not set "
            "(news gate will emit unconfirmed)"
        )
    else:
        import requests

        try:
            resp = requests.get(
                "https://data.alpaca.markets/v1beta1/news",
                params={"limit": 1},
                headers={
                    "APCA-API-KEY-ID": config.alpaca_news.key_id,
                    "APCA-API-SECRET-KEY": config.alpaca_news.secret_key,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info("     ✓ Alpaca news REST authorized")
            elif resp.status_code in (401, 403):
                logger.error(
                    "     ✗ Alpaca rejected this key for news (HTTP %s). "
                    "Verify paper/live entitlement on the Alpaca dashboard.",
                    resp.status_code,
                )
            else:
                logger.warning("     ? Alpaca news HTTP %s", resp.status_code)
        except Exception as e:
            logger.warning("     ? Alpaca news probe failed: %s", e)

    logger.info("\n  All tests passed. You're good to go.\n")


def main():
    print(BANNER)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1].lower()

    try:
        config = ScannerConfig()
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    now = datetime.now()
    logger.info(f"Current time: {now.strftime('%Y-%m-%d %H:%M:%S ET')}")

    if command == "nightly":
        run_nightly(config)
    elif command == "morning":
        run_morning(config)
    elif command == "morning-fresh":
        run_morning_fresh(config)
    elif command == "intraday":
        engine = _make_consolidating_intraday_engine(config)
        single = "--once" in sys.argv
        engine.run(single_cycle=single)
    elif command == "catalyst-backfill":
        run_catalyst_backfill(config)
    elif command == "full":
        logger.info("Running full cycle (nightly → morning)...")
        run_nightly(config)
        print("\n" + "─" * 40 + "\n")
        run_morning(config)
    elif command == "regime":
        run_regime(config)
    elif command == "test":
        run_test(config)
    else:
        print(f"Unknown command: {command}")
        print(
            "Valid commands: nightly, morning, morning-fresh, catalyst-backfill, "
            "intraday, regime, full, test"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
