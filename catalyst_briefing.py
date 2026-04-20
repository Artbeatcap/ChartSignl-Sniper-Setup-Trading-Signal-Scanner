"""
Catalyst Briefing Service
━━━━━━━━━━━━━━━━━━━━━━━━━

Lightweight wrapper that uses Anthropic (Claude) to generate short
\"why now\" catalyst blurbs for:
  - Individual alerts (intraday + morning)
  - Nightly watchlist candidates

This module is intentionally decoupled from the rest of the system:
it only depends on HTTP + your existing API keys. If ANTHROPIC_API_KEY
is not set, callers should skip using this service.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

import requests

logger = logging.getLogger(__name__)


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


@dataclass
class CatalystBriefing:
    polygon_api_key: str
    fmp_api_key: str
    anthropic_api_key: str
    model: str = "claude-3-5-sonnet-latest"

    def _call_claude(self, system: str, user: str, max_tokens: int = 256) -> Optional[str]:
        """
        Minimal Anthropic Messages API client.
        Returns the assistant text, or None on failure.
        """
        if not self.anthropic_api_key:
            return None

        headers = {
            "x-api-key": self.anthropic_api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [
                {"role": "user", "content": user},
            ],
        }

        try:
            resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            # Messages API: content is a list of blocks; we expect a single text block.
            content = data.get("content", [])
            if not content:
                return None
            first = content[0]
            if isinstance(first, dict):
                return first.get("text") or ""
            return str(first)
        except Exception as e:
            logger.error(f"Catalyst briefing failed: {e}")
            return None

    # ─── Public helpers used by AlertManager ─────────────────────────────

    def brief_alert(self, alert) -> Optional[str]:
        """
        Generate a one-line catalyst summary for a single alert.
        Safe to call repeatedly; returns None on failure.
        """
        ticker = getattr(alert, "ticker", "?")
        setup = getattr(alert, "setup", "SETUP")
        direction = getattr(alert, "direction", "")
        gap_pct = getattr(alert, "gap_pct", None)

        levels_bits = []
        if gap_pct is not None:
            levels_bits.append(f"gap {gap_pct:+.1f}%")
        if hasattr(alert, "yesterday_high"):
            levels_bits.append(f"yesterday high ${getattr(alert, 'yesterday_high', 0.0):.2f}")
        if hasattr(alert, "pm_high"):
            levels_bits.append(f"PM high ${getattr(alert, 'pm_high', 0.0):.2f}")

        levels_str = ", ".join(levels_bits) if levels_bits else "key technical levels already satisfied"

        system = (
            "You are an equity trading assistant. "
            "Given a ticker, setup name, direction, and brief technical context, "
            "summarize in one concise sentence what likely catalyst or narrative is driving this move. "
            "Do not give trading advice. Avoid repeating raw prices; focus on the story."
        )
        user = (
            f"Ticker: {ticker}\n"
            f"Setup: {setup}\n"
            f"Direction: {direction}\n"
            f"Technical context: {levels_str}.\n"
            "Respond with a single short sentence like "
            "\"Earnings miss and heavy profit taking after a multi-day rally\"."
        )

        return self._call_claude(system, user, max_tokens=96)

    def brief_candidates(self, candidates: Iterable[object], max_count: int = 10) -> None:
        """
        Enrich nightly scan candidates in-place by setting `c.catalyst`.
        To avoid hitting rate limits, only the first `max_count` candidates
        are enriched; others are left untouched.
        """
        if not self.anthropic_api_key:
            return

        for idx, c in enumerate(candidates):
            if idx >= max_count:
                break
            ticker = getattr(c, "ticker", "?")
            pct = getattr(c, "pct_change", getattr(c, "gap_pct", None))
            rvol = getattr(c, "relative_vol", getattr(c, "volume_ratio", None))

            context_bits = []
            if pct is not None:
                context_bits.append(f"move {pct:+.1f}%")
            if rvol is not None:
                context_bits.append(f"relative volume {rvol:.1f}x")

            context = ", ".join(context_bits) if context_bits else "strong move with elevated volume"

            system = (
                "You are an equity trading assistant. "
                "Given a nightly watchlist candidate with price move and volume context, "
                "summarize in one concise sentence the most likely catalyst or narrative "
                "behind today's move. Do not give trading advice."
            )
            user = (
                f"Ticker: {ticker}\n"
                f"Context: {context}.\n"
                "Respond with a single short sentence suitable to display under the ticker "
                "in a trader's nightly watchlist."
            )

            text = self._call_claude(system, user, max_tokens=96)
            if text:
                setattr(c, "catalyst", text.strip())

