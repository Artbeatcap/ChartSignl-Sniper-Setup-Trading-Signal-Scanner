# Setup 11 — Extended Name Loses Daily 9 EMA (Ribbon Break Short)

**Status:** Implemented 2026-09-02 after a missed PLTR short.
**Code:** `scanner_ribbon_break.py` (alias: `ribbon_break_scan.py`)
**CLI:** `python main.py ribbon-watch` (Stage 1) · `python main.py morning` (Stage 2) · `python main.py intraday` (Stage 3)
**Priority:** Phase 1.5 — highest-frequency setup in the book; the only one that fires on names with no earnings/parabolic/VIX catalyst.

---

## Why this setup exists

The 10 existing setups all require an *event*: an 80% runner, an earnings miss, a 50%/5-day parabolic, a VIX spike. PLTR on 2026-09-02 had none of them, and dropped ~7.4% (1.5× ATR) in a straight line off the daily 9 EMA. **The scanner as specced would not have produced a single alert on it.** This setup closes that gap.

### The PLTR forensics (the case that motivated it)

| Date | Close | Daily 9 EMA | Distance |
|---|---|---|---|
| Aug 27 | 185.93 | 176.30 | +9.63 |
| Aug 28 | 186.29 | 178.30 | +7.99 |
| Aug 31 | 186.38 | 179.91 | +6.47 |
| **Sep 1** | **179.92** | **179.91** | **+0.01** ← the signal bar |
| Sep 2 | 166.64 | 177.26 | −10.62 |

- Sep 2 **opened at 176.99** — already 2.92 below the 9 EMA at the bell.
- Premarket 04:00–09:30 ET traded 175.70–179.00. **It never touched 179.91.** Not one print above the daily 9 EMA all session.
- Daily range ~11 vs ATR(14) ≈ 7.44 → a 1.5× ATR expansion day.

### The structural reason the trade was un-enterable as planned

"Enter once price falls below the daily 9 EMA" is a **once-per-day, close-updated value**. When a name gaps through it, the trigger event and the entry are the same instant — and by then price is already 3–6% past the level. There was no intraday cross to watch for on Sep 2, because the cross happened between 4:00 PM Tuesday and 4:00 AM Wednesday. Watching the tape on Wednesday morning for a "break of the 9 EMA" was watching for something that had already happened.

**The rule has to be split into a watch trigger and an execution trigger.**

---

## Three-stage alert cadence

### Stage 1 — PROXIMITY WATCH (fires 15:45 ET, day before)

Scan universe for names that have been extended and are now compressing into the ribbon.

```
extended     = close > daily_ema9 for >= 4 consecutive sessions
              AND max(close - daily_ema9) over those sessions >= 0.75 * ATR14
compressing  = abs(today_close - daily_ema9) <= 0.25 * ATR14
lower_high   = today_high < yesterday_high
volume_fade  = today_volume < mean(volume[-5:])      # confirms, does not gate
score        = base 3, +1 lower_high, +1 volume_fade, +1 if close < open
FIRE if extended AND compressing AND score >= 4
```

PLTR on Sep 1: extended (5 sessions after a one-bar dip on Aug 25, max distance 9.63 = 1.29 ATR ✓), compressing (0.01 = 0.001 ATR ✓✓), lower high (186.55 < 187.94 ✓), red bar (179.92 < 182.98 ✓), volume fade ✓. **Score 6/6. This screams at 3:45 PM Tuesday.**

Alert copy: `WATCH PLTR — closed 179.92, daily 9 EMA 179.91 (0.00 ATR). 5 sessions extended, lower high, red close. ATR 7.44 → 1σ move = ±7. Check premarket 08:00.`

### Stage 2 — PREMARKET CONFIRM (fires 08:00 ET)

For every Stage 1 name from yesterday:

```
pm_high        = max(premarket highs since 04:00)
no_reclaim     = pm_high < daily_ema9              # hard gate
below_all_pm   = every 5-min PM close < daily_ema9  # strength grade
gap_pct        = (pm_last / prior_close) - 1
FIRE if no_reclaim
GRADE A if no_reclaim AND below_all_pm AND gap_pct <= -0.005
```

**`daily_ema9` is the value persisted from Stage 1. Never recompute it.** Recomputing on Sep 2 after the open yields ~177.26; then `pm_high 179.00 < 177.26` fails and the Grade A short vanishes — the exact gap-through bug.

PLTR: pm_high 179.00 < 179.91 ✓, every PM bar below ✓, gap −1.6% ✓ → **Grade A short.**

Alert copy carries the levels you actually trade off:
`PLTR SHORT — Grade A. PM 177.2. Daily 9 EMA 179.91 = invalidation. PM high 179.00 = stop. PM low 175.70 = trigger. ATR 7.44 → T1 173.5, T2 170.0. Wait for 09:45.`

### Stage 3 — EXECUTION TRIGGER (fires 09:45 ET onward)

Do not fire before 09:45 — this is the patience guardrail.

```
FIRE when:
  5min_close < opening_range_low(09:30-09:45)
  AND 5min_ema9 < session_vwap AND ema9 sloping down
  AND price has not reclaimed daily_ema9 at any point
```

5-min 9 EMA is warmed with premarket bars so it exists at 09:45. Session VWAP and ORL are RTH-only. Entry ~ OR low break. Stop = PM high or daily 9 EMA, whichever is tighter. T1 = 1R → move to breakeven. T2 = daily 65 EMA or measured move. Trail above 5-min 9 EMA.

---

## Guardrails

- **Regime filter:** suppress unless SPY daily trend is flat-to-down, or the name is >1 ATR extended on its own. (No countertrend shorting a healthy tape without local extension.)
- **ATR in every alert.**
- **Options vs shares:** no earnings → no IV crush → puts are viable. 1–2 weeks out, delta ~0.40. Shares if you want to trail past T2.
- **The 09:45 delay is the whole point.** Stage 1 decides the trade the night before; Stage 3 executes it mechanically.

---

## Build notes

- Stage 1: Railway/cron at 15:45 ET (`python main.py ribbon-watch`) plus a nightly refresh after the official close. Cheap — daily bars, no websocket.
- Stage 2 reuses the morning pass. The only new input is yesterday's `daily_ema9`, persisted on `watchlist_ribbon_break.json`.
- Stage 3 is gated on the Stage 2 watchlist inside the existing intraday engine.
- Backtest ask before trusting it: on the top-1000 universe, what is the 1-day forward return distribution for names that meet the Stage 1 condition? Need n ≥ 30, in-sample/out-of-sample split, and a base-rate comparison against "any extended name" so the compression condition is shown to add something.
