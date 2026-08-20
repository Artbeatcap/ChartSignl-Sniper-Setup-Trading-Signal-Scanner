# Catalyst Re-Rating Log

**Purpose:** a registry of the rare sessions where price discovers a *new equilibrium* instead of mean-reverting — so the next time one happens you can answer the only question that matters, fast: **fade it, or leave it alone?**

This is not a "missed trades" scrapbook. It's a labeled dataset. Every Setup Sniper short (1, 3, 5, 9) is a bet on mean reversion. These events are the population where that bet loses. Knowing the difference on the *morning of* is worth more than any new setup.

**Last updated:** 2026-08-20 · **Events logged:** 6 (5 resolved, 1 open) · Machine-readable copy: `catalyst_events.json`

---

## The log

| Event | Catalyst | Gap % | Day % | Close-in-range | RVOL | T+5 | T+20 | Max DD | Gap filled? | Verdict |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|
| **MRNA** 2026-08-19 | Phase 3 mRNA cancer vaccine | +84.3 | **+177.0** | **96%** | **37.1×** | — | — | — | *open* | **unresolved** |
| **NVDA** 2023-05-25 | AI datacenter guidance shock | +26.1 | +24.4 | 47% | 3.4× | +3.5 | +7.0 | −1.6 | never | re-rating |
| **ORCL** 2025-09-10 | $455B RPO / OpenAI backlog | +32.2 | +35.9 | 48% | 5.3× | −8.2 | −12.1 | **−17.5** | never | re-rating **that faded** |
| **AMD** 2025-10-06 | OpenAI 6GW deal + warrants | +37.5 | +23.7 | **3%** | 5.2× | +6.2 | **+27.5** | +2.7 | never | re-rating |
| **VKTX** 2024-02-27 | VK2735 Phase 2 obesity data | +81.3 | +121.0 | 85% | 17.9× | +3.4 | −5.0 | −29.1 | never | re-rating |
| **GME** 2024-05-14 | Roaring Kitty return | +112.9 | +60.1 | 44% | 17.2× | **−54.6** | **−47.8** | **−63.7** | **D+2** | **pump** — fade |

*All figures computed from actual Massive daily bars. "Gap filled" = a subsequent low traded back to the pre-event close. Max DD is from the event-day close over the following 20 sessions.*

---

## The finding: day-1 price action does not classify these

This is the part that should change how you scan. Every intuitive fade trigger **failed** as a discriminator:

- **Close-in-range?** AMD closed at **3% of range** — the single most bearish close in the log — and was **+27% twenty sessions later.** GME closed at a healthy 44% and lost 64%.
- **Relative volume?** VKTX (17.9×) and GME (17.2×) are nearly identical. One held, one collapsed.
- **Gap size?** GME's 113% gap was the largest. So was its failure.
- **Day size?** No relationship whatsoever.

**The one feature that separated 5/5 resolved cases: the gap never filled.**

> A genuine re-rating never trades back to its pre-event close. A pump does — within about 2–10 sessions.

ORCL fell 17.5% and VKTX fell 29%, and *neither* came close to filling. GME filled on day 2 and kept going.

The reason is not technical, it's economic. A re-rating means analysts have to **rebuild the model** — new revenue, new probability of success, new terminal value. That new price is real and it defends itself. A squeeze changes nothing about the business, so there's no floor under it once the flow stops.

### The permanence test (run this in 30 seconds, premarket)

1. Does the catalyst change **forward cash flows**, or the probability of them? *(Phase 3 win, signed multi-year contract, guidance reset → yes)*
2. Is the move reversible by **sentiment alone**? *(Influencer, meme, squeeze, temporary supply squeeze → no permanence)*
3. Would a sell-side analyst have to **raise their model** — or only their price target "on momentum"? Model = re-rating. Target-only = pump.

Two or three yeses: don't fade the gap. Zero: this is Setup 1 / Setup 3 territory and you should be hunting it.

---

## What this changes in Setup Sniper

### 1. Day 1 is a LONG. The shorts don't start until Day 2.

This is the structural rule these events sit inside: **there is no day-1 short on a catalyst gap.** Setups 1 and 3 are day-2+ trades. Day 1 is a long, and the exit is deliberately *not* the 9 EMA — it's held far from it, on time or on exhaustion, because the 9 EMA on a day-1 parabolic shakes you out of the move you were right about.

Simulated from the 9:30 open on the two extreme days:

| Exit rule | MRNA 2026-08-19 | AMD 2025-10-06 |
|---|---:|---:|
| Entry (9:30 open) | 116.02 | 226.44 |
| **MAE, first 30 min** | **−1.34%** | **−9.16%** |
| Time-based +15 min | +11.60% | −5.54% |
| **Time-based +30 min** | **+21.15%** | −7.52% |
| Time-based +60 min | +36.50% | −8.11% |
| 5-min 9 EMA trail | +28.86% *(out 10:40)* | −3.63% *(out 9:35)* |
| Hold to close | **+50.79%** | −10.07% |
| Perfect (session high) | +52.27% | +0.12% |

