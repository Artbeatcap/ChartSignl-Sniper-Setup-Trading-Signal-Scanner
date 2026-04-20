"""Scanner alerts → score → AlertPipeline (dedup/cooldown/cap). regime: data/regime_today.json."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from alert_pipeline import Alert, AlertPipeline, PipelineConfig
from conviction_score import Direction, MarketRegime, SetupId, SetupResult

_RB = {
    SetupId.VIX_SPIKE_MEAN_REVERSION: frozenset({MarketRegime.PANIC, MarketRegime.STRONG_BEAR}),
    SetupId.VIX_CRASH_REVERSAL: frozenset({MarketRegime.STRONG_BULL, MarketRegime.BULL}),
    SetupId.EARNINGS_MISS_SHORT: None,
    SetupId.EARNINGS_BEAT_FADE_ATH: frozenset({MarketRegime.NEUTRAL, MarketRegime.BEAR, MarketRegime.STRONG_BEAR}),
    SetupId.NEWS_GAP_UP_BEAR_MARKET: frozenset({MarketRegime.BEAR, MarketRegime.STRONG_BEAR, MarketRegime.PANIC}),
    SetupId.PRIOR_DAY_RUNNER_FADE: None,
    SetupId.EXHAUSTION_GAP_SHORT: None,
    SetupId.ORL_VWAP_9EMA: None,
    SetupId.DEAD_CAT_BOUNCE: frozenset({MarketRegime.BEAR, MarketRegime.STRONG_BEAR, MarketRegime.PANIC}),
    SetupId.BLACK_SWAN_REVERSAL: frozenset({MarketRegime.PANIC, MarketRegime.STRONG_BEAR}),
}
_BEARS = frozenset({MarketRegime.BEAR, MarketRegime.STRONG_BEAR, MarketRegime.PANIC})

# Exact-match keys (uppercased) → SetupId. Includes the current literal strings
# in scanner_*.py plus the alternates the reference consolidator recognized, so
# this survives minor rewording without changing the canonical ID.
SETUP_NAME_TO_ID: dict[str, SetupId] = {
    "VIX SPIKE MEAN REVERSION":             SetupId.VIX_SPIKE_MEAN_REVERSION,
    "VIX CRASH REVERSAL":                   SetupId.VIX_CRASH_REVERSAL,
    "EARNINGS MISS SHORT":                  SetupId.EARNINGS_MISS_SHORT,
    "EARNINGS BEAT FADE":                   SetupId.EARNINGS_BEAT_FADE_ATH,
    "EARNINGS BEAT FADE AT ATH":            SetupId.EARNINGS_BEAT_FADE_ATH,
    "NEWS GAP UP REVERSAL":                 SetupId.NEWS_GAP_UP_BEAR_MARKET,
    "GAP UP REVERSAL (BEAR MARKET)":        SetupId.NEWS_GAP_UP_BEAR_MARKET,
    "GAP UP REVERSAL BEAR MARKET":          SetupId.NEWS_GAP_UP_BEAR_MARKET,
    "PRIOR DAY RUNNER FADE":                SetupId.PRIOR_DAY_RUNNER_FADE,
    "EXHAUSTION GAP SHORT":                 SetupId.EXHAUSTION_GAP_SHORT,
    "ORL + VWAP + 9EMA":                    SetupId.ORL_VWAP_9EMA,
    "ORL + VWAP REJECT + 9 EMA":            SetupId.ORL_VWAP_9EMA,
    "ORL VWAP 9EMA":                        SetupId.ORL_VWAP_9EMA,
    "DEAD CAT BOUNCE":                      SetupId.DEAD_CAT_BOUNCE,
    "CATALYST BOUNCE":                      SetupId.DEAD_CAT_BOUNCE,
    "BLACK SWAN REVERSAL":                  SetupId.BLACK_SWAN_REVERSAL,
    "BLACK SWAN REVERSAL — LOAD THE BOAT":  SetupId.BLACK_SWAN_REVERSAL,
    "BLACK SWAN PANIC BUY":                 SetupId.BLACK_SWAN_REVERSAL,
}


def _load_regime() -> MarketRegime:
    p = Path(os.getenv("DATA_DIR", ".")) / "regime_today.json"
    if not p.exists():
        return MarketRegime.NEUTRAL
    try:
        return MarketRegime(json.loads(p.read_text(encoding="utf-8")).get("regime", "neutral"))
    except (json.JSONDecodeError, OSError, ValueError):
        return MarketRegime.NEUTRAL


def _parse_setup_id(setup: str) -> Optional[SetupId]:
    if not setup:
        return None
    u = setup.strip().upper()
    sid = SETUP_NAME_TO_ID.get(u)
    if sid is not None:
        return sid
    for key, ksid in SETUP_NAME_TO_ID.items():
        if key in u:
            return ksid
    for keys, kid in (
        (("PRIOR DAY RUNNER",), SetupId.PRIOR_DAY_RUNNER_FADE),
        (("RUNNER FADE",),      SetupId.PRIOR_DAY_RUNNER_FADE),
        (("EARNINGS MISS",),    SetupId.EARNINGS_MISS_SHORT),
        (("EXHAUSTION GAP",),   SetupId.EXHAUSTION_GAP_SHORT),
        (("ORL", "VWAP"),       SetupId.ORL_VWAP_9EMA),
        (("VIX SPIKE",),        SetupId.VIX_SPIKE_MEAN_REVERSION),
        (("VIX CRASH",),        SetupId.VIX_CRASH_REVERSAL),
        (("CRASH REVERSAL",),   SetupId.VIX_CRASH_REVERSAL),
        (("BLACK SWAN",),       SetupId.BLACK_SWAN_REVERSAL),
        (("DEAD CAT",),         SetupId.DEAD_CAT_BOUNCE),
    ):
        if all(k in u for k in keys):
            return kid
    if "GAP" in u and ("REVERSAL" in u or "BEAR" in u):
        return SetupId.NEWS_GAP_UP_BEAR_MARKET
    if "BEAT" in u and "FADE" in u:
        return SetupId.EARNINGS_BEAT_FADE_ATH
    return None


def _dir(a: Any) -> Direction:
    d = str(getattr(a, "direction", "SHORT")).upper()
    return Direction.LONG if d.startswith("LONG") else Direction.SHORT


def _rvol(a: Any, ub: Any) -> float:
    v = getattr(a, "relative_vol", None) or getattr(a, "volume_ratio", None)
    if v is not None and float(v) > 0:
        return float(v)
    t = getattr(a, "ticker", "") or ""
    if ub and t:
        row = ub.get_ticker_data(t)
        if row and row.relative_volume > 0:
            return float(row.relative_volume)
    return 1.0


def _price(a: Any) -> float:
    for n in ("pm_price", "current_price", "close_price"):
        v = getattr(a, n, None)
        if v is not None and float(v) > 0:
            return float(v)
    return 0.0


def _reg_adj(sid: SetupId, reg: MarketRegime) -> float:
    w = _RB.get(sid)
    if w is None:
        return 0.10 if sid == SetupId.PRIOR_DAY_RUNNER_FADE and reg in _BEARS else 0.0
    return 0.10 if reg in w else -0.10


def _score(sid: SetupId, reg: MarketRegime, rv: float, tb: bool, ta10: bool, multi: bool) -> float:
    x = 0.60 + (0.10 if rv > 3 else 0) + (0.10 if rv > 5 else 0) + (0.10 if tb or ta10 else 0) + _reg_adj(sid, reg) + (0.05 if multi else 0)
    return max(0.0, min(1.0, x))


def _sr(sid: SetupId, name: str, t: str, dr: Direction, sc: float, src: Any) -> SetupResult:
    p = max(1, min(100, int(round(sc * 100))))
    ex = getattr(src, "exit_plan", "") or ""
    return SetupResult(
        setup_id=sid, setup_name=name, direction=dr, ticker=t,
        base_conditions_met=True, time_window_valid=True, confluence_points=p, max_points=100,
        regime_penalty=0, reasons_fired=[getattr(src, "setup", name) or name], reasons_failed=[],
        entry_hint=str(getattr(src, "entry", "") or ""), exit_hint=str(ex),
    )


@dataclass
class ConsolidatedAlert:
    """Thin wrapper around a survivor scanner alert. Unknown attribute reads fall
    through to ``source`` so existing ``alerts.py`` formatting (``pm_high``,
    ``yesterday_high``, etc.) keeps working unchanged."""
    ticker: str
    setup: str
    direction: str
    grade: str
    score: float
    regime: str
    is_upgrade: bool
    previous_grade: Optional[str]
    supporting_setups: list[str]
    source: Any

    def __getattr__(self, name: str) -> Any:
        src = self.__dict__.get("source")
        if src is None:
            raise AttributeError(name)
        return getattr(src, name)


@dataclass
class ConsolidateBatchResult:
    delivery_order: list[Any]
    shadow_rows: list[dict]


class AlertConsolidator:
    def __init__(self, universe_builder: Any = None):
        self.universe_builder = universe_builder
        self._pipeline = AlertPipeline(PipelineConfig())

    def _tiers(self) -> tuple[set[str], set[str]]:
        ta, tb = set(), set()
        ub = self.universe_builder
        if not ub:
            return ta, tb
        try:
            for x in ub.get_tier_b_candidates():
                tb.add(x.upper())
        except Exception:
            pass
        try:
            for u in getattr(ub, "_universe", [])[:10]:
                ta.add(u.ticker.upper())
        except Exception:
            pass
        return ta, tb

    def process_batch(self, alerts: list[Any], now: datetime) -> ConsolidateBatchResult:
        reg = _load_regime()
        tier_a, tier_b = self._tiers()
        thru: list[Any] = []
        by: dict[str, list[tuple[Any, SetupId, float]]] = {}
        for a in alerts:
            sid = _parse_setup_id(getattr(a, "setup", ""))
            if sid is None:
                thru.append(a)
                continue
            t = str(getattr(a, "ticker", "") or "").upper()
            by.setdefault(t, []).append((a, sid, _rvol(a, self.universe_builder)))

        cands: dict[str, Alert] = {}
        prim: dict[str, Any] = {}
        support: dict[str, list[str]] = {}
        for t, lst in by.items():
            multi = len(lst) > 1
            scored = [(a, sid, rv, _score(sid, reg, rv, t in tier_b, t in tier_a, multi)) for a, sid, rv in lst]
            scored.sort(key=lambda z: -z[3])
            pa, psid, prv, psc = scored[0]
            sup_srs = [_sr(sid, str(getattr(a, "setup", "")), t, _dir(a), sc, a) for a, sid, rv, sc in scored[1:]]
            ps = _sr(psid, str(getattr(pa, "setup", "")), t, _dir(pa), psc, pa)
            cands[t] = Alert(
                alert_id=str(uuid.uuid4())[:8], ticker=t, timestamp=now, primary_setup=ps,
                supporting_setups=sup_srs, price=_price(pa), rvol=prv, atr_daily=float(getattr(pa, "daily_atr", 0) or 0),
            )
            prim[t] = pa
            support[t] = [str(getattr(a, "setup", "")) for a, _, _, _ in scored[1:]]

        cand_list = list(cands.values())
        pip = self._pipeline
        pip._maybe_auto_reset(now)
        pip._prune_history(now)

        after: list[Alert] = []
        cooldown_tickers: set[str] = set()
        for ca in cand_list:
            ok, up, prev = pip._check_cooldown_and_upgrade(ca, now)
            if ok:
                ca.is_upgrade, ca.previous_grade = up, prev
                after.append(ca)
            else:
                cooldown_tickers.add(ca.ticker)
        after.sort(key=lambda x: (-x.score, -x.rvol if pip.config.use_rvol_tiebreak else 0, x.ticker))
        fin = pip._apply_hourly_cap(after, now)
        ft = {x.ticker for x in fin}
        capped_tickers = {x.ticker for x in after if x.ticker not in ft}
        for x in fin:
            pip._record_alert(x, now)

        sh: list[dict] = []
        for ca in cand_list:
            t = ca.ticker
            if t in ft:
                outcome, note = "emitted", "upgrade" if ca.is_upgrade else ""
            elif t in cooldown_tickers:
                outcome, note = "cooldown", f"within {pip.config.per_ticker_cooldown_minutes}m cooldown"
            elif t in capped_tickers:
                outcome, note = "capped", f"hourly cap {pip.config.max_alerts_per_hour} reached"
            else:
                outcome, note = "suppressed", ""
            sh.append({
                "ts": now.isoformat(),
                "ticker": t,
                "outcome": outcome,
                "note": note,
                "grade": ca.grade,
                "score": round(ca.score, 3),
                "primary_setup": ca.primary_setup.setup_name,
                "supporting": support.get(t, []),
                "is_upgrade": ca.is_upgrade,
                "previous_grade": ca.previous_grade,
                "regime": reg.value,
            })
        sh += [{"ts": now.isoformat(), "ticker": getattr(a, "ticker", ""), "outcome": "passthrough",
                "note": "unknown setup string", "setup": getattr(a, "setup", "")} for a in thru]

        out, seen = [], set()
        for a in alerts:
            if a in thru:
                out.append(a)
                continue
            t = str(getattr(a, "ticker", "") or "").upper()
            if t in ft and id(a) == id(prim.get(t)) and id(a) not in seen:
                seen.add(id(a))
                ca = cands[t]
                out.append(ConsolidatedAlert(
                    ticker=t,
                    setup=str(getattr(a, "setup", "")),
                    direction=str(getattr(a, "direction", "SHORT")),
                    grade=ca.grade,
                    score=round(ca.score, 3),
                    regime=reg.value,
                    is_upgrade=ca.is_upgrade,
                    previous_grade=ca.previous_grade,
                    supporting_setups=support.get(t, []),
                    source=a,
                ))
        return ConsolidateBatchResult(delivery_order=out, shadow_rows=sh)

    def append_shadow(self, rows: list[dict]) -> None:
        if not rows:
            return
        d = Path(os.getenv("DATA_DIR", "."))
        d.mkdir(parents=True, exist_ok=True)
        with (d / "consolidator_shadow.jsonl").open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, default=str) + "\n")
