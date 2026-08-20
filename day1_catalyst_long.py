"""
day1_catalyst_long.py — Setup 11: Day-1 Catalyst Gap Long.

Gates run cheapest-first and fail fast. The news gate is hard: no confirmed
material announcement, no signal.

    1. Gap     ≥ +20% vs prior close
    2. RVOL    ≥ 10× the 20-day average
    3. News    primary-source material announcement within 18h
    4. Tape    @ 09:45: above the 5-min 9 EMA, no break before 09:45,
               extension ≥ 3%

SIGNAL is research-labeled. Six logged events is not an edge. The standing
rule in CATALYST_RERATING.md: the immediate value is knowing when NOT to
short. Revisit a live long at 20+ events.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from catalyst_news import NewsGateResult, unconfirmed_result
from indicators import ema as ema_series

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

SETUP_NAME = "SETUP 11: Day-1 Catalyst Gap Long"
WATCH_TAG = "SETUP 11: Catalyst re-rating WATCH — do not fade"
RESEARCH_ONLY = True
RESEARCH_NOTE = (
    "Research only (n=6). Not a playbook long until 20+ logged events. "
    "Standing rule: do not fade a confirmed cash-flow catalyst."
)

MIN_GAP_PCT = 20.0
MIN_RVOL = 10.0
MIN_EXTENSION_PCT = 3.0
EMA_PERIOD = 9
DECISION_TIME = time(9, 45)
RTH_OPEN = time(9, 30)


@dataclass
class MinuteBar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Setup11Result:
    ticker: str
    passed: bool
    stage: str
    reason: str
    news: Optional[NewsGateResult] = None
    entry: Optional[float] = None
    extension_pct: Optional[float] = None
    rvol: Optional[float] = None
    gap_pct: Optional[float] = None
    ema_9: Optional[float] = None
    broke_at: Optional[str] = None
    research_only: bool = RESEARCH_ONLY
    feeds_tried: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.passed:
            return f"SIGNAL (research) — {self.reason}"
        return f"{self.stage} — {self.reason}"


def _as_et(ts) -> datetime:
    if isinstance(ts, datetime):
        dt = ts
    elif isinstance(ts, (int, float)):
        seconds = ts / 1000.0 if ts > 1e12 else float(ts)
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    else:
        raise TypeError(f"unsupported timestamp: {ts!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET)


def normalize_bars(raw: Sequence) -> list[MinuteBar]:
    out: list[MinuteBar] = []
    for b in raw:
        if isinstance(b, MinuteBar):
            bar = MinuteBar(
                ts=_as_et(b.ts),
                open=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                volume=float(b.volume),
            )
        elif isinstance(b, dict):
            ts = b.get("t") if "t" in b else b.get("ts")
            bar = MinuteBar(
                ts=_as_et(ts),
                open=float(b.get("o", b.get("open", 0))),
                high=float(b.get("h", b.get("high", 0))),
                low=float(b.get("l", b.get("low", 0))),
                close=float(b.get("c", b.get("close", 0))),
                volume=float(b.get("v", b.get("volume", 0)) or 0),
            )
        else:
            continue
        out.append(bar)
    out.sort(key=lambda x: x.ts)
    return out


def evaluate_technical_gate(
    bars: Sequence,
    *,
    decision: time = DECISION_TIME,
    ema_period: int = EMA_PERIOD,
    min_extension_pct: float = MIN_EXTENSION_PCT,
) -> dict:
    """Return {passed, reason, entry, extension_pct, ema_9, broke_at}."""
    series = normalize_bars(bars)
    if not series:
        return {
            "passed": False,
            "reason": "no 5-min bars to evaluate the 09:45 technical gate",
            "entry": None,
            "extension_pct": None,
            "ema_9": None,
            "broke_at": None,
        }

    closes = np.array([b.close for b in series], dtype=float)
    ema_vals = ema_series(closes, ema_period)

    broke_at = None
    decision_idx = None
    for i, bar in enumerate(series):
        t = bar.ts.time()
        if t < RTH_OPEN:
            continue
        ema_v = ema_vals[i]
        if t < decision:
            if not np.isnan(ema_v) and bar.close < float(ema_v):
                broke_at = bar.ts.strftime("%H:%M")
                return {
                    "passed": False,
                    "reason": (
                        f"5-min 9 EMA broke at {broke_at} — gap not defended. "
                        "No trade in either direction."
                    ),
                    "entry": float(bar.close),
                    "extension_pct": None,
                    "ema_9": float(ema_v),
                    "broke_at": broke_at,
                }
        if t == decision:
            decision_idx = i

    if decision_idx is None:
        return {
            "passed": False,
            "reason": f"missing {decision.strftime('%H:%M')} 5-min bar",
            "entry": None,
            "extension_pct": None,
            "ema_9": None,
            "broke_at": None,
        }

    bar = series[decision_idx]
    ema_v = ema_vals[decision_idx]
    if np.isnan(ema_v) or float(ema_v) <= 0:
        return {
            "passed": False,
            "reason": "5-min 9 EMA not defined at 09:45 (need premarket bars)",
            "entry": float(bar.close),
            "extension_pct": None,
            "ema_9": None,
            "broke_at": None,
        }

    ema_f = float(ema_v)
    entry = float(bar.close)
    if entry <= ema_f:
        return {
            "passed": False,
            "reason": (
                f"09:45 close {entry:.2f} is not above 5-min 9 EMA {ema_f:.2f}"
            ),
            "entry": entry,
            "extension_pct": (entry / ema_f - 1.0) * 100.0,
            "ema_9": ema_f,
            "broke_at": None,
        }

    extension = (entry / ema_f - 1.0) * 100.0
    if extension < min_extension_pct:
        return {
            "passed": False,
            "reason": (
                f"09:45 extension above 9 EMA is {extension:.1f}% "
                f"(need ≥ {min_extension_pct:.0f}%)"
            ),
            "entry": entry,
            "extension_pct": extension,
            "ema_9": ema_f,
            "broke_at": None,
        }

    return {
        "passed": True,
        "reason": (
            f"gap defended — 09:45 close {entry:.2f}, "
            f"ext +{extension:.1f}% above 9 EMA {ema_f:.2f}"
        ),
        "entry": entry,
        "extension_pct": extension,
        "ema_9": ema_f,
        "broke_at": None,
    }


class Day1CatalystLong:
    """Pure evaluator. Does not fetch news or bars — callers inject them."""

    def __init__(
        self,
        min_gap_pct: float = MIN_GAP_PCT,
        min_rvol: float = MIN_RVOL,
        min_extension_pct: float = MIN_EXTENSION_PCT,
        ema_period: int = EMA_PERIOD,
        decision: time = DECISION_TIME,
    ):
        self.min_gap_pct = min_gap_pct
        self.min_rvol = min_rvol
        self.min_extension_pct = min_extension_pct
        self.ema_period = ema_period
        self.decision = decision

    def evaluate(
        self,
        ticker: str,
        *,
        prev_close: float,
        last_price: Optional[float] = None,
        session_volume: float = 0.0,
        avg_vol_20d: float = 0.0,
        bars_5m: Sequence = (),
        news: Optional[NewsGateResult] = None,
        gap_pct: Optional[float] = None,
        rvol: Optional[float] = None,
    ) -> Setup11Result:
        t = ticker.upper()
        feeds = list(news.feeds_tried) if news else []

        if gap_pct is None:
            if not prev_close:
                return Setup11Result(
                    ticker=t, passed=False, stage="gap-fail",
                    reason="prior close missing", news=news, feeds_tried=feeds,
                )
            px = last_price if last_price is not None else prev_close
            gap_pct = (px / prev_close - 1.0) * 100.0

        if gap_pct < self.min_gap_pct:
            return Setup11Result(
                ticker=t, passed=False, stage="gap-fail",
                reason=f"gap {gap_pct:.1f}% < {self.min_gap_pct:.0f}%",
                news=news, gap_pct=gap_pct, feeds_tried=feeds,
            )

        if rvol is None:
            rvol = (session_volume / avg_vol_20d) if avg_vol_20d else 0.0
        if rvol < self.min_rvol:
            return Setup11Result(
                ticker=t, passed=False, stage="rvol-fail",
                reason=f"RVOL {rvol:.1f}× < {self.min_rvol:.0f}×",
                news=news, gap_pct=gap_pct, rvol=rvol, feeds_tried=feeds,
            )

        gate = news if news is not None else unconfirmed_result(feeds)
        if gate.tier == "rejected":
            return Setup11Result(
                ticker=t, passed=False, stage="news-rejected",
                reason=gate.reason, news=gate, gap_pct=gap_pct, rvol=rvol,
                feeds_tried=gate.feeds_tried or feeds,
            )
        if not gate.passed or gate.tier != "confirmed":
            return Setup11Result(
                ticker=t, passed=False, stage="unconfirmed",
                reason=gate.reason, news=gate, gap_pct=gap_pct, rvol=rvol,
                feeds_tried=gate.feeds_tried or feeds,
            )

        tech = evaluate_technical_gate(
            bars_5m,
            decision=self.decision,
            ema_period=self.ema_period,
            min_extension_pct=self.min_extension_pct,
        )
        if not tech["passed"]:
            return Setup11Result(
                ticker=t, passed=False, stage="technical-gate",
                reason=tech["reason"], news=gate, gap_pct=gap_pct, rvol=rvol,
                entry=tech["entry"], extension_pct=tech["extension_pct"],
                ema_9=tech["ema_9"], broke_at=tech["broke_at"],
                feeds_tried=gate.feeds_tried or feeds,
            )

        return Setup11Result(
            ticker=t, passed=True, stage="SIGNAL",
            reason=f"{gate.reason}; {tech['reason']}. {RESEARCH_NOTE}",
            news=gate, gap_pct=gap_pct, rvol=rvol,
            entry=tech["entry"], extension_pct=tech["extension_pct"],
            ema_9=tech["ema_9"],
            feeds_tried=gate.feeds_tried or feeds,
        )


def apply_news_overlay(mover, gate: NewsGateResult) -> None:
    """Mutate a premarket mover in place from a news-gate result.

    confirmed  → suppress Setup 5 fade tag; WATCH / do not fade
    rejected   → keep fade-setup territory; annotate the reject
    unconfirmed → WATCH; check the wire yourself
    """
    mover.news_gate_tier = gate.tier
    mover.news_gate_reason = gate.reason
    tags = list(getattr(mover, "setup_tags", None) or [])

    if gate.tier == "confirmed":
        tags = [x for x in tags if not x.upper().startswith("SETUP 5:")]
        if WATCH_TAG not in tags:
            tags.insert(0, WATCH_TAG)
        mover.setup_tags = tags
        mover.direction = "WATCH"
        mover.confidence = "HIGH"
        headline = ""
        if gate.catalyst is not None:
            headline = f" {gate.catalyst.headline}"
        mover.entry_note = (
            f"Primary-source catalyst confirmed.{headline} "
            "Do not fade this gap. " + RESEARCH_NOTE
        )
        mover.exit_note = "No short. If researching the long, wait for the 09:45 technical gate."
        extra = "Confirmed cash-flow catalyst — Setup 5 fade is suppressed"
        mover.warning = " | ".join(
            p for p in (getattr(mover, "warning", "") or "", extra) if p
        )
        return

    if gate.tier == "rejected":
        extra = f"News gate rejected ({gate.reason})"
        mover.warning = " | ".join(
            p for p in (getattr(mover, "warning", "") or "", extra) if p
        )
        if not getattr(mover, "entry_note", ""):
            mover.entry_note = "Sentiment/dilution/analyst — fade-setup territory, not a long."
        return

    extra = (
        "Gap qualified but no catalyst resolved — unconfirmed WATCH. "
        "Check the wire yourself before the 09:45 bar."
    )
    if "LARGE GAP UP" not in " ".join(tags).upper() and WATCH_TAG not in tags:
        tags.append("LARGE GAP UP - Unconfirmed catalyst (check the wire)")
        mover.setup_tags = tags
    if not getattr(mover, "direction", ""):
        mover.direction = "WATCH"
        mover.confidence = "LOW"
    mover.warning = " | ".join(
        p for p in (getattr(mover, "warning", "") or "", extra) if p
    )
    if not getattr(mover, "entry_note", ""):
        mover.entry_note = extra
