"""
Morning regime job — fetch SPY + VIX, classify once, write data/regime_today.json.
Run before 8:00 AM morning check (cron 7:45 AM ET).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from config import ScannerConfig
from conviction_score import Direction
from indicators import ema, supertrend
from polygon_client import PolygonClient
from regime_classifier import RegimeClassifier, RegimeInputs

logger = logging.getLogger(__name__)


def _bars_to_arrays(bars: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    highs = np.array([float(b["h"]) for b in bars], dtype=float)
    lows = np.array([float(b["l"]) for b in bars], dtype=float)
    closes = np.array([float(b["c"]) for b in bars], dtype=float)
    return highs, lows, closes


def _vix_snapshot_and_bars(client: PolygonClient) -> tuple[float, float, float]:
    """Current VIX, 14d fractional change, 3d fractional change (e.g. 0.30 = +30%)."""
    vix_snap = client.get_snapshot("VIX:VIX") or client.get_snapshot("I:VIX")
    if not vix_snap:
        return 0.0, 0.0, 0.0
    day_data = vix_snap.get("day", {})
    vix_current = float(day_data.get("c") or day_data.get("l") or 0.0)
    if vix_current <= 0:
        return 0.0, 0.0, 0.0

    vix_bars = client.get_daily_bars("VIX:VIX", days_back=22)
    if not vix_bars or len(vix_bars) < 5:
        vix_bars = client.get_daily_bars("I:VIX", days_back=22)
    if not vix_bars or len(vix_bars) < 5:
        return vix_current, 0.0, 0.0

    closes = np.array([float(b["c"]) for b in vix_bars], dtype=float)
    lookback_14 = min(14, len(closes) - 1)
    vix_14d_ago = float(closes[-(lookback_14 + 1)])
    pct_14d = ((vix_current - vix_14d_ago) / vix_14d_ago) if vix_14d_ago > 0 else 0.0

    lookback_3 = min(3, len(closes) - 1)
    vix_3d_ago = float(closes[-(lookback_3 + 1)])
    pct_3d = ((vix_current - vix_3d_ago) / vix_3d_ago) if vix_3d_ago > 0 else 0.0

    return vix_current, pct_14d, pct_3d


def run_regime(config: ScannerConfig) -> Path:
    tz = ZoneInfo(config.timezone)
    now_et = datetime.now(tz)

    client = PolygonClient(config.api)
    bars = client.get_daily_bars("SPY", days_back=280)
    if not bars or len(bars) < 210:
        raise RuntimeError(f"Insufficient SPY daily bars: {len(bars or [])}")

    highs, lows, closes = _bars_to_arrays(bars)
    spy_close = float(closes[-1])

    e9 = ema(closes, 9)
    e65 = ema(closes, 65)
    e200 = ema(closes, 200)
    spy_ema_9 = float(e9[-1])
    spy_ema_65 = float(e65[-1])
    spy_ema_200 = float(e200[-1])

    st = config.runner_fade.supertrend
    _, st_dir = supertrend(highs, lows, closes, period=st.period, multiplier=st.multiplier)
    last_st = float(st_dir[-1])
    spy_st = Direction.LONG if last_st >= 0 else Direction.SHORT

    spy_20d = 0.0
    if len(closes) >= 21:
        spy_20d = float(closes[-1] / closes[-21] - 1.0)

    vix_current, vix_14d_frac, vix_3d_frac = _vix_snapshot_and_bars(client)

    inputs = RegimeInputs(
        spy_close=spy_close,
        spy_ema_9_daily=spy_ema_9,
        spy_ema_65_daily=spy_ema_65,
        spy_ema_200_daily=spy_ema_200,
        spy_supertrend_direction=spy_st,
        spy_20d_pct_change=spy_20d,
        vix_current=vix_current,
        vix_pct_change_14d=vix_14d_frac,
        vix_pct_change_3d=vix_3d_frac,
    )

    classifier = RegimeClassifier()
    decision = classifier.classify(inputs, now_et)

    data_dir = Path(os.getenv("DATA_DIR", "."))
    out_path = data_dir / "regime_today.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "regime": decision.regime.value,
        "confidence": decision.confidence,
        "timestamp": decision.timestamp.isoformat(),
        "bias": decision.bias,
        "reasons": decision.reasons,
        "signals": {k: (v if isinstance(v, (int, float, str, bool)) else str(v)) for k, v in decision.signals.items()},
        "confidence_grade": decision.confidence_grade,
        "is_transition": decision.is_transition,
        "previous_regime": decision.previous_regime.value if decision.previous_regime else None,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info(f"Regime written: {out_path} → {decision.regime.value}")

    print(decision.format_morning_brief())
    return out_path
