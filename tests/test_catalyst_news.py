"""Classifier, wire-vs-editorial, resolver latch, Setup 11 verified cases."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from catalyst_news import (
    CONFIRMING,
    AlpacaNewsFeed,
    CatalystNews,
    CatalystResolver,
    NewsGateResult,
    _is_wire,
    classify,
    load_catalyst_events,
)
from day1_catalyst_long import (
    WATCH_TAG,
    Day1CatalystLong,
    apply_news_overlay,
    evaluate_technical_gate,
)
from scanner_premarket_fresh import PremktMover

ET = ZoneInfo("America/New_York")
MRNA_DAY = datetime(2026, 8, 19, tzinfo=ET)
AMD_DAY = datetime(2025, 10, 6, tzinfo=ET)


def _news(**kw) -> CatalystNews:
    defaults = dict(
        headline="x",
        published_utc=datetime(2026, 8, 19, 10, 45, tzinfo=timezone.utc),
        source="wire",
        catalyst_type="clinical_readout",
        is_primary_source=True,
        url="",
        ticker="MRNA",
    )
    defaults.update(kw)
    if "catalyst_type" not in kw:
        defaults["catalyst_type"] = classify(defaults["headline"])
    return CatalystNews(**defaults)


class FakeFeed:
    def __init__(self, name, items=None, error=None, is_primary=True):
        self.name = name
        self.items = list(items or [])
        self.error = error
        self.is_primary = is_primary

    async def poll(self, tickers, since):
        if self.error:
            raise self.error
        want = {t.upper() for t in tickers}
        return [
            n for n in self.items
            if n.ticker in want and n.published_utc >= since
        ]


def _ramp(day: datetime, start_px: float, end_px: float):
    bars = []
    t = day.replace(hour=4, minute=0, second=0, microsecond=0)
    end = day.replace(hour=9, minute=25)
    times = []
    while t <= end:
        times.append(t)
        t = t + timedelta(minutes=5)
    for i, ts in enumerate(times):
        frac = i / max(len(times) - 1, 1)
        px = start_px + (end_px - start_px) * frac
        bars.append({"t": ts, "o": px, "h": px + 0.4, "l": px - 0.4, "c": px, "v": 400_000})
    return bars


def _rth(day: datetime, hh: int, mm: int, close: float):
    ts = day.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return {"t": ts, "o": close, "h": close + 0.8, "l": close - 0.8, "c": close, "v": 2_000_000}


def mrna_defended_bars():
    bars = _ramp(MRNA_DAY, 80.0, 116.02)
    bars.extend([
        _rth(MRNA_DAY, 9, 30, 116.02),
        _rth(MRNA_DAY, 9, 35, 120.50),
        _rth(MRNA_DAY, 9, 40, 125.10),
        _rth(MRNA_DAY, 9, 45, 129.47),
    ])
    return bars


def amd_broke_bars():
    bars = _ramp(AMD_DAY, 200.0, 226.44)
    bars.extend([
        _rth(AMD_DAY, 9, 30, 226.44),
        _rth(AMD_DAY, 9, 35, 205.00),
        _rth(AMD_DAY, 9, 40, 204.50),
        _rth(AMD_DAY, 9, 45, 203.80),
    ])
    return bars


def mrna_primary() -> CatalystNews:
    return _news(
        headline=(
            "Moderna and Merck Announce Phase 3 Topline Results for mRNA "
            "Cancer Vaccine That Met the Primary Endpoint"
        ),
        source="GlobeNewswire",
        is_primary_source=True,
        ticker="MRNA",
        url="https://www.globenewswire.com/news-release/mrna-phase3",
    )


def confirmed(news: CatalystNews) -> NewsGateResult:
    return NewsGateResult(
        passed=True, tier="confirmed",
        reason=f"{news.catalyst_type} confirmed via {news.source}",
        catalyst=news, feeds_tried=["alpaca_news", "edgar_8k"],
    )


class ClassifierTests(unittest.TestCase):
    CASES = [
        (
            "Moderna Phase 3 trial met the primary endpoint",
            "clinical_readout",
        ),
        (
            "FDA Approves Novel Cancer Therapy",
            "regulatory_approval",
        ),
        (
            "NVIDIA Raises Full-Year Guidance on Datacenter Demand",
            "guidance_shock",
        ),
        (
            "AMD Announces Multi-Year Agreement with OpenAI for 6 Gigawatt Compute Deal",
            "strategic_deal",
        ),
        (
            "Acme Corp Announces Definitive Agreement to Acquire Rival Inc",
            "m_and_a",
        ),
        (
            "Roaring Kitty returns as GME meme stock rips",
            "sentiment_squeeze",
        ),
        (
            "Analyst upgrades NVDA and raises price target to $200",
            "analyst_action",
        ),
        (
            "Company Announces Pricing of $500 Million Public Offering",
            "offering",
        ),
        (
            "FDA Grants Breakthrough Therapy Designation",
            "regulatory_approval",
        ),
        (
            "Oracle reports record RPO and backlog",
            "guidance_shock",
        ),
    ]

    def test_ten_of_ten_real_headlines(self):
        self.assertEqual(len(self.CASES), 10)
        for headline, expected in self.CASES:
            with self.subTest(headline=headline):
                self.assertEqual(classify(headline), expected)


class WireDetectionTests(unittest.TestCase):
    def test_globenewswire_release_is_primary(self):
        msg = {
            "source": "GlobeNewswire",
            "author": "",
            "url": "https://www.globenewswire.com/news-release/2026/08/19/mrna",
            "summary": "Press release — Moderna and Merck today announced",
        }
        self.assertTrue(_is_wire(msg))

    def test_benzinga_editorial_is_secondary(self):
        msg = {
            "source": "benzinga",
            "author": "Benzinga Newsdesk",
            "url": "https://www.benzinga.com/news/26/08/mrna-rips-on-vaccine-data",
            "summary": "Shares rally after the biotech printed trial data",
        }
        self.assertFalse(_is_wire(msg))

    def test_editorial_recap_does_not_clear_the_gate(self):
        editorial = _news(
            headline="Moderna Phase 3 trial met the primary endpoint",
            source="Benzinga/benzinga",
            is_primary_source=False,
            ticker="MRNA",
        )
        self.assertIn(editorial.catalyst_type, CONFIRMING)
        edgar = FakeFeed("edgar_8k", error=RuntimeError("SEC 503"))
        alpaca = FakeFeed("alpaca_news", items=[editorial])
        resolver = CatalystResolver([alpaca, edgar])
        resolver.watch(["MRNA"])
        asyncio.run(resolver._tick())
        gate = resolver.result_for("MRNA")
        self.assertEqual(gate.tier, "unconfirmed")
        self.assertFalse(gate.passed)


class ResolverTests(unittest.TestCase):
    def test_dead_edgar_still_latches_mrna_from_wire(self):
        edgar = FakeFeed("edgar_8k", error=RuntimeError("HTTP 503"))
        wire = FakeFeed("wire_rss", items=[mrna_primary()])
        resolver = CatalystResolver([edgar, wire])
        resolver.watch(["MRNA"])
        asyncio.run(resolver._tick())
        gate = resolver.result_for("MRNA")
        self.assertTrue(gate.passed)
        self.assertEqual(gate.tier, "confirmed")
        self.assertEqual(gate.catalyst.ticker, "MRNA")
        self.assertEqual(gate.catalyst.catalyst_type, "clinical_readout")

    def test_alpaca_poll_keeps_unmatched_buffer_items(self):
        feed = AlpacaNewsFeed("key", "secret")
        feed._rest_on_poll = False
        now = datetime(2026, 8, 19, 11, 0, tzinfo=timezone.utc)
        feed.ingest({
            "headline": "Moderna Phase 3 trial met the primary endpoint",
            "created_at": now.isoformat(),
            "symbols": ["MRNA"],
            "source": "GlobeNewswire",
            "url": "https://www.globenewswire.com/news-release/mrna",
            "summary": "press release",
            "T": "n",
        })
        feed.ingest({
            "headline": "AMD Announces Multi-Year Agreement with OpenAI",
            "created_at": now.isoformat(),
            "symbols": ["AMD"],
            "source": "Business Wire",
            "url": "https://www.businesswire.com/news/amd",
            "summary": "press release",
            "T": "n",
        })
        since = now - timedelta(hours=18)
        drained = asyncio.run(feed.poll(["MRNA"], since))
        self.assertEqual({n.ticker for n in drained}, {"MRNA"})
        self.assertEqual({n.ticker for n in feed._buffer}, {"AMD"})

    def test_events_file_has_six_logged_names(self):
        events = load_catalyst_events()
        self.assertEqual(len(events), 6)
        self.assertEqual(
            {e["ticker"] for e in events},
            {"MRNA", "NVDA", "ORCL", "AMD", "VKTX", "GME"},
        )


class Setup11VerifiedCases(unittest.TestCase):
    def setUp(self):
        self.scanner = Day1CatalystLong()

    def test_mrna_primary_wire_signal(self):
        tech = evaluate_technical_gate(mrna_defended_bars())
        self.assertTrue(tech["passed"], tech["reason"])
        self.assertAlmostEqual(tech["entry"], 129.47, places=2)
        self.assertGreaterEqual(tech["extension_pct"], 3.0)

        result = self.scanner.evaluate(
            "MRNA",
            prev_close=62.95,
            last_price=129.47,
            session_volume=11_200_000,
            avg_vol_20d=1_000_000,
            bars_5m=mrna_defended_bars(),
            news=confirmed(mrna_primary()),
            gap_pct=84.3,
            rvol=11.2,
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.stage, "SIGNAL")
        self.assertTrue(result.research_only)
        self.assertAlmostEqual(result.entry, 129.47, places=2)
        self.assertGreaterEqual(result.extension_pct, 3.0)
        self.assertAlmostEqual(result.rvol, 11.2, places=1)

    def test_amd_technical_gate_reject(self):
        result = self.scanner.evaluate(
            "AMD",
            prev_close=164.68,
            last_price=226.44,
            session_volume=33_000_000,
            avg_vol_20d=3_000_000,
            bars_5m=amd_broke_bars(),
            news=confirmed(_news(
                headline="AMD Announces Multi-Year Agreement with OpenAI for 6 Gigawatt Compute Deal",
                ticker="AMD",
                source="Business Wire",
                is_primary_source=True,
            )),
            gap_pct=37.5,
            rvol=11.0,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.stage, "technical-gate")
        self.assertEqual(result.broke_at, "09:35")
        self.assertIn("not defended", result.reason)

    def test_mrna_meme_headline_rejected(self):
        meme = _news(
            headline="Roaring Kitty posts on Reddit as Moderna meme stock rips",
            is_primary_source=False,
            ticker="MRNA",
        )
        self.assertEqual(meme.catalyst_type, "sentiment_squeeze")
        feed = FakeFeed("alpaca_news", items=[meme])
        resolver = CatalystResolver([feed])
        resolver.watch(["MRNA"])
        asyncio.run(resolver._tick())
        gate = resolver.result_for("MRNA")
        self.assertEqual(gate.tier, "rejected")
        self.assertFalse(gate.passed)

        result = self.scanner.evaluate(
            "MRNA",
            prev_close=62.95,
            last_price=129.47,
            bars_5m=mrna_defended_bars(),
            news=gate,
            gap_pct=84.3,
            rvol=11.2,
        )
        self.assertEqual(result.stage, "news-rejected")
        self.assertFalse(result.passed)

    def test_mrna_motley_fool_only_unconfirmed(self):
        fool = _news(
            headline="Moderna and Merck Just Made History With This Vaccine",
            source="The Motley Fool",
            is_primary_source=False,
            ticker="MRNA",
        )
        self.assertEqual(fool.catalyst_type, "unconfirmed")
        feed = FakeFeed("massive", items=[fool], is_primary=False)
        resolver = CatalystResolver([feed])
        resolver.watch(["MRNA"])
        asyncio.run(resolver._tick())
        gate = resolver.result_for("MRNA")
        self.assertEqual(gate.tier, "unconfirmed")
        self.assertFalse(gate.passed)

        result = self.scanner.evaluate(
            "MRNA",
            prev_close=62.95,
            last_price=129.47,
            bars_5m=mrna_defended_bars(),
            news=gate,
            gap_pct=84.3,
            rvol=11.2,
        )
        self.assertEqual(result.stage, "unconfirmed")
        self.assertFalse(result.passed)


class OverlayTests(unittest.TestCase):
    def _mover(self) -> PremktMover:
        m = PremktMover(
            ticker="MRNA",
            pm_price=116.02,
            prev_close=62.95,
            gap_pct=84.3,
            pm_high=120.0,
            pm_low=114.0,
            pm_volume=5_000_000,
            avg_daily_volume=1_000_000,
        )
        m.setup_tags = ["SETUP 5: News Gap Up - Watch for Bear Market Reversal"]
        m.direction = "SHORT (conditional)"
        m.confidence = "MEDIUM"
        m.entry_note = "Only short if SPY is bearish"
        return m

    def test_confirmed_suppresses_setup_5(self):
        mover = self._mover()
        apply_news_overlay(mover, confirmed(mrna_primary()))
        self.assertEqual(mover.news_gate_tier, "confirmed")
        self.assertTrue(any(t.startswith("SETUP 11") for t in mover.setup_tags))
        self.assertFalse(any(t.startswith("SETUP 5") for t in mover.setup_tags))
        self.assertEqual(mover.direction, "WATCH")
        self.assertIn(WATCH_TAG, mover.setup_tags)

    def test_rejected_keeps_fade_tag(self):
        mover = self._mover()
        apply_news_overlay(mover, NewsGateResult(
            passed=False, tier="rejected",
            reason="sentiment_squeeze — not cash flows",
        ))
        self.assertTrue(any(t.startswith("SETUP 5") for t in mover.setup_tags))
        self.assertIn("rejected", mover.warning.lower())

    def test_alpaca_config_optional(self):
        from config import AlpacaNewsConfig
        cfg = AlpacaNewsConfig(key_id="", secret_key="")
        self.assertFalse(cfg.available)


class ConsolidatorIdTests(unittest.TestCase):
    def test_setup_11_does_not_collide_with_dead_cat(self):
        from alert_consolidator import _parse_setup_id
        from conviction_score import SetupId

        self.assertEqual(
            _parse_setup_id("SETUP 11: Catalyst re-rating WATCH — do not fade"),
            SetupId.DAY1_CATALYST_GAP_LONG,
        )
        self.assertEqual(
            _parse_setup_id("CATALYST BOUNCE"),
            SetupId.DEAD_CAT_BOUNCE,
        )


if __name__ == "__main__":
    unittest.main()
