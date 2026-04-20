"""
Universe Builder — top-N liquid US equities from Polygon all-tickers snapshot.

Caches ranked universe to disk for reuse across nightly / morning / intraday.
Tier 2 = subset of the universe with elevated session move, gap, or volume vs prior day.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from config import UniverseConfig
from polygon_client import PolygonClient

logger = logging.getLogger(__name__)


@dataclass
class UniverseTicker:
    """One name in the ranked scan universe (snapshot-derived)."""

    ticker: str
    rank: int
    dollar_volume: float
    close: float
    open_price: float
    prev_close: float
    volume: float
    high: float
    low: float
    pct_change: float
    gap_pct: float
    relative_volume: float = 0.0  # day.v / prevDay.v when prev vol > 0


class UniverseBuilder:
    """
    Build and cache the dollar-volume-ranked universe; screen + Tier 2 helpers.
    """

    def __init__(self, polygon_client: PolygonClient, config: UniverseConfig):
        self.client = polygon_client
        self.config = config
        self._universe: list[UniverseTicker] = []
        self._by_ticker: dict[str, UniverseTicker] = {}
        self._built_at: Optional[str] = None

    # ─── Snapshot → models ─────────────────────────────────────────────

    @staticmethod
    def _parse_ticker_row(raw: dict[str, Any], rank: int) -> Optional[UniverseTicker]:
        ticker = raw.get("ticker") or raw.get("T")
        if not ticker:
            return None
        day = raw.get("day") or {}
        prev = raw.get("prevDay") or {}

        close = float(day.get("c", 0) or 0)
        open_p = float(day.get("o", 0) or 0)
        high = float(day.get("h", 0) or 0)
        low = float(day.get("l", 0) or 0)
        volume = float(day.get("v", 0) or 0)
        prev_close = float(prev.get("c", 0) or 0)

        if close <= 0 or volume <= 0:
            return None

        dollar_volume = close * volume
        pct_change = raw.get("todaysChangePerc")
        if pct_change is None and prev_close > 0:
            pct_change = ((close - prev_close) / prev_close) * 100.0
        else:
            pct_change = float(pct_change or 0.0)

        gap_pct = 0.0
        if prev_close > 0 and open_p > 0:
            gap_pct = ((open_p - prev_close) / prev_close) * 100.0

        prev_vol = float(prev.get("v", 0) or 0)
        rel_vol = (volume / prev_vol) if prev_vol > 0 else 0.0

        return UniverseTicker(
            ticker=str(ticker).upper(),
            rank=rank,
            dollar_volume=dollar_volume,
            close=close,
            open_price=open_p,
            prev_close=prev_close,
            volume=volume,
            high=high,
            low=low,
            pct_change=round(pct_change, 2),
            gap_pct=round(gap_pct, 2),
            relative_volume=round(rel_vol, 2),
        )

    def _load_cache(self) -> Optional[dict]:
        path = Path(self.config.universe_file)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Universe cache unreadable ({path}): {e}")
            return None

    def _cache_fresh(self, data: dict) -> bool:
        built_at = data.get("built_at")
        if not built_at:
            return False
        try:
            built = datetime.fromisoformat(built_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        age = (datetime.now() - built.replace(tzinfo=None)).total_seconds()
        if age > self.config.cache_ttl_seconds:
            return False
        # Prefer same calendar day for session-based fields
        if built.date() != datetime.now().date():
            return False
        return True

    def _apply_cached(self, data: dict) -> list[str]:
        rows = data.get("tickers", [])
        self._universe = []
        self._by_ticker = {}
        for i, d in enumerate(rows):
            ut = UniverseTicker(**d)
            ut.rank = i + 1
            self._universe.append(ut)
            self._by_ticker[ut.ticker] = ut
        self._built_at = data.get("built_at")
        return [u.ticker for u in self._universe]

    def _save_cache(self):
        path = Path(self.config.universe_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "built_at": datetime.now().isoformat(),
            "top_n": self.config.top_n,
            "tickers": [asdict(u) for u in self._universe],
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info(f"Universe cache written: {path} ({len(self._universe)} tickers)")

    def build_universe(self, force: bool = False) -> list[str]:
        """
        Load from cache when fresh unless force=True; else fetch snapshot and rebuild.
        Returns ordered list of tickers (highest dollar volume first).
        """
        if not force:
            cached = self._load_cache()
            if cached and self._cache_fresh(cached):
                tickers = self._apply_cached(cached)
                logger.info(f"Universe loaded from cache: {len(tickers)} tickers")
                return tickers

        logger.info("Building universe from Polygon all-tickers snapshot...")
        raw_list = self.client.get_all_stocks_snapshot_tickers()
        logger.info(f"  → {len(raw_list)} raw snapshot rows")

        parsed: list[UniverseTicker] = []
        for raw in raw_list:
            ut = self._parse_ticker_row(raw, rank=0)
            if ut is None:
                continue
            if ut.close < self.config.min_price:
                continue
            if ut.dollar_volume < self.config.min_dollar_volume:
                continue
            parsed.append(ut)

        parsed.sort(key=lambda u: u.dollar_volume, reverse=True)
        self._universe = parsed[: self.config.top_n]
        for i, u in enumerate(self._universe):
            u.rank = i + 1
        self._by_ticker = {u.ticker: u for u in self._universe}
        self._save_cache()
        logger.info(f"Scan universe: {len(self._universe)} tickers (top {self.config.top_n} by $ vol)")
        return [u.ticker for u in self._universe]

    def get_ticker_data(self, ticker: str) -> Optional[UniverseTicker]:
        return self._by_ticker.get(ticker.upper())

    def get_tier2_candidates(
        self,
        refresh: bool = False,
        min_abs_pct_change: float = 5.0,
        min_abs_gap_pct: float = 3.0,
        min_rel_vol: float = 1.5,
        max_names: int = 75,
    ) -> list[str]:
        """
        Promote names with meaningful session action (for intraday scanning).
        Uses snapshot-only metrics to avoid extra API calls.
        """
        if refresh:
            self.build_universe(force=True)

        out: list[str] = []
        for u in self._universe:
            if abs(u.pct_change) >= min_abs_pct_change:
                out.append(u.ticker)
                continue
            if abs(u.gap_pct) >= min_abs_gap_pct:
                out.append(u.ticker)
                continue
            if u.relative_volume >= min_rel_vol and u.relative_volume > 0:
                out.append(u.ticker)
                continue

        # Dedupe preserve order
        seen: set[str] = set()
        tier2: list[str] = []
        for t in out:
            if t not in seen:
                seen.add(t)
                tier2.append(t)
            if len(tier2) >= max_names:
                break
        return tier2

    def get_tier_b_candidates(
        self,
        min_rvol: float = 5.0,
        min_session_dollar_volume: float = 2_000_000,
    ) -> list[str]:
        """
        Stocks with unusual volume that top-$-volume Tier A misses — small-cap runners.
        Uses snapshot-derived fields only (no extra API calls).
        """
        out: list[str] = []
        for u in self._universe:
            if u.relative_volume >= min_rvol and u.dollar_volume >= min_session_dollar_volume:
                out.append(u.ticker)
        return out

    def screen_runners(self, min_pct: float) -> list[dict]:
        """Fast-filter dicts compatible with RunnerFadeScanner.deep_validate_fast_candidates."""
        rows: list[dict] = []
        for u in self._universe:
            if u.pct_change < min_pct:
                continue
            rows.append(
                {
                    "ticker": u.ticker,
                    "close": u.close,
                    "pct_change": u.pct_change,
                    "volume": int(u.volume),
                    "high": u.high,
                    "low": u.low,
                    "dollar_volume": u.dollar_volume,
                }
            )
        return rows

    def screen_exhaustion_candidates(self, min_change_pct: float = 10.0) -> list[dict]:
        """
        Gap-up vs prior close at least min_change_pct; same shape as gainers loop entries.
        """
        out: list[dict] = []
        for u in self._universe:
            if u.prev_close <= 0 or u.open_price <= 0:
                continue
            gap_pct = u.gap_pct
            if gap_pct < min_change_pct:
                continue
            # Build a minimal 'g' compatible with ExhaustionGapScanner.try_build_from_snapshot_gainer
            out.append(
                {
                    "ticker": u.ticker,
                    "day": {
                        "o": u.open_price,
                        "h": u.high,
                        "l": u.low,
                        "c": u.close,
                        "v": u.volume,
                    },
                    "prevDay": {"c": u.prev_close},
                }
            )
        return out

    def screen_big_gaps(
        self,
        min_gap_pct: float = 5.0,
        direction: str = "down",
    ) -> list[dict]:
        """Large gaps vs prior close (open vs prev close)."""
        out: list[dict] = []
        direction = direction.lower()
        for u in self._universe:
            if u.prev_close <= 0 or u.open_price <= 0:
                continue
            g = u.gap_pct
            if direction == "down" and g > -min_gap_pct:
                continue
            if direction == "up" and g < min_gap_pct:
                continue
            out.append(
                {
                    "ticker": u.ticker,
                    "gap_pct": g,
                    "open": u.open_price,
                    "prev_close": u.prev_close,
                    "close": u.close,
                    "dollar_volume": u.dollar_volume,
                }
            )
        return out


def _cli_smoke():
    """Run: python universe_builder.py"""
    import os
    from dotenv import load_dotenv

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from config import APIConfig, UniverseConfig

    api = APIConfig()
    client = PolygonClient(api)
    data_dir = os.getenv("DATA_DIR", ".")
    uc = UniverseConfig(
        top_n=int(os.getenv("UNIVERSE_SIZE", "500")),
        universe_file=os.path.join(data_dir, "universe.json"),
    )
    ub = UniverseBuilder(client, uc)
    tickers = ub.build_universe(force=True)
    print(f"Universe size: {len(tickers)}")
    if tickers:
        print(f"Top 5: {', '.join(tickers[:5])}")
    t2 = ub.get_tier2_candidates(refresh=False)
    print(f"Tier 2 sample ({len(t2)}): {', '.join(t2[:15])}")


if __name__ == "__main__":
    _cli_smoke()
