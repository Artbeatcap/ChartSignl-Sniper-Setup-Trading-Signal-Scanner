"""
Setup 11 regression: PLTR Sep 1 signal bar / Sep 2 gap-through.

These tests reconstruct the miss. Stage 1 must fire Tuesday. Stage 2 must
use the persisted 9 EMA (179.91), not a recomputed mid-session value.
Recomputing after the Sep 2 dump yields ~177.26, which fails the PM-high
gate and deletes the trade — the bug this setup exists to close.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np

from config import RibbonBreakConfig
from indicators import ema
from scanner_ribbon_break import (
    RibbonExecEngine,
    consecutive_above_ema,
    evaluate_stage1,
    evaluate_stage2,
    filter_premarket_bars,
    filter_rth_bars,
    format_stage1_alert,
    format_stage2_alert,
)

FIX = Path(__file__).parent / "fixtures"
CFG = RibbonBreakConfig()


def _daily():
    return json.loads((FIX / "pltr_daily_to_sep1.json").read_text())


def _minutes():
    return json.loads((FIX / "pltr_5min_sep2.json").read_text())


class Stage1PltrTests(unittest.TestCase):
    def setUp(self):
        self.bars = _daily()
        self.watch = evaluate_stage1(
            "PLTR",
            self.bars,
            CFG,
            scan_date="2026-09-01",
            spy_close=761.78,
            spy_ema9=766.52,  # SPY flat-to-down on Sep 1
        )

    def test_fires_on_sep1_signal_bar(self):
        self.assertIsNotNone(self.watch, "PLTR Sep 1 must be a Stage 1 hit")
        self.assertEqual(self.watch.ticker, "PLTR")
        self.assertAlmostEqual(self.watch.close, 179.92, places=2)

    def test_persists_daily_ema9_at_179_91(self):
        self.assertAlmostEqual(self.watch.daily_ema9, 179.91, delta=0.02)

    def test_compressing_into_ribbon(self):
        self.assertLessEqual(self.watch.compress_atr, 0.25)
        self.assertLess(abs(self.watch.close - self.watch.daily_ema9), 0.05)

    def test_extended_at_least_four_sessions(self):
        self.assertGreaterEqual(self.watch.extended_sessions, 4)
        self.assertGreaterEqual(self.watch.max_extension_atr, 0.75)

    def test_confluence_score_at_least_four(self):
        self.assertGreaterEqual(self.watch.score, 4)
        self.assertTrue(self.watch.lower_high)
        self.assertTrue(self.watch.red_close)

    def test_watch_alert_copy_names_the_ema(self):
        alert = format_stage1_alert(self.watch)
        self.assertIn("WATCH PLTR", alert.entry)
        self.assertIn("179.91", alert.entry)
        self.assertIn("do not recompute", alert.exit_plan.lower())

    def test_does_not_fire_on_sep2_after_the_dump(self):
        # Append Sep 2's RTH dump as if someone scanned at the close.
        dumped = list(self.bars) + [{
            "t": 1788321600000, "o": 176.99, "h": 177.57, "l": 166.55,
            "c": 166.64, "v": 19_360_284,
        }]
        watch = evaluate_stage1("PLTR", dumped, CFG, scan_date="2026-09-02")
        self.assertIsNone(watch, "Sep 2 close is 1.5 ATR below the EMA — not compressing")


class Stage1RegimeTests(unittest.TestCase):
    def test_local_extension_overrides_bull_tape(self):
        bars = _daily()
        # Force a tiny extension by cloning a flat series near the EMA
        # Use real bars but claim SPY is strongly above its 9 EMA and
        # shrink the extension gate via a custom config that still
        # requires 4 sessions — PLTR's 1.29 ATR override should still pass.
        watch = evaluate_stage1(
            "PLTR", bars, CFG, "2026-09-01",
            spy_close=800.0, spy_ema9=760.0,  # SPY uptrend
        )
        self.assertIsNotNone(watch, "PLTR max extension > 1 ATR — override allows the short")
        self.assertTrue(watch.regime_override)

    def test_suppresses_when_spy_is_up_and_override_is_disabled(self):
        bars = _daily()
        cfg = RibbonBreakConfig(local_extension_override_atr=9.0)
        watch = evaluate_stage1(
            "PLTR", bars, cfg, "2026-09-01",
            spy_close=800.0, spy_ema9=760.0,
        )
        self.assertIsNone(watch, "bull tape + no local-extension override → suppress")


class Stage2PersistedEmaTests(unittest.TestCase):
    def setUp(self):
        self.watch = evaluate_stage1("PLTR", _daily(), CFG, "2026-09-01", 761.78, 766.52)
        self.assertIsNotNone(self.watch)
        self.pm = filter_premarket_bars(_minutes())

    def test_grade_a_against_persisted_ema(self):
        updated = evaluate_stage2(self.watch, self.pm, CFG)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.grade, "A")
        self.assertTrue(updated.stage2_qualified)
        self.assertLess(updated.pm_high, updated.daily_ema9)
        self.assertAlmostEqual(updated.daily_ema9, 179.91, delta=0.02)
        self.assertAlmostEqual(updated.pm_high, 179.0, delta=0.05)
        self.assertAlmostEqual(updated.pm_low, 175.70, delta=0.05)

    def test_does_not_mutate_persisted_ema(self):
        frozen = self.watch.daily_ema9
        evaluate_stage2(self.watch, self.pm, CFG)
        self.assertEqual(self.watch.daily_ema9, frozen)

    def test_recomputing_ema_after_the_dump_kills_the_setup(self):
        """The bug: feed Sep 2's close into the 9 EMA, then Stage 2 fails."""
        closes = np.array([b["c"] for b in _daily()] + [166.64], dtype=float)
        live_ema = float(ema(closes, 9)[-1])
        self.assertLess(live_ema, 178.0, "live EMA must have decayed toward the dump")

        # Simulate someone stuffing the live EMA into the watch
        broken = evaluate_stage1("PLTR", _daily(), CFG, "2026-09-01", 761.78, 766.52)
        broken.daily_ema9 = round(live_ema, 2)
        result = evaluate_stage2(broken, self.pm, CFG)
        self.assertIsNone(
            result,
            "recomputed EMA ~177.26 makes PM high 179 a 'reclaim' and deletes Grade A",
        )

    def test_stage2_alert_carries_tradeable_levels(self):
        updated = evaluate_stage2(self.watch, self.pm, CFG)
        alert = format_stage2_alert(updated)
        self.assertIn("Grade A", alert.entry)
        self.assertIn("Wait for 09:45", alert.entry)
        self.assertEqual(alert.daily_ema9, updated.daily_ema9)
        self.assertEqual(alert.stop_price, updated.stop)

    def test_pm_never_printed_above_persisted_ema(self):
        self.assertTrue(self.pm, "need premarket bars")
        for b in self.pm:
            self.assertLess(b["h"], 179.91)


