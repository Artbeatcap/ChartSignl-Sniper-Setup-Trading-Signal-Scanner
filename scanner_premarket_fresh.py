"""
scanner_premarket_fresh.py
─────────────────────────────────────────────────────────────────────────
8:30 AM ET — Fresh Premarket Discovery Scan

PURPOSE:
  The nightly scan (4:35 PM) finds prior-day runners. This scanner finds
  stocks that *started moving overnight/premarket* — earnings reporters,
  news-driven gaps, and big premarket movers that weren't on last night's
  watchlist.

  Runs independently. Does NOT touch the nightly watchlist file.

SETUPS IT HUNTS:
  - Setup 3: Earnings Miss Short    → gap down on miss, below 9 EMA in PM
  - Setup 4: Earnings Beat Fade     → gap up near ATH on beat, failing to hold
  - Setup 5: News Gap Reversal      → gap up into resistance in bear market
  - Setup 9: Dead Cat Bounce        → 20%+ gap down past all levels
  - Market Bias                     → SPY/QQQ gap direction + UVXY (VIX proxy)
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import requests

from config import ScannerConfig
from indicators import atr as atr_series, ema as ema_series

logger = logging.getLogger(__name__)

# ─── Minimum price filter — skip penny stocks / illiquid garbage ──────
MIN_PRICE = 5.0
MIN_AVG_VOLUME = 500_000   # shares/day average
MIN_PM_VOLUME = 50_000    # shares traded so far in premarket

# ─── Setup classification thresholds ─────────────────────────────────
EARNINGS_MISS_GAP_PCT = -5.0   # -5% or worse
EARNINGS_BEAT_GAP_PCT = 5.0   # +5% or more
NEWS_GAP_UP_PCT = 4.0   # +4% news-driven gap
DEAD_CAT_GAP_PCT = -18.0  # -18% = potential dead cat candidate
ATH_PROXIMITY_PCT = 3.0   # within 3% of ATH = "near ATH"
ROUND_NUMBER_PROXIMITY = 2.0   # within 2% of round number
SIGNIFICANT_MOVE_PCT = 3.0   # minimum gap to appear on watchlist at all


def _last_valid_scalar(arr: np.ndarray) -> Optional[float]:
    valid = arr[~np.isnan(arr)]
    if valid.size == 0:
        return None
    return float(valid[-1])


def _ema_last_scalar(closes: List[float], period: int) -> Optional[float]:
    """Last EMA value; `period` must be <= len(closes) for indicators.ema."""
    arr = np.array(closes, dtype=float)
    if len(arr) < period:
        return None
    out = ema_series(arr, period)
    return _last_valid_scalar(out)


def _atr_last_scalar(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    period: int = 14,
) -> Optional[float]:
    h = np.array(highs, dtype=float)
    l = np.array(lows, dtype=float)
    c = np.array(closes, dtype=float)
    out = atr_series(h, l, c, period)
    return _last_valid_scalar(out)


@dataclass
class PremktMover:
    """A premarket mover with all computed context."""
    ticker: str
    pm_price: float          # current premarket price
    prev_close: float        # yesterday's close
    gap_pct: float           # (pm_price / prev_close - 1) * 100
    pm_high: float
    pm_low: float
    pm_volume: int
    avg_daily_volume: float

    # Computed indicators (filled during enrichment)
    ema_9_pm: Optional[float] = None        # 9 EMA on 5-min PM bars
    ema_200_daily: Optional[float] = None
    daily_atr: Optional[float] = None
    all_time_high: Optional[float] = None
    fifty_two_week_high: Optional[float] = None
    fifty_two_week_low: Optional[float] = None

    # Earnings context (filled if reporting today/yesterday)
    has_earnings: bool = False
    earnings_surprise_pct: Optional[float] = None   # positive = beat
    eps_beat: bool = False
    rev_beat: bool = False

    # Classification output
    setup_tags: List[str] = field(default_factory=list)
    direction: str = ""        # "SHORT" or "LONG"
    confidence: str = ""       # "HIGH" / "MEDIUM"
    entry_note: str = ""
    exit_note: str = ""
    warning: str = ""          # behavioral guardrail notes
    news_gate_tier: str = ""
    news_gate_reason: str = ""

    @property
    def relative_pm_volume(self) -> float:
        """PM volume so far vs daily average — 0.3 means 30% of avg already in PM."""
        if self.avg_daily_volume > 0:
            return self.pm_volume / self.avg_daily_volume
        return 0.0

    @property
    def near_ath(self) -> bool:
        if self.all_time_high and self.all_time_high > 0:
            return abs(self.pm_high / self.all_time_high - 1) * 100 <= ATH_PROXIMITY_PCT
        return False

    @property
    def near_round_number(self) -> Optional[float]:
        """Returns the round number if PM price is within 2% of one, else None."""
        for level in [2000, 1500, 1000, 750, 500, 400, 300, 250, 200, 150,
                      100, 75, 50, 40, 30, 25, 20, 15, 10]:
            if self.pm_price > 0 and abs(self.pm_high / level - 1) * 100 <= ROUND_NUMBER_PROXIMITY:
                return float(level)
        return None

    @property
    def below_52w_low(self) -> bool:
        if self.fifty_two_week_low and self.fifty_two_week_low > 0:
            return self.pm_price < self.fifty_two_week_low
        return False


class PremktFreshScanner:
    """
    Scans premarket movers not already on the nightly watchlist.
    Designed to run at 8:30 AM ET — premarket has been active for ~4 hours.
    """

    def __init__(self, config: ScannerConfig):
        self.cfg = config
        self.api_key = config.api.api_key
        self.base_url = config.api.base_url
        self.session = requests.Session()
        # Match polygon_client.py (apiKey query param — same as rest of Setup Sniper)
        self.session.params = {"apiKey": self.api_key}  # type: ignore
        self.session.headers.update({"User-Agent": "SetupSniper/1.0"})
        self._rate_limit_delay = 60.0 / max(config.api.rate_limit_per_min, 1)

    # ─── API helpers ──────────────────────────────────────────────────

    def _get(self, path: str, params: Optional[Dict] = None) -> Optional[Dict]:
        url = f"{self.base_url}{path}"
        merged = {**(params or {})}
        try:
            r = self.session.get(url, params=merged, timeout=self.cfg.api.timeout_seconds)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            logger.error(f"API error {path}: {e}")
            return None

    def _throttle(self):
        time.sleep(self._rate_limit_delay)

    def _snapshot_ticker(self, symbol: str) -> Optional[Dict]:
        """Full snapshot JSON for one symbol (standard US stocks path)."""
        data = self._get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}")
        if data and data.get("ticker"):
            return data
        return None

    # ─── Step 1: Get premarket movers ─────────────────────────────────

    def fetch_premarket_movers(self, nightly_tickers: List[str]) -> List[PremktMover]:
        """
        Pull top premarket gainers and losers. Filter out:
        - Already on nightly watchlist (they're covered)
        - Penny stocks (< $5)
        - Low premarket volume
        """
        movers: List[PremktMover] = []
        excluded = {t.upper() for t in nightly_tickers if t}

        for direction in ["gainers", "losers"]:
            data = self._get(
                f"/v2/snapshot/locale/us/markets/stocks/{direction}",
                {"include_otc": "false"},
            )
            self._throttle()

            if not data or "tickers" not in data:
                logger.warning(f"No {direction} data returned")
                continue

            for t in data["tickers"]:
                ticker = t.get("ticker", "")
                if not ticker:
                    continue

                if ticker.upper() in excluded:
                    logger.debug(f"  Skip {ticker} — already on nightly watchlist")
                    continue

                day = t.get("day", {})
                prev_day = t.get("prevDay", {})

                pm_price = t.get("lastTrade", {}).get("p") or day.get("c") or 0
                prev_close = prev_day.get("c") or 0

                if pm_price < MIN_PRICE or prev_close <= 0:
                    continue

                gap_pct = (pm_price / prev_close - 1) * 100
                pm_volume = day.get("v") or 0

                if abs(gap_pct) < SIGNIFICANT_MOVE_PCT:
                    continue

                if pm_volume < MIN_PM_VOLUME:
                    continue

                avg_vol = prev_day.get("v") or 0
                if avg_vol < MIN_AVG_VOLUME and avg_vol > 0:
                    continue

                movers.append(PremktMover(
                    ticker=ticker,
                    pm_price=pm_price,
                    prev_close=prev_close,
                    gap_pct=gap_pct,
                    pm_high=day.get("h") or pm_price,
                    pm_low=day.get("l") or pm_price,
                    pm_volume=int(pm_volume),
                    avg_daily_volume=float(avg_vol),
                ))

        movers.sort(key=lambda m: abs(m.gap_pct), reverse=True)
        logger.info(f"Premarket movers found: {len(movers)} (after filters)")
        return movers

    def _enrich_earnings_fmp(self, mover: PremktMover) -> None:
        """Set has_earnings / beat flags from FMP calendar when API key is present."""
        if not self.cfg.fmp.enabled:
            return
        from fmp_client import FmpClient

        fmp = FmpClient(self.cfg.fmp.api_key, self.cfg.fmp.timeout_seconds)
        today = date.today()
        start = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")
        rows = fmp.get_earnings_rows_for_symbol(mover.ticker, start, end)
        if not rows:
            return

        row = rows[-1]
        mover.has_earnings = True

        eps = row.get("eps")
        eps_est = row.get("epsEstimated")
        rev = row.get("revenue")
        rev_est = row.get("revenueEstimated")

        eps_sp = None
        rev_sp = None
        if eps is not None and eps_est not in (None, 0):
            try:
                eps_sp = (float(eps) - float(eps_est)) / float(eps_est) * 100.0
            except (ValueError, ZeroDivisionError):
                pass
        if rev is not None and rev_est not in (None, 0):
            try:
                rev_sp = (float(rev) - float(rev_est)) / float(rev_est) * 100.0
            except (ValueError, ZeroDivisionError):
                pass

        if eps_sp is not None:
            mover.eps_beat = eps_sp > 0
        if rev_sp is not None:
            mover.rev_beat = rev_sp > 0

        if eps_sp is not None and rev_sp is not None:
            mover.earnings_surprise_pct = (eps_sp + rev_sp) / 2.0
        elif eps_sp is not None:
            mover.earnings_surprise_pct = eps_sp
        elif rev_sp is not None:
            mover.earnings_surprise_pct = rev_sp

    # ─── Step 2: Enrich with indicators and context ───────────────────

    def enrich_mover(self, mover: PremktMover) -> PremktMover:
        """Pull daily bars + premarket 5-min bars and compute indicators."""
        today_str = date.today().strftime("%Y-%m-%d")
        lookback = (date.today() - timedelta(days=60)).strftime("%Y-%m-%d")

        daily = self._get(
            f"/v2/aggs/ticker/{mover.ticker}/range/1/day/{lookback}/{today_str}",
            {"adjusted": "true", "sort": "asc", "limit": 60},
        )
        self._throttle()

        if daily and daily.get("resultsCount", 0) >= 20:
            bars = daily["results"]
            closes = [b["c"] for b in bars]
            highs = [b["h"] for b in bars]
            lows = [b["l"] for b in bars]

            n = len(closes)
            period_ema = 200 if n >= 200 else n
            mover.ema_200_daily = _ema_last_scalar(closes, period_ema)

            mover.daily_atr = _atr_last_scalar(highs, lows, closes, 14)
            mover.fifty_two_week_high = max(highs[-252:]) if len(highs) >= 252 else max(highs)
            mover.fifty_two_week_low = min(lows[-252:]) if len(lows) >= 252 else min(lows)
            mover.all_time_high = max(highs)

        pm_bars_data = self._get(
            f"/v2/aggs/ticker/{mover.ticker}/range/5/minute/{today_str}/{today_str}",
            {"adjusted": "true", "sort": "asc", "limit": 100, "extended_hours": "true"},
        )
        self._throttle()

        if pm_bars_data and pm_bars_data.get("resultsCount", 0) >= 3:
            pm_bars = pm_bars_data["results"]
            pm_closes = [b["c"] for b in pm_bars]
            if len(pm_closes) >= 9:
                mover.ema_9_pm = _ema_last_scalar(pm_closes, 9)
            elif pm_closes:
                mover.ema_9_pm = sum(pm_closes[-3:]) / len(pm_closes[-3:])

        self._enrich_earnings_fmp(mover)
        return mover

    # ─── Step 3: Classify into setups ────────────────────────────────

    def classify_mover(self, mover: PremktMover) -> PremktMover:
        """
        Tag each mover with the matching setup(s) from the playbook.
        Priority order: earnings setups first (more defined edge), then generic gap plays.
        """
        tags = []
        warnings = []

        if mover.has_earnings and mover.gap_pct <= EARNINGS_MISS_GAP_PCT:
            double_miss = not mover.eps_beat and not mover.rev_beat
            label = "SETUP 3: Earnings Double Miss Short" if double_miss else "SETUP 3: Earnings Miss Short"
            tags.append(label)
            mover.direction = "SHORT"
            mover.confidence = "HIGH" if double_miss else "MEDIUM"
            below_9ema = mover.ema_9_pm and mover.pm_price < mover.ema_9_pm
            if below_9ema:
                mover.entry_note = (
                    "Below 9 EMA in PM [ok] - Short on 9 EMA rejection after first 5-min bar, "
                    "or sell call spread at open"
                )
            else:
                mover.entry_note = "Not yet below 9 EMA - WAIT. Only enter if PM stays below 9 EMA through open"
                warnings.append("Price not below 9 EMA yet - patience required (Rule 1)")
            mover.exit_note = "Cover at PM lows or 10:00 AM | Use shares/spreads - IV crushed on long puts"
            warnings.append("Options IV crushed after earnings - use shares short or spreads, not long puts")

        elif mover.has_earnings and mover.gap_pct >= EARNINGS_BEAT_GAP_PCT:
            near_wall = mover.near_ath or mover.near_round_number is not None
            if near_wall:
                wall_desc = "near ATH" if mover.near_ath else f"near ${mover.near_round_number:.0f} round number"
                tags.append(f"SETUP 4: Earnings Beat Fade at Wall ({wall_desc})")
                mover.direction = "SHORT"
                mover.confidence = "HIGH" if mover.near_ath else "MEDIUM"
                failing = mover.pm_price < mover.pm_high * 0.99
                if failing:
                    mover.entry_note = (
                        "PM failing to hold high [ok] - Short on break below 9 EMA + 65 EMA at open, or fade PM high"
                    )
                else:
                    mover.entry_note = "Still holding PM high - WAIT for failure to hold before entering"
                    warnings.append("Price still holding PM high - wait for rejection confirmation")
                mover.exit_note = "Cover at prior support | Trail below 9 EMA"

        if mover.gap_pct <= DEAD_CAT_GAP_PCT:
            tags.append("SETUP 9: Dead Cat Bounce Candidate (Long)")
            if not mover.direction:
                mover.direction = "LONG (Day Trade Only)"
                mover.confidence = "MEDIUM"
                mover.entry_note = "Wait for 9 EMA cross UP between 9:35-9:40 AM - bounce to first resistance"
                mover.exit_note = "Sell at nearest major resistance. DAY TRADE ONLY - most gap lower Day 2"
            warnings.append("HIGH RISK - Day 1 bounce only. Most gap lower Day 2 in AH/PM")

        if not mover.has_earnings and mover.gap_pct >= NEWS_GAP_UP_PCT:
            tags.append("SETUP 5: News Gap Up - Watch for Bear Market Reversal")
            if not mover.direction:
                mover.direction = "SHORT (conditional)"
                mover.confidence = "MEDIUM"
                mover.entry_note = (
                    "Only short if SPY is in bearish regime (below daily 9 EMA) AND first candle is red"
                )
                mover.exit_note = "Cover at prior support / VWAP reclaim"
            warnings.append("Setup 5 only valid in bearish SPY regime - check SPY daily 9 EMA before trading")

        if not tags and mover.gap_pct <= -8.0:
            tags.append("LARGE GAP DOWN - Unclassified (check for news/earnings)")
            mover.direction = "WATCH"
            mover.confidence = "LOW"
            mover.entry_note = "Identify catalyst first - no entry without a clear setup"
            warnings.append("No setup tag - identify the catalyst before trading")

        if not tags and mover.gap_pct >= 8.0:
            tags.append("LARGE GAP UP - Unclassified (check for news/catalyst)")
            mover.direction = "WATCH"
            mover.confidence = "LOW"
            mover.entry_note = "Identify catalyst first - news-driven gaps need Setup 5 confirmation"
            warnings.append("No setup tag - check if newsworthy or just noise")

        if not tags:
            tags.append(f"PREMARKET MOVER {mover.gap_pct:+.1f}%")
            mover.direction = "WATCH"
            mover.entry_note = "No setup match - monitor for setup development at open"

        mover.setup_tags = tags
        mover.warning = " | ".join(warnings) if warnings else ""
        return mover

    def _apply_catalyst_overlay(self, movers: List[PremktMover]) -> None:
        """One-shot news resolve for ≥20% gaps. Confirmed catalyst → do not fade."""
        min_gap = self.cfg.setup11.min_gap_pct
        watch = [m for m in movers if m.gap_pct >= min_gap]
        if not watch:
            return
        try:
            from catalyst_news import feeds_from_config, load_cached_cik_map, resolve_watchlist_sync
            from day1_catalyst_long import apply_news_overlay
            from polygon_client import PolygonClient
        except Exception as e:
            logger.warning("catalyst overlay import failed: %s", e)
            return

        session = requests.Session()
        session.headers.update({"User-Agent": "ChartSignl Setup Sniper admin@chartsignl.com"})
        cik_map: Dict[str, str] = {}
        try:
            cik_map = load_cached_cik_map(session)
        except Exception as e:
            logger.warning("CIK map unavailable: %s", e)

        polygon_client = None
        try:
            polygon_client = PolygonClient(self.cfg.api)
        except Exception as e:
            logger.warning("Massive news client unavailable: %s", e)

        feeds = feeds_from_config(
            self.cfg,
            session=session,
            polygon_client=polygon_client,
            cik_map=cik_map or None,
        )
        if not feeds:
            logger.info("  Catalyst overlay: no news feeds configured")
            return

        tickers = [m.ticker for m in watch]
        logger.info("  Catalyst overlay: resolving %s", ",".join(tickers))
        try:
            results = resolve_watchlist_sync(
                tickers, feeds, lookback_hours=self.cfg.setup11.lookback_hours,
            )
        except Exception as e:
            logger.warning("catalyst resolve failed: %s", e)
            return

        for mover in watch:
            gate = results.get(mover.ticker.upper())
            if gate is None:
                continue
            apply_news_overlay(mover, gate)
            logger.info("  %s news gate: %s — %s", mover.ticker, gate.tier, gate.reason)

    # ─── Step 4: Market bias (SPY + QQQ + UVXY) ───────────────────────

    def fetch_market_bias(self) -> Dict[str, Any]:
        """
        Quick SPY, QQQ, and volatility-proxy snapshot.

        I:VIX is omitted: Polygon index snapshots often 404/403 on lower tiers.
        UVXY (1.5x short-term VIX futures ETF) is used as a tradable proxy for
        implied-vol / fear positioning in the same alert header.
        """
        bias: Dict[str, Any] = {}

        for symbol in ["SPY", "QQQ", "UVXY"]:
            snap = self._snapshot_ticker(symbol)
            self._throttle()

            if not snap or "ticker" not in snap:
                continue

            t = snap["ticker"]
            day = t.get("day", {})
            prev = t.get("prevDay", {})
            pm_price = t.get("lastTrade", {}).get("p") or day.get("c") or 0
            prev_close = prev.get("c") or 0
            gap = ((pm_price / prev_close - 1) * 100) if prev_close > 0 else 0

            bias[symbol] = {
                "pm_price": pm_price,
                "prev_close": prev_close,
                "gap_pct": gap,
                "direction": "▲" if gap > 0 else "▼" if gap < 0 else "—",
            }

        spy = bias.get("SPY", {})
        uvxy = bias.get("UVXY", {})
        uvxy_gap = float(uvxy.get("gap_pct", 0) or 0) if uvxy else 0.0

        # UVXY gap > +5%: leveraged vol product ripping — treat like a VIX spike warning
        if uvxy_gap > 5.0:
            bias["regime"] = "VOLATILE OPEN"
        elif spy:
            if spy["gap_pct"] <= -1.5:
                bias["regime"] = "BEARISH OPEN"
            elif spy["gap_pct"] >= 1.5:
                bias["regime"] = "BULLISH OPEN"
            else:
                bias["regime"] = "NEUTRAL OPEN"
        else:
            bias["regime"] = "UNKNOWN"

        return bias

    # ─── Main entry point ─────────────────────────────────────────────

    def run(self, nightly_tickers: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Execute the full 8:30 AM premarket fresh scan.

        Returns dict with:
          - movers: List[PremktMover]  (classified, enriched)
          - market_bias: Dict          (SPY/QQQ/UVXY context)
          - scan_time: str
        """
        nightly_tickers = nightly_tickers or []
        logger.info("=" * 60)
        logger.info("8:30 AM PREMARKET FRESH SCAN — Starting")
        logger.info(f"  Excluding {len(nightly_tickers)} nightly watchlist ticker(s)")

        logger.info("  Fetching market bias (SPY/QQQ/UVXY)...")
        market_bias = self.fetch_market_bias()
        logger.info(f"  Market regime: {market_bias.get('regime', '?')}")

        logger.info("  Fetching premarket gainers/losers...")
        raw_movers = self.fetch_premarket_movers(nightly_tickers)
        logger.info(f"  Qualified movers: {len(raw_movers)}")

        enriched: List[PremktMover] = []
        for i, mover in enumerate(raw_movers[:20]):
            logger.info(
                f"  [{i + 1}/{min(len(raw_movers), 20)}] Enriching {mover.ticker} ({mover.gap_pct:+.1f}%)..."
            )
            try:
                mover = self.enrich_mover(mover)
                mover = self.classify_mover(mover)
                enriched.append(mover)
            except Exception as e:
                logger.error(f"  Error enriching {mover.ticker}: {e}")
                continue

        try:
            self._apply_catalyst_overlay(enriched)
        except Exception as e:
            logger.warning("catalyst overlay failed: %s", e)

        def priority_key(m: PremktMover) -> tuple:
            conf_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(m.confidence, 3)
            return (conf_rank, -abs(m.gap_pct))

        enriched.sort(key=priority_key)

        logger.info(f"8:30 AM SCAN COMPLETE — {len(enriched)} mover(s) classified")
        return {
            "movers": enriched,
            "market_bias": market_bias,
            "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M ET"),
        }