The 30-minute exit alone was **+21%** on MRNA. Holding to the bell was **+51%**.

### 2. The same 10:00 AM tape read that says "don't short" says "go long"

The filter works in both directions, which is what makes it trustworthy rather than curve-fit:

| At 10:00 AM | MRNA | AMD |
|---|---|---|
| First 5-min close below the 9 EMA | **10:40** (held 70 min) | **9:35** (failed instantly) |
| Extension above 5-min 9 EMA at 10:00 | **+8.1%** | **−2.3%** |
| Max extension all session | +12.0% | +1.7% |
| Verdict | gap **defended** → long is live | gap **failed** → no trade |

> **If price is holding above the 5-min 9 EMA at 10:00 and extended several percent above it, the gap is being defended — that's the long.**
> **If the 5-min 9 EMA breaks in the first 15 minutes, there is no trade in either direction.**

AMD is the discipline case, not a missed long: entering it at the open cost −9.2% within 30 minutes. Same catalyst class, same gap size — completely different tape.

### 3. On the exit: the 9 EMA is a validator, not a target

On MRNA the 9 EMA trail exited at 10:40 @ 149.50 for +28.9% — and price then dropped to **136.13 (−8.9%)** before running to **176.66 (+18.2% above the exit)**. So the trail wasn't noise; it caught a real flush. But taking it cost you 22 points, and re-entering after an 8.9% drawdown is exactly the situation impatience loses.

On AMD the same trail exited in **five minutes** for −3.63%, versus −10.07% holding to the close. It was the single best outcome available.

**The structure that fits both:** use the 5-min 9 EMA as the *early validator* — if it breaks inside the first 15–30 minutes, you're out cheap and the thesis is dead. Once the position is extended (MRNA was +8% above the 9 EMA by 10:00), switch to the loose exit — 30–60 minutes, or a climax bar — and stop watching the 9 EMA entirely.

*Note on the exhaustion rule:* a naive climax definition (top-decile volume + >50% upper wick) **misfires on the 9:30 opening bar**, which had a 63% wick on huge volume and would have exited you at +1.17%. Any exhaustion exit needs to exclude the first 2–3 bars of the session.

### 4. The multi-day fade is a different trade and needs a different target