class Stage3ExecutionTests(unittest.TestCase):
    def setUp(self):
        watch = evaluate_stage1("PLTR", _daily(), CFG, "2026-09-01", 761.78, 766.52)
        self.watch = evaluate_stage2(watch, filter_premarket_bars(_minutes()), CFG)
        self.assertIsNotNone(self.watch)
        self.engine = RibbonExecEngine(CFG)
        self.engine.prime(self.watch, filter_premarket_bars(_minutes()))

    def test_does_not_fire_before_opening_range_completes(self):
        rth = filter_rth_bars(_minutes())
        alert = None
        for bar in rth[:2]:
            alert = self.engine.process_rth_bar(
                "PLTR", bar["o"], bar["h"], bar["l"], bar["c"], bar["v"]
            )
        self.assertIsNone(alert)

    def test_invalidates_if_rth_reclaims_persisted_ema(self):
        rth = filter_rth_bars(_minutes())
        # Inject a reclaim print
        fake = dict(rth[0])
        fake["h"] = 180.50
        alert = self.engine.process_rth_bar(
            "PLTR", fake["o"], fake["h"], fake["l"], fake["c"], fake["v"]
        )
        self.assertIsNone(alert)
        self.assertTrue(self.engine.states["PLTR"].reclaimed)

    def test_orl_break_eventually_fires_on_sep2(self):
        rth = filter_rth_bars(_minutes())
        alert = None
        for bar in rth:
            result = self.engine.process_rth_bar(
                "PLTR", bar["o"], bar["h"], bar["l"], bar["c"], bar["v"]
            )
            if result:
                alert = result
                break
        self.assertIsNotNone(alert, "PLTR Sep 2 must produce a Stage 3 ORL-break entry")
        self.assertLess(alert.entry_price, alert.orl)
        self.assertLess(alert.ema_9, alert.vwap_level)
        self.assertEqual(alert.daily_ema9, self.watch.daily_ema9)
        self.assertTrue(alert.entry_price < alert.stop_price)
        self.assertTrue(alert.target_1r < alert.entry_price, "short T1 must be below entry")


class HelperTests(unittest.TestCase):
    def test_consecutive_above_ema_stops_at_first_close_below(self):
        closes = np.array([10.0, 11.0, 12.0, 11.5, 10.0], dtype=float)
        ema9 = np.array([9.0, 9.5, 10.0, 11.6, 10.5], dtype=float)
        n, max_dist = consecutive_above_ema(closes, ema9)
        self.assertEqual(n, 0)  # last close 10.0 < 10.5

    def test_premarket_filter_is_0400_to_0930_et(self):
        bars = _minutes()
        pm = filter_premarket_bars(bars)
        rth = filter_rth_bars(bars)
        self.assertTrue(pm)
        self.assertTrue(rth)
        pm_ts = {b["t"] for b in pm}
        rth_ts = {b["t"] for b in rth}
        self.assertFalse(pm_ts & rth_ts)


if __name__ == "__main__":
    unittest.main()
