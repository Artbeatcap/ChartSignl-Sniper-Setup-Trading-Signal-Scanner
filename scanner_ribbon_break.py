"""
Setup 11: Extended name loses daily 9 EMA (ribbon-break short)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The 10 event-driven setups miss names that simply walk up the ribbon and
gap through the daily 9 EMA. That failure is structural: a close-updated
level cannot be "crossed" intraday on a gap-through.

Three stages, one persisted 9 EMA:
  Stage 1 — 15:45 ET  PROXIMITY WATCH  (daily bars, universe pass)
  Stage 2 — 08:00 ET  PREMARKET CONFIRM (hard gate: PM high < persisted EMA)
  Stage 3 — 09:45 ET  EXECUTION         (ORL break + 9 EMA < VWAP, slope down)

CRITICAL: Stage 2/3 read daily_ema9 persisted from Stage 1. Recomputing
intraday reintroduces the gap-through bug this setup exists to fix.

Usage:
  python main.py ribbon-watch   # Stage 1 (15:45 ET and nightly refresh)
  python main.py morning        # Stage 2 (with the other morning checks)
  python main.py intraday       # Stage 3 (gated on Stage 2 names)
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np

from config import RibbonBreakConfig, ScannerConfig
from indicators import atr, ema, ema_slope, opening_range, vwap
from polygon_client import PolygonClient
from range_floors import compute_levels, log_floor_decision
from universe_builder import UniverseBuilder

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
SETUP_WATCH = "RIBBON BREAK WATCH"
SETUP_SHORT = "RIBBON BREAK SHORT"
MAX_SCORE = 6


# ─── Session time helpers ────────────────────────────────────────────────

def bar_dt_et(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(ET)


def bar_minutes_et(ts_ms: int) -> int:
    dt = bar_dt_et(ts_ms)
    return dt.hour * 60 + dt.minute


def is_premarket_bar(ts_ms: int) -> bool:
    """04:00–09:30 ET (bar start)."""
    m = bar_minutes_et(ts_ms)
    return 4 * 60 <= m < 9 * 60 + 30


def is_rth_bar(ts_ms: int) -> bool:
    """09:30–16:00 ET (bar start)."""
    m = bar_minutes_et(ts_ms)
    return 9 * 60 + 30 <= m < 16 * 60


def filter_premarket_bars(bars: list[dict]) -> list[dict]:
    return [b for b in bars if b.get("t") and is_premarket_bar(int(b["t"]))]


def filter_rth_bars(bars: list[dict]) -> list[dict]:
    return [b for b in bars if b.get("t") and is_rth_bar(int(b["t"]))]


def _ohlcv(bars: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    opens = np.array([float(b["o"]) for b in bars], dtype=float)
    highs = np.array([float(b["h"]) for b in bars], dtype=float)
    lows = np.array([float(b["l"]) for b in bars], dtype=float)
    closes = np.array([float(b["c"]) for b in bars], dtype=float)
    volumes = np.array([float(b.get("v") or 0) for b in bars], dtype=float)
    return opens, highs, lows, closes, volumes


# ─── Dataclasses ─────────────────────────────────────────────────────────

@dataclass
class RibbonWatch:
    """Stage 1 hit. daily_ema9 is frozen here and must travel with the name."""

    ticker: str
    close: float
    open_price: float
    high: float
    low: float
    volume: float
    daily_ema9: float
    daily_ema65: Optional[float]
    atr14: float
    extended_sessions: int
    max_extension: float
    max_extension_atr: float
    compress_atr: float
    lower_high: bool
    volume_fade: bool
    red_close: bool
    score: int
    scan_date: str  # YYYY-MM-DD of the signal bar
    prior_high: float
    prior_close: float
    spy_close: Optional[float] = None
    spy_ema9: Optional[float] = None
    regime_override: bool = False  # True if allowed only via local >1 ATR extension
    stage2_qualified: bool = False
    grade: str = ""
    pm_high: Optional[float] = None
    pm_low: Optional[float] = None
    pm_last: Optional[float] = None
    gap_pct: Optional[float] = None
    stop: Optional[float] = None
    t1: Optional[float] = None
    t2: Optional[float] = None


@dataclass
class RibbonAlert:
    """Duck-typed alert for AlertManager (Stage 1 watch or Stage 2 short)."""

    ticker: str
    setup: str = SETUP_SHORT
    direction: str = "SHORT"
    confidence: str = ""
    entry: str = ""
    exit_plan: str = ""
    # Levels
    pm_price: float = 0.0
    pm_high: float = 0.0
    pm_low: float = 0.0
    ema_9_current: float = 0.0  # persisted daily 9 EMA (not 5-min)
    daily_atr: Optional[float] = None
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    daily_ema9: float = 0.0
    grade: str = ""
    score: int = 0
    extended_sessions: int = 0
    below_9ema: bool = False
    supertrend_direction: str = ""
    scan_date: str = ""
    stage: str = ""


@dataclass
class RibbonEntryAlert:
    """Stage 3 execution alert (intraday)."""

    ticker: str
    setup: str = SETUP_SHORT
    direction: str = "SHORT"
    stage: str = "ENTRY_SIGNAL"
    stage_number: int = 4
    message: str = ""
    current_price: float = 0.0
    orl: float = 0.0
    orh: float = 0.0
    ema_9: float = 0.0
    vwap_level: float = 0.0
    ema_slope_val: float = 0.0
    daily_atr: Optional[float] = None
    daily_ema9: float = 0.0
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    target_1r: Optional[float] = None
    exit_plan: str = ""
    confidence: str = ""
    stop_was_widened: bool = False
    floor_source: str = ""
    grade: str = ""


@dataclass
class RibbonExecState:
    """Per-ticker Stage 3 session state."""

    ticker: str
    daily_ema9: float
    pm_high: float
    pm_low: float
    atr14: float
    planned_stop: float
    planned_t1: float
    planned_t2: float
    grade: str = ""
    orh: float = 0.0
    orl: float = 0.0
    reclaimed: bool = False
    fired: bool = False
    pm_closes: list = field(default_factory=list)
    rth_opens: list = field(default_factory=list)
    rth_highs: list = field(default_factory=list)
    rth_lows: list = field(default_factory=list)
    rth_closes: list = field(default_factory=list)
    rth_volumes: list = field(default_factory=list)


# ─── Pure Stage 1 / Stage 2 logic (no I/O — unit-tested against PLTR) ────

def consecutive_above_ema(closes: np.ndarray, ema9: np.ndarray) -> tuple[int, float]:
    """Trailing sessions with close > ema9, plus max (close - ema9) over that run."""
    n = 0
    max_dist = 0.0
    for i in range(len(closes) - 1, -1, -1):
        if np.isnan(ema9[i]):
            break
        if closes[i] > ema9[i]:
            n += 1
            max_dist = max(max_dist, float(closes[i] - ema9[i]))
        else:
            break
    return n, max_dist


def spy_is_flat_to_down(spy_close: Optional[float], spy_ema9: Optional[float]) -> bool:
    if spy_close is None or spy_ema9 is None or spy_ema9 == 0:
        return True  # unknown tape: do not suppress
    return spy_close <= spy_ema9


def evaluate_stage1(
    ticker: str,
    bars: list[dict],
    cfg: RibbonBreakConfig,
    scan_date: str,
    spy_close: Optional[float] = None,
    spy_ema9: Optional[float] = None,
) -> Optional[RibbonWatch]:
    """
    Proximity watch on one name's daily bars (oldest first).
    Last bar is the signal bar. Returns None if the name does not fire.
    """
    if len(bars) < max(cfg.ema_period, cfg.atr_period) + cfg.min_extended_sessions:
        return None

    opens, highs, lows, closes, volumes = _ohlcv(bars)
    close = float(closes[-1])
    if close < cfg.min_price or close > cfg.max_price:
        return None
    dollar_vol = close * float(volumes[-1])
    if dollar_vol < cfg.min_dollar_volume:
        return None

    ema9 = ema(closes, cfg.ema_period)
    atr14 = atr(highs, lows, closes, cfg.atr_period)
    daily_ema9 = float(ema9[-1])
    daily_atr = float(atr14[-1])
    if np.isnan(daily_ema9) or np.isnan(daily_atr) or daily_atr <= 0:
        return None

    sessions, max_ext = consecutive_above_ema(closes, ema9)
    max_ext_atr = max_ext / daily_atr
    extended = (
        sessions >= cfg.min_extended_sessions
        and max_ext >= cfg.min_extension_atr * daily_atr
    )
    dist = abs(close - daily_ema9)
    compressing = dist <= cfg.compress_atr * daily_atr
    if not (extended and compressing):
        return None

    prior_high = float(highs[-2]) if len(highs) >= 2 else float(highs[-1])
    prior_close = float(closes[-2]) if len(closes) >= 2 else close
    lower_high = float(highs[-1]) < prior_high
    red_close = close < float(opens[-1])

    volume_fade = False
    look = cfg.volume_lookback
    if len(volumes) >= look + 1:
        prior_mean = float(np.mean(volumes[-(look + 1) : -1]))
        if prior_mean > 0:
            volume_fade = float(volumes[-1]) < prior_mean

    score = 3
    if lower_high:
        score += 1
    if volume_fade:
        score += 1
    if red_close:
        score += 1
    if score < cfg.min_score:
        return None

    flat_to_down = spy_is_flat_to_down(spy_close, spy_ema9)
    local_override = max_ext_atr >= cfg.local_extension_override_atr
    if not flat_to_down and not local_override:
        logger.info(
            f"  {ticker}: Stage 1 suppressed — SPY uptrend and extension "
            f"{max_ext_atr:.2f} ATR < {cfg.local_extension_override_atr}"
        )
        return None

    ema65_val = None
    if len(closes) >= cfg.ema_65_period:
        e65 = ema(closes, cfg.ema_65_period)
        if not np.isnan(e65[-1]):
            ema65_val = round(float(e65[-1]), 2)

    return RibbonWatch(
        ticker=ticker.upper(),
        close=round(close, 2),
        open_price=round(float(opens[-1]), 2),
        high=round(float(highs[-1]), 2),
        low=round(float(lows[-1]), 2),
        volume=float(volumes[-1]),
        daily_ema9=round(daily_ema9, 2),
        daily_ema65=ema65_val,
        atr14=round(daily_atr, 2),
        extended_sessions=sessions,
        max_extension=round(max_ext, 2),
        max_extension_atr=round(max_ext_atr, 2),
        compress_atr=round(dist / daily_atr, 4),
        lower_high=lower_high,
        volume_fade=volume_fade,
        red_close=red_close,
        score=score,
        scan_date=scan_date,
        prior_high=round(prior_high, 2),
        prior_close=round(prior_close, 2),
        spy_close=spy_close,
        spy_ema9=spy_ema9,
        regime_override=bool((not flat_to_down) and local_override),
    )


def evaluate_stage2(
    watch: RibbonWatch,
    pm_bars: list[dict],
    cfg: RibbonBreakConfig,
) -> Optional[RibbonWatch]:
    """
    Premarket confirm. Hard gate: pm_high < persisted daily_ema9.
    Does NOT recompute the daily 9 EMA.
    """
    daily_ema9 = watch.daily_ema9  # persisted — do not touch
    if not pm_bars:
        return None

    _opens, highs, lows, closes, _vols = _ohlcv(pm_bars)
    pm_high = float(np.max(highs))
    pm_low = float(np.min(lows))
    pm_last = float(closes[-1])

    no_reclaim = pm_high < daily_ema9
    if not no_reclaim:
        logger.info(
            f"  {watch.ticker}: Stage 2 FAIL — PM high ${pm_high:.2f} "
            f">= persisted 9 EMA ${daily_ema9:.2f} (reclaimed)"
        )
        return None

    below_all_pm = bool(np.all(closes < daily_ema9))
    prior_close = watch.close if watch.close else watch.prior_close
    gap_pct = (pm_last / prior_close) - 1.0 if prior_close else 0.0
    grade_a = no_reclaim and below_all_pm and gap_pct <= cfg.grade_a_gap_pct
    grade = "A" if grade_a else "B"

    stop = min(pm_high, daily_ema9)
    t1 = pm_last - cfg.t1_atr_fraction * watch.atr14
    t2 = pm_last - cfg.t2_atr_fraction * watch.atr14
    if watch.daily_ema65 is not None and watch.daily_ema65 < pm_last:
        # Further of ATR T2 vs 65 EMA (shorts: lower = more ambitious)
        t2 = min(t2, watch.daily_ema65)

    watch.stage2_qualified = True
    watch.grade = grade
    watch.pm_high = round(pm_high, 2)
    watch.pm_low = round(pm_low, 2)
    watch.pm_last = round(pm_last, 2)
    watch.gap_pct = round(gap_pct, 4)
    watch.stop = round(stop, 2)
    watch.t1 = round(t1, 2)
    watch.t2 = round(t2, 2)
    return watch


def format_stage1_alert(watch: RibbonWatch) -> RibbonAlert:
    atr_move = watch.atr14
    flags = []
    if watch.lower_high:
        flags.append("lower high")
    if watch.red_close:
        flags.append("red close")
    if watch.volume_fade:
        flags.append("volume fade")
    flag_txt = ", ".join(flags) if flags else "compressed into ribbon"
    copy = (
        f"WATCH {watch.ticker} — closed {watch.close:.2f}, daily 9 EMA "
        f"{watch.daily_ema9:.2f} ({watch.compress_atr:.2f} ATR). "
        f"{watch.extended_sessions} sessions extended, {flag_txt}. "
        f"ATR {watch.atr14:.2f} → 1σ move = ±{atr_move:.0f}. Check premarket 08:00."
    )
    return RibbonAlert(
        ticker=watch.ticker,
        setup=SETUP_WATCH,
        confidence=f"{watch.score}/{MAX_SCORE}",
        entry=copy,
        exit_plan=(
            "Do not enter. Decision is overnight. "
            "Stage 2 at 08:00 uses THIS 9 EMA — do not recompute it."
        ),
        pm_price=watch.close,
        ema_9_current=watch.daily_ema9,
        daily_ema9=watch.daily_ema9,
        daily_atr=watch.atr14,
        score=watch.score,
        extended_sessions=watch.extended_sessions,
        scan_date=watch.scan_date,
        stage="WATCH",
        below_9ema=watch.compress_atr <= 0.25,
    )


def format_stage2_alert(watch: RibbonWatch) -> RibbonAlert:
    pm = watch.pm_last or 0.0
    copy = (
        f"{watch.ticker} SHORT — Grade {watch.grade}. PM {pm:.1f}. "
        f"Daily 9 EMA {watch.daily_ema9:.2f} = invalidation. "
        f"PM high {watch.pm_high:.2f} = stop. "
        f"PM low {watch.pm_low:.2f} = trigger. "
        f"ATR {watch.atr14:.2f} → T1 {watch.t1:.1f}, T2 {watch.t2:.1f}. "
        f"Wait for 09:45."
    )
    return RibbonAlert(
        ticker=watch.ticker,
        setup=SETUP_SHORT,
        confidence=f"Grade {watch.grade} ({watch.score}/{MAX_SCORE} overnight)",
        entry=copy,
        exit_plan=(
            f"Stop ${watch.stop:.2f} (tighter of PM high / daily 9 EMA). "
            f"T1 ${watch.t1:.2f} → move to BE. T2 ${watch.t2:.2f}. "
            f"Do not enter before 09:45. Trail above 5-min 9 EMA."
        ),
        pm_price=pm,
        pm_high=watch.pm_high or 0.0,
        pm_low=watch.pm_low or 0.0,
        ema_9_current=watch.daily_ema9,
        daily_ema9=watch.daily_ema9,
        daily_atr=watch.atr14,
        stop_price=watch.stop,
        target_price=watch.t1,
        grade=watch.grade,
        score=watch.score,
        extended_sessions=watch.extended_sessions,
        below_9ema=True,
        supertrend_direction=f"Grade {watch.grade}",
        scan_date=watch.scan_date,
        stage="PREMARKET",
    )


# ─── Stage 3 execution engine ────────────────────────────────────────────

class RibbonExecEngine:
    """
    Feed RTH 5-min bars. 9 EMA is warmed with premarket closes so it exists
    at 09:45; session VWAP and ORL are RTH-only. daily_ema9 is persisted.
    """

    def __init__(self, cfg: RibbonBreakConfig, range_config=None):
        self.cfg = cfg
        self.range_config = range_config
        self.states: dict[str, RibbonExecState] = {}

    def reset_session(self):
        self.states.clear()

    def prime(self, watch: RibbonWatch, pm_bars: list[dict]) -> RibbonExecState:
        pm_closes = [float(b["c"]) for b in pm_bars] if pm_bars else []
        state = RibbonExecState(
            ticker=watch.ticker,
            daily_ema9=watch.daily_ema9,
            pm_high=watch.pm_high or 0.0,
            pm_low=watch.pm_low or 0.0,
            atr14=watch.atr14,
            planned_stop=watch.stop or watch.daily_ema9,
            planned_t1=watch.t1 or 0.0,
            planned_t2=watch.t2 or 0.0,
            grade=watch.grade,
            pm_closes=pm_closes,
        )
        self.states[watch.ticker] = state
        return state

    def process_rth_bar(
        self,
        ticker: str,
        open_price: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> Optional[RibbonEntryAlert]:
        state = self.states.get(ticker)
        if state is None or state.fired or state.reclaimed:
            return None

        if high >= state.daily_ema9:
            logger.info(
                f"  {ticker}: Stage 3 INVALIDATED — high ${high:.2f} "
                f"reclaimed persisted 9 EMA ${state.daily_ema9:.2f}"
            )
            state.reclaimed = True
            return None

        state.rth_opens.append(open_price)
        state.rth_highs.append(high)
        state.rth_lows.append(low)
        state.rth_closes.append(close)
        state.rth_volumes.append(volume)
        n = len(state.rth_closes)

        # Opening range from first N RTH bars. Do not fire before it completes.
        if n < self.cfg.opening_range_bars:
            return None
        if n == self.cfg.opening_range_bars or state.orl == 0.0:
            highs_arr = np.array(state.rth_highs, dtype=float)
            lows_arr = np.array(state.rth_lows, dtype=float)
            state.orh, state.orl = opening_range(
                highs_arr, lows_arr, self.cfg.opening_range_bars
            )

        if close >= state.orl:
            return None

        closes_for_ema = np.array(state.pm_closes + state.rth_closes, dtype=float)
        if len(closes_for_ema) < self.cfg.ema_period:
            return None
        ema9 = ema(closes_for_ema, self.cfg.ema_period)
        ema_val = float(ema9[-1])
        if np.isnan(ema_val):
            return None

        highs_arr = np.array(state.rth_highs, dtype=float)
        lows_arr = np.array(state.rth_lows, dtype=float)
        rth_closes = np.array(state.rth_closes, dtype=float)
        vols_arr = np.array(state.rth_volumes, dtype=float)
        vwap_vals = vwap(highs_arr, lows_arr, rth_closes, vols_arr)
        vwap_val = float(vwap_vals[-1])
        if np.isnan(vwap_val):
            return None
        if ema_val >= vwap_val:
            return None

        slope_lookback = self.cfg.slope_lookback
        slope_vals = ema_slope(ema9, slope_lookback)
        slope_val = float(slope_vals[-1]) if not np.isnan(slope_vals[-1]) else 0.0
        if np.isnan(slope_vals[-1]) or slope_val == 0:
            # Fall back to 1-bar slope once EMA has two valid prints
            if len(closes_for_ema) >= self.cfg.ema_period + 1:
                prev = float(ema9[-2])
                if not np.isnan(prev):
                    slope_val = ema_val - prev
        if slope_val >= 0:
            return None

        entry_price = close
        structural_stop = min(state.pm_high, state.daily_ema9) if state.pm_high else state.daily_ema9
        if structural_stop <= entry_price:
            structural_stop = max(state.orh, state.daily_ema9)

        risk = structural_stop - entry_price
        raw_target = entry_price - risk if risk > 0 else state.planned_t1

        lv = None
        if self.range_config is not None:
            lv = compute_levels(
                entry_price=entry_price,
                structural_stop_price=structural_stop,
                structural_target_price=raw_target,
                price=float(close),
                daily_atr=state.atr14,
                ticker=ticker,
                direction="short",
                config=self.range_config,
            )
            log_floor_decision(lv["log_entry"])
            if lv["skip"]:
                logger.info(f"  Suppressed {ticker} Stage 3: {lv['skip_reason']}")
                return None

        stop = lv["stop"] if lv else structural_stop
        target = lv["target"] if lv else raw_target
        stop_was_widened = lv["stop_was_widened"] if lv else False
        floor_source = lv["floor_source"] if lv else ""

        state.fired = True
        logger.info(
            f"  🔴 {ticker}: RIBBON BREAK ENTRY — close ${close:.2f} < ORL ${state.orl:.2f}"
        )
        return RibbonEntryAlert(
            ticker=ticker,
            message=(
                f"ORL break ${close:.2f} < ${state.orl:.2f}. "
                f"5-min 9 EMA ${ema_val:.2f} < VWAP ${vwap_val:.2f}, slope down. "
                f"Daily 9 EMA ${state.daily_ema9:.2f} not reclaimed."
            ),
            current_price=round(close, 2),
            orl=round(state.orl, 2),
            orh=round(state.orh, 2),
            ema_9=round(ema_val, 2),
            vwap_level=round(vwap_val, 2),
            ema_slope_val=round(slope_val, 4),
            daily_atr=state.atr14,
            daily_ema9=state.daily_ema9,
            entry_price=round(entry_price, 2),
            stop_price=round(stop, 2),
            target_1r=round(target, 2),
            exit_plan=(
                f"Stop ${stop:.2f}. T1 1R → BE. "
                f"T2 ${state.planned_t2:.2f}. Trail above 5-min 9 EMA. "
                f"Dead if price reclaims daily 9 EMA ${state.daily_ema9:.2f}."
            ),
            confidence=f"HIGH — Grade {state.grade} overnight, ORL + ribbon intact",
            stop_was_widened=stop_was_widened,
            floor_source=floor_source,
            grade=state.grade,
        )


# ─── Scanner (I/O) ───────────────────────────────────────────────────────

class RibbonBreakScanner:
    """Stage 1 + Stage 2 I/O wrapper. Stage 3 lives on RibbonExecEngine."""

    def __init__(self, config: ScannerConfig):
        self.config = config
        self.cfg = config.ribbon_break
        self.client = PolygonClient(config.api)
        self.watchlist_path = Path(config.ribbon_watchlist_file)
        self.exec_engine = RibbonExecEngine(self.cfg, config.range_floor)

    # ── persistence ──

    def _save_watchlist(self, watches: list[RibbonWatch], stage: int):
        payload = {
            "scan_date": watches[0].scan_date if watches else datetime.now().strftime("%Y-%m-%d"),
            "scan_time": datetime.now().isoformat(),
            "stage": stage,
            "count": len(watches),
            "names": [asdict(w) for w in watches],
        }
        self.watchlist_path.parent.mkdir(parents=True, exist_ok=True)
        self.watchlist_path.write_text(json.dumps(payload, indent=2))
        logger.info(f"Ribbon watchlist saved: {self.watchlist_path} ({len(watches)} names, stage {stage})")

    def load_watchlist(self) -> list[RibbonWatch]:
        if not self.watchlist_path.exists():
            return []
        try:
            data = json.loads(self.watchlist_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Ribbon watchlist unreadable: {e}")
            return []
        scan_date = data.get("scan_date", "")
        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        if scan_date not in (today, yesterday):
            logger.warning(f"Ribbon watchlist stale (from {scan_date})")
            return []
        out: list[RibbonWatch] = []
        for row in data.get("names", []):
            try:
                out.append(RibbonWatch(**row))
            except TypeError as e:
                logger.warning(f"Skipping malformed ribbon row: {e}")
        return out

    def stage2_qualified(self) -> list[RibbonWatch]:
        return [w for w in self.load_watchlist() if w.stage2_qualified]

    # ── Stage 1 ──

    def _spy_trend(self) -> tuple[Optional[float], Optional[float]]:
        try:
            bars = self.client.get_daily_bars("SPY", days_back=30)
            if not bars or len(bars) < 12:
                return None, None
            closes = np.array([float(b["c"]) for b in bars], dtype=float)
            e9 = ema(closes, self.cfg.ema_period)
            spy_close = float(closes[-1])
            spy_ema9 = float(e9[-1]) if not np.isnan(e9[-1]) else None
            return spy_close, spy_ema9
        except Exception as e:
            logger.warning(f"SPY trend fetch failed: {e}")
            return None, None

    def _merge_today_snapshot(self, ticker: str, daily: list[dict], today: str) -> list[dict]:
        """Append today's snapshot OHLC if daily aggs do not yet include today."""
        if daily:
            last_t = daily[-1].get("t")
            if last_t:
                last_date = bar_dt_et(int(last_t)).strftime("%Y-%m-%d")
                if last_date >= today:
                    return daily
        snap = self.client.get_snapshot(ticker)
        if not snap:
            return daily
        day = snap.get("day") or {}
        if not day.get("c"):
            return daily
        daily = list(daily)
        daily.append(
            {
                "t": int(datetime.now(tz=ET).timestamp() * 1000),
                "o": day.get("o", 0),
                "h": day.get("h", 0),
                "l": day.get("l", 0),
                "c": day.get("c", 0),
                "v": day.get("v", 0),
            }
        )
        return daily

    def run_stage1(self, tickers: Optional[list[str]] = None) -> list[RibbonWatch]:
        """
        Universe pass. Persist daily_ema9 on every hit.
        Run at 15:45 ET (planning) and again after the close (official refresh).
        """
        logger.info("═══ STAGE 1: RIBBON BREAK PROXIMITY WATCH ═══")
        today = datetime.now(tz=ET).strftime("%Y-%m-%d")
        spy_close, spy_ema9 = self._spy_trend()
        if spy_close is not None and spy_ema9 is not None:
            side = "flat-to-down" if spy_is_flat_to_down(spy_close, spy_ema9) else "UP"
            logger.info(f"  SPY {spy_close:.2f} vs 9 EMA {spy_ema9:.2f} → {side}")

        if tickers is None:
            ub = UniverseBuilder(self.client, self.config.universe)
            tickers = ub.build_universe(force=False)
        logger.info(f"  Scanning {len(tickers)} tickers")

        hits: list[RibbonWatch] = []
        for i, ticker in enumerate(tickers, 1):
            if i % 50 == 0:
                logger.info(f"  … {i}/{len(tickers)}")
            try:
                daily = self.client.get_daily_bars(ticker, days_back=self.cfg.daily_bars_lookback)
                daily = self._merge_today_snapshot(ticker, daily, today)
                watch = evaluate_stage1(
                    ticker, daily, self.cfg, today,
                    spy_close=spy_close, spy_ema9=spy_ema9,
                )
                if watch:
                    hits.append(watch)
                    logger.info(
                        f"  ✓ {ticker}: score {watch.score}/{MAX_SCORE}  "
                        f"close {watch.close} ema9 {watch.daily_ema9}  "
                        f"{watch.extended_sessions} sess  {watch.compress_atr:.3f} ATR"
                    )
            except Exception as e:
                logger.debug(f"  {ticker}: {e}")
                continue

        self._save_watchlist(hits, stage=1)
        logger.info(f"═══ STAGE 1 COMPLETE: {len(hits)} watch name(s) ═══")
        return hits

    # ── Stage 2 ──

    def run_stage2(self) -> list[RibbonAlert]:
        """Premarket confirm against persisted daily_ema9. Run 08:00 ET."""
        logger.info("═══ STAGE 2: RIBBON BREAK PREMARKET CONFIRM ═══")
        watches = self.load_watchlist()
        if not watches:
            logger.info("  No Stage 1 names. Run ribbon-watch first.")
            return []

        today = datetime.now(tz=ET).strftime("%Y-%m-%d")
        qualified: list[RibbonWatch] = []
        alerts: list[RibbonAlert] = []

        for watch in watches:
            logger.info(
                f"  {watch.ticker}: persisted daily 9 EMA ${watch.daily_ema9:.2f} "
                f"(from {watch.scan_date} — not recomputed)"
            )
            try:
                bars = self.client.get_intraday_bars(
                    watch.ticker, minutes=5, date=today, extended_hours=True
                )
                pm = filter_premarket_bars(bars or [])
                updated = evaluate_stage2(watch, pm, self.cfg)
                if updated:
                    qualified.append(updated)
                    alerts.append(format_stage2_alert(updated))
                    logger.info(
                        f"    🔴 Grade {updated.grade} — PM high {updated.pm_high} "
                        f"< ema9 {updated.daily_ema9}"
                    )
                else:
                    # Keep the name on the list but unmarked so we can see why
                    watch.stage2_qualified = False
                    qualified.append(watch)
            except Exception as e:
                logger.error(f"  {watch.ticker} Stage 2 error: {e}")
                qualified.append(watch)

        self._save_watchlist(qualified, stage=2)
        fired = [a for a in alerts]
        logger.info(f"═══ STAGE 2 COMPLETE: {len(fired)} Grade A/B short(s) ═══")
        return fired

    def stage1_alerts(self, watches: list[RibbonWatch]) -> list[RibbonAlert]:
        return [format_stage1_alert(w) for w in watches]


def watch_from_dict(row: dict) -> RibbonWatch:
    return RibbonWatch(**row)
