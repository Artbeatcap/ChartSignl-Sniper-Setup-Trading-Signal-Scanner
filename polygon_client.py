"""
Polygon.io API Client
Handles: OHLCV bars, snapshots, gainers, premarket data.
Rate-limited and fault-tolerant.
"""
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

import requests

from config import APIConfig

logger = logging.getLogger(__name__)


class PolygonClient:
    """Thin, rate-limited wrapper around Polygon.io REST API."""

    def __init__(self, config: APIConfig):
        self.config = config
        self.session = requests.Session()
        self.session.params = {"apiKey": config.api_key}  # type: ignore
        self.session.headers.update({"User-Agent": "SetupSniper/1.0"})
        self._last_request_time = 0.0
        self._min_interval = 60.0 / config.rate_limit_per_min

    def _throttle(self):
        """Enforce rate limit between requests."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            sleep_time = self._min_interval - elapsed
            logger.debug(f"Rate limit: sleeping {sleep_time:.1f}s")
            time.sleep(sleep_time)
        self._last_request_time = time.time()

    def _get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        """GET with throttling, timeout, and error handling."""
        self._throttle()
        url = f"{self.config.base_url}{endpoint}"
        try:
            resp = self.session.get(url, params=params, timeout=self.config.timeout_seconds)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            if resp.status_code == 429:
                logger.warning("Rate limited by Polygon. Backing off 30s...")
                time.sleep(30)
                return self._get(endpoint, params)  # Retry once
            logger.error(f"HTTP {resp.status_code} for {endpoint}: {e}")
            raise
        except requests.exceptions.RequestException as e:
            logger.error(f"Request failed for {endpoint}: {e}")
            raise

    # ─── Market Snapshot Endpoints ──────────────────────────────────────

    def get_all_stocks_snapshot_tickers(self) -> list[dict]:
        """
        GET /v2/snapshot/locale/us/markets/stocks/tickers (paginated).
        Returns all US stock snapshot rows for the current session.
        """
        tickers: list[dict] = []
        next_url: Optional[str] = None
        endpoint = "/v2/snapshot/locale/us/markets/stocks/tickers"

        while True:
            self._throttle()
            if next_url:
                try:
                    resp = self.session.get(
                        next_url, timeout=self.config.timeout_seconds
                    )
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as e:
                    logger.error(f"Snapshot pagination failed: {e}")
                    raise
            else:
                data = self._get(endpoint)

            batch = data.get("tickers", [])
            tickers.extend(batch)
            next_url = data.get("next_url")
            if not next_url:
                break

        return tickers

    def get_gainers(self, include_otc: bool = False) -> list[dict]:
        """
        GET /v2/snapshot/locale/us/markets/stocks/gainers
        Returns top gaining tickers from today's session.
        This is the fast path for the nightly runner scan.
        """
        params = {"include_otc": str(include_otc).lower()}
        data = self._get("/v2/snapshot/locale/us/markets/stocks/gainers", params)
        return data.get("tickers", [])

    def get_snapshot(self, ticker: str) -> Optional[dict]:
        """
        GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}
        Full snapshot: day, prev day, min bar, last trade, etc.
        """
        try:
            data = self._get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}")
            return data.get("ticker")
        except Exception:
            logger.warning(f"Snapshot failed for {ticker}")
            return None

    # ─── Earnings (Benzinga partner API) ────────────────────────────────

    def get_earnings(self, params: Optional[dict] = None) -> list[dict]:
        """
        GET /benzinga/v1/earnings
        Structured historical and upcoming earnings announcements.

        Caller is responsible for passing any filters, e.g.:
          {
            "date.gte": "2025-01-01",
            "date.lte": "2025-01-02",
            "eps_surprise_percent.lte": "0",
            "revenue_surprise_percent.lte": "0",
          }
        """
        data = self._get("/benzinga/v1/earnings", params or {})
        return data.get("results", [])

    # ─── Aggregate (Bar) Endpoints ──────────────────────────────────────

    def get_aggs(
        self,
        ticker: str,
        multiplier: int,
        timespan: str,  # "minute", "hour", "day", "week"
        from_date: str,  # YYYY-MM-DD
        to_date: str,
        adjusted: bool = True,
        sort: str = "asc",
        limit: int = 50000,
    ) -> list[dict]:
        """
        GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}
        Core endpoint for all OHLCV data.
        """
        endpoint = f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from_date}/{to_date}"
        params = {
            "adjusted": str(adjusted).lower(),
            "sort": sort,
            "limit": limit,
        }
        data = self._get(endpoint, params)
        return data.get("results", [])

    def get_daily_bars(self, ticker: str, days_back: int = 30) -> list[dict]:
        """Convenience: fetch N days of daily OHLCV."""
        to_date = datetime.now().strftime("%Y-%m-%d")
        from_date = (datetime.now() - timedelta(days=days_back + 5)).strftime("%Y-%m-%d")
        return self.get_aggs(ticker, 1, "day", from_date, to_date)

    def get_intraday_bars(
        self,
        ticker: str,
        minutes: int = 5,
        date: Optional[str] = None,
        extended_hours: bool = True,
    ) -> list[dict]:
        """
        Fetch intraday bars for a specific date.
        If extended_hours=True, includes premarket (4:00 AM) and after-hours.
        Polygon includes extended hours by default in aggs.
        """
        target_date = date or datetime.now().strftime("%Y-%m-%d")
        return self.get_aggs(ticker, minutes, "minute", target_date, target_date)

    def get_previous_close(self, ticker: str) -> Optional[dict]:
        """
        GET /v2/aggs/ticker/{ticker}/prev
        Previous day's OHLCV. Fast endpoint.
        """
        try:
            data = self._get(f"/v2/aggs/ticker/{ticker}/prev")
            results = data.get("results", [])
            return results[0] if results else None
        except Exception:
            return None

    # ─── Ticker Details ─────────────────────────────────────────────────

    def get_ticker_details(self, ticker: str) -> Optional[dict]:
        """
        GET /v3/reference/tickers/{ticker}
        Market cap, name, type, etc.
        """
        try:
            data = self._get(f"/v3/reference/tickers/{ticker}")
            return data.get("results")
        except Exception:
            return None
