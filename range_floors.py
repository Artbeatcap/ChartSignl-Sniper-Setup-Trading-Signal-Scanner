"""
Range floors: structural stop cap, R:R gate, smart target floor, JSONL decision log.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional, Tuple

from config import RangeConfig

logger = logging.getLogger(__name__)


def _floor_decisions_path() -> str:
    p = os.getenv("RANGE_FLOOR_DECISIONS_LOG", "")
    if p:
        return p
    return os.path.join(os.getenv("DATA_DIR", "."), "floor_decisions.jsonl")


def get_price_tier_floor(
    price: float,
    config: RangeConfig,
    side: str = "stop",
) -> float:
    for ceiling, min_stop, min_target in config.price_tiers:
        if price <= ceiling:
            return min_stop if side == "stop" else min_target
    return config.price_tiers[-1][1 if side == "stop" else 2]


def get_atr_floor(
    daily_atr: Optional[float],
    config: RangeConfig,
    side: str = "stop",
) -> float:
    if daily_atr is None or daily_atr <= 0:
        return 0.0
    fraction = (
        config.atr_stop_fraction if side == "stop"
        else config.atr_target_fraction
    )
    return daily_atr * fraction


def get_index_etf_floor(
    price: float,
    config: RangeConfig,
    side: str = "stop",
) -> float:
    pct = (
        config.index_stop_min_pct if side == "stop"
        else config.index_target_min_pct
    )
    return price * pct


def apply_range_floor(
    structural_distance: float,
    price: float,
    daily_atr: Optional[float],
    ticker: str,
    config: RangeConfig,
    side: str = "stop",
) -> Tuple[float, str]:
    atr_floor = get_atr_floor(daily_atr, config, side)

    is_index = ticker.upper() in config.index_etf_tickers
    if is_index:
        tier_floor = get_index_etf_floor(price, config, side)
        tier_label = "Index % floor"
    else:
        tier_floor = get_price_tier_floor(price, config, side)
        tier_label = "Price-tier floor"

    if side == "target" and daily_atr and daily_atr > 0:
        threshold = daily_atr * config.target_floor_activation_threshold
        if structural_distance >= threshold:
            return structural_distance, "structural"

    if atr_floor >= tier_floor:
        floor = atr_floor
        if daily_atr is not None and daily_atr > 0:
            frac = (
                config.atr_stop_fraction if side == "stop"
                else config.atr_target_fraction
            )
            floor_label = f"ATR floor ({frac:.0%} of ${daily_atr:.2f})"
        else:
            floor_label = "ATR floor"
    else:
        floor = tier_floor
        floor_label = tier_label

    cap_pct = (
        config.max_stop_pct_of_price if side == "stop"
        else config.max_target_pct_of_price
    )
    cap = price * cap_pct
    if floor > cap:
        floor = cap
        floor_label += " (capped)"

    if structural_distance >= floor:
        return structural_distance, "structural"
    return floor, floor_label


def cap_structural_stop(
    structural_stop_dist: float,
    daily_atr: Optional[float],
    config: RangeConfig,
) -> Tuple[float, bool]:
    if daily_atr is None or daily_atr <= 0:
        return structural_stop_dist, False

    cap = daily_atr * config.max_stop_atr_multiple
    if structural_stop_dist > cap:
        return cap, True
    return structural_stop_dist, False


def compute_levels(
    entry_price: float,
    structural_stop_price: float,
    structural_target_price: float,
    price: float,
    daily_atr: Optional[float],
    ticker: str,
    direction: str = "short",
    config: Optional[RangeConfig] = None,
) -> dict:
    if config is None:
        config = RangeConfig()

    if direction == "short":
        raw_stop_dist = abs(structural_stop_price - entry_price)
        raw_target_dist = abs(entry_price - structural_target_price)
    else:
        raw_stop_dist = abs(entry_price - structural_stop_price)
        raw_target_dist = abs(structural_target_price - entry_price)

    capped_stop_dist, stop_was_capped = cap_structural_stop(
        raw_stop_dist, daily_atr, config
    )

    eff_stop_dist, stop_source = apply_range_floor(
        capped_stop_dist, price, daily_atr, ticker, config, "stop"
    )

    eff_target_dist, target_source = apply_range_floor(
        raw_target_dist, price, daily_atr, ticker, config, "target"
    )

    if direction == "short":
        adj_stop = entry_price + eff_stop_dist
        adj_target = entry_price - eff_target_dist
    else:
        adj_stop = entry_price - eff_stop_dist
        adj_target = entry_price + eff_target_dist

    rr = round(eff_target_dist / eff_stop_dist, 2) if eff_stop_dist > 0 else 0.0

    skip = False
    skip_reason = ""
    if rr < config.min_rr_ratio:
        skip = True
        skip_reason = (
            f"R:R {rr:.2f} below minimum {config.min_rr_ratio:.1f} "
            f"(stop ${eff_stop_dist:.2f} vs target ${eff_target_dist:.2f})"
        )

    stop_was_widened = eff_stop_dist > raw_stop_dist or stop_was_capped
    target_was_widened = eff_target_dist > raw_target_dist

    source_parts = []
    if stop_was_capped:
        source_parts.append(f"stop capped at {config.max_stop_atr_multiple}x ATR")
    if stop_source != "structural":
        source_parts.append(stop_source)
    floor_source = "; ".join(source_parts) if source_parts else "structural"

    log_entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker,
        "price": price,
        "direction": direction,
        "daily_atr": daily_atr,
        "entry": round(entry_price, 2),
        "raw_stop_dist": round(raw_stop_dist, 2),
        "raw_target_dist": round(raw_target_dist, 2),
        "stop_capped": stop_was_capped,
        "capped_stop_dist": round(capped_stop_dist, 2) if stop_was_capped else None,
        "eff_stop_dist": round(eff_stop_dist, 2),
        "eff_target_dist": round(eff_target_dist, 2),
        "stop_source": stop_source,
        "target_source": target_source,
        "rr": rr,
        "skipped": skip,
        "skip_reason": skip_reason if skip else None,
    }

    return {
        "entry": round(entry_price, 2),
        "stop": round(adj_stop, 2),
        "target": round(adj_target, 2),
        "stop_distance": round(eff_stop_dist, 2),
        "target_distance": round(eff_target_dist, 2),
        "stop_was_widened": stop_was_widened,
        "target_was_widened": target_was_widened,
        "floor_source": floor_source,
        "structural_stop_was_capped": stop_was_capped,
        "risk_reward": rr,
        "stop_pct": round(eff_stop_dist / entry_price * 100, 2) if entry_price else 0,
        "target_pct": round(eff_target_dist / entry_price * 100, 2) if entry_price else 0,
        "skip": skip,
        "skip_reason": skip_reason,
        "log_entry": log_entry,
    }


def log_floor_decision(log_entry: dict, log_path: Optional[str] = None) -> None:
    path = log_path or _floor_decisions_path()
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")
    except OSError as e:
        logger.warning("Failed to log floor decision: %s", e)
