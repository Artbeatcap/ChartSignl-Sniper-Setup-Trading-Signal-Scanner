"""
Financial Modeling Prep (FMP) API client
Used as primary earnings source for Setup 3 (Earnings Miss Short).
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict

import requests

logger = logging.getLogger(__name__)


class FmpClient:
    def __init__(self, api_key: str, timeout_seconds: int = 10):
        self.api_key = api_key
        self.base_url = "https://financialmodelingprep.com/stable"
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        self.session.params = {"apikey": api_key}  # type: ignore

    def _get(self, path: str, params: Dict) -> List[Dict]:
        url = f"{self.base_url}{path}"
        merged = {**self.session.params, **params}
        try:
            resp = self.session.get(url, params=merged, timeout=self.timeout_seconds)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            return data.get("earningsCalendar", []) or data.get("results", []) or []
        except requests.RequestException as e:
            logger.error(f"FMP request failed for {path}: {e}")
            return []

    def get_recent_earnings_misses(
        self,
        lookback_days: int,
        eps_surprise_threshold: float,
        revenue_surprise_threshold: float,
    ) -> List[Dict]:
        """
        Fetch recent earnings and filter for misses.
        Uses /earnings-calendar and computes surprise percentages.
        """
        today = datetime.now(timezone.utc).date()
        start_date = (today - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")

        records = self._get(
            "/earnings-calendar",
            {"from": start_date, "to": end_date},
        )
        logger.info(f"FMP earnings calendar: {len(records)} records from {start_date} to {end_date}")

        misses: List[Dict] = []
        for r in records:
            ticker = r.get("symbol")
            if not ticker:
                continue

            eps = r.get("eps")
            eps_est = r.get("epsEstimated")
            rev = r.get("revenue")
            rev_est = r.get("revenueEstimated")

            eps_sp_pct = None
            if eps is not None and eps_est not in (None, 0):
                try:
                    eps_sp_pct = (float(eps) - float(eps_est)) / float(eps_est) * 100.0
                except (ValueError, ZeroDivisionError):
                    eps_sp_pct = None

            rev_sp_pct = None
            if rev is not None and rev_est not in (None, 0):
                try:
                    rev_sp_pct = (float(rev) - float(rev_est)) / float(rev_est) * 100.0
                except (ValueError, ZeroDivisionError):
                    rev_sp_pct = None

            # Require both to be <= threshold when available
            if eps_sp_pct is None or rev_sp_pct is None:
                continue
            if eps_sp_pct > eps_surprise_threshold or rev_sp_pct > revenue_surprise_threshold:
                continue

            misses.append(
                {
                    "ticker": ticker,
                    "company_name": r.get("company"),
                    "date": r.get("date"),
                    "time": r.get("time"),
                    "eps_surprise_percent": eps_sp_pct,
                    "revenue_surprise_percent": rev_sp_pct,
                }
            )

        logger.info(f"FMP filtered earnings misses: {len(misses)}")
        return misses

    def get_earnings_rows_for_symbol(
        self,
        symbol: str,
        from_date: str,
        to_date: str,
    ) -> List[Dict]:
        """Earnings calendar rows for a ticker between two dates (inclusive)."""
        records = self._get(
            "/earnings-calendar",
            {"from": from_date, "to": to_date},
        )
        sym = symbol.upper()
        return [r for r in records if r.get("symbol", "").upper() == sym]