If you ever *do* hold one of these overnight (Setup 3's 1–3 day follow-up window), the daily 9 EMA is unreachable — MRNA's sits at $61.76 against a $174.38 close. Use the **top of the gap zone** as the swing target instead. ORCL is the proof: −17.5% over three weeks, and it never got within 12% of filling. Also note VKTX **peaked on D+1**, so the swing entry is D+2, not the day-1 close.

### 5. There is a data-source hole — closed without a paid feed

Three of six catalysts here — MRNA, AMD, VKTX — were **unscheduled**: a clinical readout, a strategic deal, a Phase 2 print. `scanner_premarket_fresh.py` hooks an earnings calendar, which sees none of them.

A news gate built on Massive `get_ticker_news` would have **blocked the single best signal in this log.** On MRNA 2026-08-19:

| Published (ET) | Source | Headline |
|---|---|---|
| **12:34 PM** | The Motley Fool | "Moderna and Merck Just Made History…" |
| 8:07 PM | The Motley Fool | "Why Moderna Stock Skyrocketed Today" |

**Nothing before the open. The press release never appears in that feed at all.** The stock moved at 6:45 AM; the first article is **5h49m late** and is commentary, not the primary source.

**Do not spend $139 on a paid wire.** Because `CatalystResolver` latches from 04:00 ET, the real requirement is *"resolve within ~3 hours of the print"* — catalyst at 6:45, decision bar at 9:45. Sub-second latency buys nothing you can act on.

| Feed | Latency | Clears 9:45? | Cost |
|---|---|---|---|
| `AlpacaNewsFeed` — websocket, **Benzinga-sourced** | seconds | yes | **free** |
| `EdgarCurrentFeed` — SEC 8-K atom, `Accepted:` to the second | 30–120 min | yes | free |
| `WireRSSFeed` — per-company wire / IR RSS | seconds | yes | free (optional fallback) |
| `MassiveNewsFeed` | **~349 min** | no | included |

Alpaca streams the same **Benzinga** feed paid services repackage, over `wss://stream.data.alpaca.markets/v1beta1/news`, on the free Basic plan. It also exposes **historical news back to 2015** over REST, so the gate can be backtested against `catalyst_events.json`. Massive cannot do that; its endpoint has no date parameter.

v1 does **not** run a 04:00 websocket daemon. REST at 08:30 / 09:45 still clears 180 minutes of slack. `EdgarCurrentFeed` stays wired as the free spare. Alpaca has described the news API as free "during the beta period" — verify entitlement on your own key.

Implementation notes: EDGAR **full-text search (`efts.sec.gov`) is date-granular only** and is useless intraday. SEC requires a real contact in the `User-Agent` header, rate-limited to 10 req/s.

**Plus a fourth state.** When nothing resolves by the deadline, emit `unconfirmed` — a watch alert saying *"gap + volume qualified, no catalyst found, check the wire yourself."*

Replay historical latency:

```bash
python main.py catalyst-backfill
```

Requires a free Alpaca paper key (`APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`). Without keys the command skips cleanly.

---

## Setup 11 — Day-1 Catalyst Gap Long (spec: `day1_catalyst_long.py`)

Gates run cheapest-first and fail fast. **The news gate is hard: no confirmed material announcement, no signal.**

| # | Gate | Threshold |
|---|---|---|
| 1 | Gap | ≥ +20% vs prior close |
| 2 | RVOL | ≥ 10× the 20-day average |
| 3 | **News** | **primary-source material announcement within 18h** |
| 4 | Technical @ 09:45 | above the 5-min 9 EMA, no break before 09:45, extension ≥ 3% |

Catalyst classes that clear the gate: `clinical_readout`, `regulatory_approval`, `guidance_shock`, `strategic_deal`, `m_and_a`. Auto-rejected: `sentiment_squeeze`, `analyst_action`, `offering` — those are Setup 1/3 fade territory, not longs.

Setup 11 code exists so the gates can be measured. **It is not a bread-and-butter long.** Six observations is not an edge. SIGNAL output is labeled research-only until 20+ logged events. The immediate, defensible value is the opposite: knowing when **not** to short.

Morning-fresh overlay: names gapping ≥ 20% get a one-shot news resolve. Confirmed primary catalyst **suppresses the Setup 5 fade tag** and retags as `SETUP 11: Catalyst re-rating WATCH — do not fade`.

### Verified gate behaviour

| Case | Result |
|---|---|
| MRNA 8/19, primary wire | **SIGNAL** (research) — entry 129.47, ext +5.2%, RVOL 11.2× |
| AMD 10/6, primary wire, RVOL passing | technical gate — *9 EMA broke at 09:35, gap not defended* |
| MRNA tape + same-day meme headline | news gate `rejected` — *sentiment, not cash flows* |
| MRNA tape + Motley Fool only | news gate `unconfirmed` — *confirm against the primary wire* |

### What the trigger actually pays

Entry is the **09:45 close at 129.47**, not the 116.02 open — waiting for confirmation costs 11.6% of the move. That's the price of not guessing:

| Exit | From 129.47 |
|---|---:|
| +30 min | **+18.93%** |
| +60 min | +14.75% |
| +90 min | +5.97% |
| Hold to close | **+35.12%** |
| Worst drawdown after entry | **−4.85%** |

---

## Adding the next one

Append to `catalyst_events.json`. Minimum viable entry, logged the evening of:

```
ticker, event_date, catalyst {type, headline, time_et, scheduled, permanence}
tape   {prev_close, open, high, low, close, volume, avg_vol_20d}
metrics{gap_pct, day_pct, close_in_range_pct, rvol, atr_multiple, x_daily_9ema}
classification: "unresolved"
```

Then revisit at **T+20** and set `gap_filled_on_day`, `event_low_broken_on_day`, and the final `classification`. Two fields, one follow-up, and the dataset compounds.

**Catalyst types to track:** `clinical_readout` · `guidance_shock` · `strategic_deal` · `m_and_a` · `regulatory_approval` · `sentiment_squeeze` (the control group)

---

## Standing rule

You do not currently have a codified long setup for these, and this log is **not** an argument for bolting one on as a playbook trade. Six observations is not an edge. The immediate, defensible value is the opposite: knowing when **not** to short. That protects capital in the exact situation where your playbook's reflex — *"parabolic, 31× ATR, 2.8× above the 9 EMA, fade it"* — is most confident and most wrong.

Revisit the question of a live long setup at 20+ logged events.

---

## Correction on the MSFT/OpenAI memory

Worth being precise, because the analogy is driving the mental model. **MSFT never went parabolic on the OpenAI investment.** The $10B announcement landed 2023-01-23; MSFT closed **+1.0%** that day, then **−0.2%** through earnings the next session. Its best day in that entire stretch was 2023-02-02, **+4.7%**.

The megacap moves that actually match what you're remembering are **ORCL +35.9%** and **AMD +23.7%** — both OpenAI-driven, both in late 2025, both in this log. That's the right reference class. MSFT was a slow multi-quarter re-rating, not an event.
