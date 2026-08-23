"""
catalyst_news.py — sub-minute catalyst confirmation for Setup 11.

THE PROBLEM THIS FIXES
----------------------
Measured on MRNA 2026-08-19, the biggest catalyst day in that ticker's history:

    catalyst hit the tape ............ 06:45 ET
    Massive get_ticker_news first hit . 12:34 ET   (5h49m late, and it was a
                                                    Motley Fool commentary piece)
    the actual press release ......... never appeared in that feed at all

A news gate that queries an aggregator at decision time would have blocked the
single best signal in the log.

THE FIX — three changes, in order of importance
-----------------------------------------------
1. DON'T QUERY AT 09:45. Resolve continuously from 04:00 ET and latch the result.
   By the decision bar you are reading a cached answer, not making a network call.
   This alone removes all decision-time latency.

2. SCOPE THE POLLING TO THE GAP WATCHLIST. You only need news for tickers that
   already cleared gap >= 20% and RVOL >= 10x. That is typically 0-5 names a day,
   which makes aggressive per-ticker polling of authoritative-but-slow sources
   affordable. No firehose subscription required.

3. RACE PRIMARY SOURCES, DON'T TRUST ONE. First confirming hit wins:

     AlpacaNewsFeed   websocket push, Benzinga-sourced <- PRIMARY. free.
     EdgarCurrentFeed SEC 8-K atom, Accepted: to the   <- corroboration, free
                      second, polled globally by CIK
     WireRSSFeed      per-company wire/IR RSS          <- optional fallback, free
     MassiveNewsFeed  backstop only, assume ~6h lag

   Alpaca resells the same Benzinga feed that paid services repackage, over a
   real-time websocket, for $0 on the Basic plan. It also exposes historical news
   back to 2015 via REST, which means you can BACKTEST this gate against past
   catalyst days instead of trusting it blind — something the Massive endpoint
   cannot do, since it has no date parameter.

   Because step 1 latches from 04:00 ET, the real latency requirement is "resolve
   within ~3 hours of the print", not "sub-second". Every free option above clears
   that comfortably. Only Massive (~6h, REST-only, batch) fails.

   Note EDGAR full-text search (efts.sec.gov) is DATE-granular only, so it is
   useless intraday. The current-filings atom feed is the one with timestamps.

A fourth state matters as much as the other three: when nothing resolves by the
deadline, emit UNCONFIRMED, not silence. Feeds fail. A watch alert that says
"gap + volume qualified, no catalyst found, check the wire yourself" costs you
30 seconds. Swallowing it costs you the trade.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import xml.etree.ElementTree as ET_XML
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Protocol, Sequence
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

SEC_UA = "ChartSignl Setup Sniper admin@chartsignl.com"  # SEC requires a real contact
POLL_START = time(4, 0)          # begin premarket watch
POLL_INTERVAL_SEC = 20
DECISION_TIME = time(9, 45)
LOOKBACK_HOURS = 18

CONFIRMING = {
    "clinical_readout",
    "regulatory_approval",
    "guidance_shock",
    "strategic_deal",
    "m_and_a",
}
REJECTED = {"sentiment_squeeze", "analyst_action", "offering"}

# Ordered: first match wins, so put the specific patterns first.
CLASSIFIER_RULES: list[tuple[str, str]] = [
    (r"\b(phase\s*(1|2|3|i{1,3})\b.*\b(met|success|positive|topline|primary endpoint)"
     r"|met (its|the) primary endpoint|topline results|pivotal trial)", "clinical_readout"),
    (r"\b(fda|ema|chmp)\b.*\b(approv|clearance|authoriz|breakthrough|priority review)"
     r"|\bpdufa\b", "regulatory_approval"),
    (r"\b(acqui|merger|to be acquired|definitive agreement to (acquire|merge)|takeover|"
     r"tender offer)\b", "m_and_a"),
    (r"\b(raises|lifts|increases) (its )?(full[- ]year|fy\d*|q\d|annual)? ?(guidance|outlook|forecast)"
     r"|\braises (fy|full[- ]year)|record (backlog|rpo|bookings)|\brpo\b", "guidance_shock"),
    (r"\b(strategic (partnership|agreement|collaboration|alliance)|multi[- ]year agreement|"
     r"supply agreement|licensing agreement|joint venture|gigawatt|compute (deal|agreement))\b",
     "strategic_deal"),
    # explicit rejects
    (r"\b(public offering|private placement|registered direct|atm program|"
     r"pricing of .* offering|shelf registration)\b", "offering"),
    (r"\b(upgrade[sd]?|downgrade[sd]?|price target|initiat(es|ed) coverage|reiterat)\b",
     "analyst_action"),
    (r"\b(reddit|wallstreetbets|roaring kitty|meme stock|posts on x|tweet)\b",
     "sentiment_squeeze"),
]


NowFn = Callable[[], datetime]


# ---------------------------------------------------------------- data model

@dataclass(frozen=True)
class CatalystNews:
    headline: str
    published_utc: datetime
    source: str
    catalyst_type: str
    is_primary_source: bool
    url: str = ""
    ticker: str = ""

    def key(self) -> str:
        return re.sub(r"\W+", "", self.headline.lower())[:120]


@dataclass
class NewsGateResult:
    passed: bool
    tier: str                       # confirmed | unconfirmed | rejected
    reason: str
    catalyst: Optional[CatalystNews] = None
    resolved_at: Optional[datetime] = None
    latency_sec: Optional[float] = None
    feeds_tried: list[str] = field(default_factory=list)


def classify(headline: str) -> str:
    h = headline.lower()
    for pattern, label in CLASSIFIER_RULES:
        if re.search(pattern, h):
            return label
    return "unconfirmed"


def unconfirmed_result(feeds_tried: Optional[list[str]] = None) -> NewsGateResult:
    return NewsGateResult(
        passed=False,
        tier="unconfirmed",
        reason=(
            "Gap and volume qualified but no catalyst resolved by any feed. "
            "Feeds fail — this is a WATCH alert, not a no-trade. "
            "Check the wire yourself before the 09:45 bar."
        ),
        feeds_tried=list(feeds_tried or []),
    )


# ---------------------------------------------------------------- feed layer

class NewsFeed(Protocol):
    name: str
    is_primary: bool
    async def poll(self, tickers: Sequence[str], since: datetime) -> list[CatalystNews]: ...


def _new_http_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": SEC_UA})
    return session


class _HttpFeed:
    """Shared HTTP via requests, offloaded to a thread so the resolver stays async.

    A per-feed semaphore keeps SEC's 10 req/s ceiling honest when several
    ticker RSS URLs fire in parallel.
    """

    def __init__(self, session: Optional[requests.Session] = None, max_concurrency: int = 4,
                 timeout: float = 8):
        self._session = session
        self._sem = asyncio.Semaphore(max_concurrency)
        self._timeout = timeout
        self._owns_session = session is None

    def _client(self) -> requests.Session:
        if self._session is None:
            self._session = _new_http_session()
        return self._session

    def _get_sync(self, url: str, headers: Optional[dict] = None) -> Optional[str]:
        try:
            resp = self._client().get(url, headers=headers or {}, timeout=self._timeout)
            if resp.status_code != 200:
                log.warning("%s -> HTTP %s", url, resp.status_code)
                return None
            return resp.text
        except Exception:
            log.exception("fetch failed: %s", url)
            return None

    async def _get(self, url: str, headers: Optional[dict] = None) -> Optional[str]:
        async with self._sem:
            return await asyncio.to_thread(self._get_sync, url, headers)


class WireRSSFeed(_HttpFeed):
    """Per-company RSS from the four wires. Seconds of latency, free.

    `wire_map` is ticker -> list of RSS URLs, built once by discover_feeds()
    and cached. Most issuers use exactly one wire consistently, so this is a
    small, stable mapping. Optional fallback — not load-bearing.
    """

    name = "wire_rss"
    is_primary = True

    def __init__(self, session, wire_map: dict[str, list[str]], **kw):
        super().__init__(session, **kw)
        self.wire_map = wire_map

    async def poll(self, tickers, since):
        urls = [(t, u) for t in tickers for u in self.wire_map.get(t.upper(), [])]
        pages = await asyncio.gather(*(self._get(u) for _, u in urls))
        out: list[CatalystNews] = []
        for (ticker, url), body in zip(urls, pages):
            if not body:
                continue
            out.extend(_parse_rss(body, ticker, self.name, since, primary=True))
        return out


class CompanyIRFeed(WireRSSFeed):
    """Issuer IR-page RSS. Same parser, different map."""
    name = "company_ir"


class EdgarCurrentFeed(_HttpFeed):
    """SEC latest-filings Atom feed, matched by CIK.

    Use THIS, not efts.sec.gov full-text search — EFTS is date-granular only and
    cannot tell you a filing landed twenty minutes ago. This feed carries an
    accepted-at timestamp to the second.

    8-Ks usually trail the press release by 30-120 minutes, which still clears a
    09:45 decision, but treat it as corroboration rather than the fast path.
    """

    name = "edgar_8k"
    is_primary = True
    URL = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
           "&type=8-K&company=&dateb=&owner=include&count=100&output=atom")

    def __init__(self, session, cik_map: dict[str, str], **kw):
        super().__init__(session, **kw)
        self.cik_map = {k.upper(): str(v).lstrip("0") for k, v in cik_map.items()}

    async def poll(self, tickers, since):
        body = await self._get(
            self.URL,
            headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate",
                     "Host": "www.sec.gov"},
        )
        if not body:
            return []
        wanted = {self.cik_map.get(t.upper()): t.upper() for t in tickers}
        wanted.pop(None, None)
        if not wanted:
            return []

        out = []
        try:
            root = ET_XML.fromstring(body)
        except ET_XML.ParseError:
            log.warning("EDGAR atom parse failed")
            return []
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("a:entry", ns):
            title = (entry.findtext("a:title", "", ns) or "").strip()
            updated = entry.findtext("a:updated", "", ns)
            link_el = entry.find("a:link", ns)
            href = link_el.get("href", "") if link_el is not None else ""
            cik = next((c for c in wanted if c and f"/{c}/" in href), None)
            if not cik:
                continue
            try:
                ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts < since:
                continue
            out.append(CatalystNews(
                headline=f"8-K filed: {title}",
                published_utc=ts,
                source="SEC EDGAR 8-K",
                catalyst_type=classify(title),
                is_primary_source=True,
                url=href,
                ticker=wanted[cik],
            ))
        return out


class AlpacaNewsFeed:
    """PRIMARY FEED. Free, real-time, Benzinga-sourced.

        wss://stream.data.alpaca.markets/v1beta1/news
        REST: https://data.alpaca.markets/v1beta1/news   (history back to 2015)

    Free Basic plan: 200 req/min, no separate news subscription. This is the same
    Benzinga content that paid press-release services repackage — Benzinga carries
    the major wires, so a company press release lands here within seconds.

    Push, not poll: run start_stream() once at 04:00 ET and it fills a buffer that
    poll() drains. The REST path is for backfill on reconnect, one-shot 08:30/09:45
    resolves, and backtesting.

    Caveat worth watching: Alpaca has described the news API as free "during the
    beta period" with possible future pricing changes. Verify entitlement on your
    own key before you depend on it, and keep EdgarCurrentFeed wired as a spare.
    """

    name = "alpaca_news"
    is_primary = True
    WS_URL = "wss://stream.data.alpaca.markets/v1beta1/news"
    REST_URL = "https://data.alpaca.markets/v1beta1/news"

    def __init__(self, key_id: str, secret_key: str, session: Optional[requests.Session] = None):
        self.key_id, self.secret_key = key_id, secret_key
        self._session = session
        self._buffer: list[CatalystNews] = []
        self._ws = None
        self._rest_on_poll = True
        self._http = _HttpFeed(session)

    def _auth_headers(self) -> dict:
        return {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret_key,
        }

    def _to_news(self, msg: dict) -> Optional[CatalystNews]:
        try:
            headline = msg["headline"]
            ts = datetime.fromisoformat(
                (msg.get("created_at") or msg["updated_at"]).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            syms = [s.upper() for s in (msg.get("symbols") or [])]
            ticker = syms[0] if syms else ""
            # Prefer an exact watchlist match when the item is tagged to several names.
            return CatalystNews(
                headline=headline,
                published_utc=ts,
                source=f"Benzinga/{msg.get('source', 'alpaca')}",
                catalyst_type=classify(headline),
                # Benzinga relays the issuer's own release; treat a wire-sourced
                # item as primary, an editorial byline as secondary.
                is_primary_source=_is_wire(msg),
                url=msg.get("url", ""),
                ticker=ticker,
            )
        except Exception:
            log.exception("bad Alpaca news payload")
            return None

    def ingest(self, msg: dict) -> Optional[CatalystNews]:
        """Parse one WS/REST payload into the buffer. Exposed for tests."""
        n = self._to_news(msg)
        if n:
            self._buffer.append(n)
        return n

    async def start_stream(self, tickers: Sequence[str]) -> None:
        """Connect, authenticate, subscribe. Reconnects with backoff.

        Optional — REST at 08:30 / 09:45 already clears a 09:45 decision given
        ~180 minutes of slack after a 06:45 print. Keep EdgarCurrentFeed as spare.
        """
        try:
            import websockets
        except ImportError:
            log.warning("websockets not installed; Alpaca news stream disabled (REST still works)")
            return

        if not self.key_id or not self.secret_key:
            log.warning("Alpaca keys missing; news stream disabled")
            return

        self._rest_on_poll = False
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.WS_URL) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({
                        "action": "auth",
                        "key": self.key_id,
                        "secret": self.secret_key,
                    }))
                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "news": list(tickers) or ["*"],
                    }))
                    log.info("Alpaca news stream connected (%d symbols)", len(tickers))
                    backoff = 1
                    async for raw in ws:
                        payload = json.loads(raw)
                        if isinstance(payload, dict):
                            payload = [payload]
                        for msg in payload:
                            if msg.get("T") != "n":
                                continue
                            self.ingest(msg)
            except Exception:
                log.exception("Alpaca news stream dropped; retrying in %ss", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def resubscribe(self, tickers: Sequence[str]) -> None:
        """Add watchlist names mid-session as the gap scanner finds them."""
        if self._ws is not None:
            await self._ws.send(json.dumps(
                {"action": "subscribe", "news": list(tickers)}))

    async def poll(self, tickers, since):
        want = {t.upper() for t in tickers}
        kept: list[CatalystNews] = []
        matched: list[CatalystNews] = []
        # Keep unmatched buffer items — draining the whole buffer used to drop
        # headlines for tickers that were not in this tick's watchlist.
        for n in self._buffer:
            if n.ticker in want and n.published_utc >= since:
                matched.append(n)
            else:
                kept.append(n)
        self._buffer = kept

        if self._rest_on_poll and self.key_id and self.secret_key and want:
            end = datetime.now(timezone.utc)
            rest = await self.backfill(sorted(want), since, end)
            matched.extend(rest)
        return matched

    async def backfill(self, tickers: Sequence[str], start: datetime,
                       end: datetime) -> list[CatalystNews]:
        """REST history — use this to BACKTEST the gate against past catalyst days.

        Run it over every event in catalyst_events.json and measure, per event,
        how long after the print the first confirming headline appeared. That is
        the number that tells you whether this gate is real.
        """
        if not self.key_id or not self.secret_key:
            return []
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        out: list[CatalystNews] = []
        token = None
        for _ in range(5):
            params = (
                f"?symbols={','.join(tickers)}"
                f"&start={start.isoformat()}"
                f"&end={end.isoformat()}"
                f"&limit=50"
                f"&sort=desc"
            )
            if token:
                params += f"&page_token={token}"
            body = await self._http._get(self.REST_URL + params, headers=self._auth_headers())
            if not body:
                break
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                log.warning("Alpaca news REST JSON parse failed")
                break
            for msg in data.get("news", []):
                n = self._to_news(msg)
                if n:
                    out.append(n)
            token = data.get("next_page_token")
            if not token:
                break
        return out


_WIRE_MARKERS = ("business wire", "globenewswire", "globe newswire",
                 "pr newswire", "prnewswire", "accesswire", "newsfile",
                 "press release")


def _is_wire(msg: dict) -> bool:
    """True when the item is the issuer's own release rather than editorial."""
    blob = " ".join(str(msg.get(k, "")) for k in
                    ("source", "author", "url", "summary")).lower()
    return any(m in blob for m in _WIRE_MARKERS)


class MassiveNewsFeed:
    """Backstop only. Measured ~6h lag on MRNA 2026-08-19 — never rely on it
    to clear the gate before the open. Kept so the post-mortem log has a record.

    Polygon `/v2/reference/news` has no usable published-at date filter on this
    plan, which is why historical replay has to go through Alpaca REST.
    """

    name = "massive"
    is_primary = False

    def __init__(self, client):
        self.client = client

    async def poll(self, tickers, since):
        out = []
        for t in tickers:
            try:
                arts = await asyncio.to_thread(
                    self.client.get_ticker_news, ticker=t, limit=20
                )
            except Exception:
                log.exception("massive news failed for %s", t)
                continue
            for a in arts or []:
                raw_ts = a.get("published_utc") or ""
                try:
                    ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < since:
                    continue
                pub = a.get("publisher", "massive")
                if isinstance(pub, dict):
                    pub = pub.get("name", "massive")
                out.append(CatalystNews(
                    headline=a.get("title") or "",
                    published_utc=ts,
                    source=str(pub or "massive"),
                    catalyst_type=classify(a.get("title") or ""),
                    is_primary_source=False,
                    url=a.get("article_url", "") or "",
                    ticker=t.upper(),
                ))
        return out


def _parse_rss(body: str, ticker: str, source: str, since: datetime,
               primary: bool) -> list[CatalystNews]:
    out = []
    try:
        root = ET_XML.fromstring(body)
    except ET_XML.ParseError:
        return out
    for item in root.iter():
        if not item.tag.endswith("item") and not item.tag.endswith("entry"):
            continue
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        raw = (item.findtext("pubDate") or item.findtext("published")
               or item.findtext("updated") or "")
        ts = _parse_date(raw)
        if ts is None or ts < since:
            continue
        link_el = item.find("link")
        if link_el is not None and (link_el.text or "").strip():
            link = link_el.text.strip()
        elif link_el is not None:
            link = link_el.get("href", "")
        else:
            link = ""
        out.append(CatalystNews(
            headline=title, published_utc=ts, source=source,
            catalyst_type=classify(title), is_primary_source=primary,
            url=link, ticker=ticker.upper(),
        ))
    return out


def _parse_date(raw: str) -> Optional[datetime]:
    raw = raw.strip()
    if not raw:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            d = datetime.strptime(raw, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------- resolver

class CatalystResolver:
    """Continuous premarket watcher. Latches the first confirming catalyst per
    ticker so the 09:45 gate check is a dict lookup, not a network call."""

    def __init__(
        self,
        feeds: Sequence[NewsFeed],
        *,
        now_fn: Optional[NowFn] = None,
        poll_interval_sec: float = POLL_INTERVAL_SEC,
        sleep_fn=None,
        lookback_hours: float = LOOKBACK_HOURS,
    ):
        self.feeds = list(feeds)
        self._resolved: dict[str, NewsGateResult] = {}
        self._seen: set[str] = set()
        self._watch: set[str] = set()
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._poll_interval = poll_interval_sec
        self._sleep = sleep_fn or asyncio.sleep
        self._lookback_hours = lookback_hours

    def watch(self, tickers: Iterable[str]) -> None:
        """Called by the gap scanner as names clear gap + RVOL."""
        for t in tickers:
            self._watch.add(t.upper())

    def result_for(self, ticker: str) -> NewsGateResult:
        """Read the latch. Never blocks."""
        t = ticker.upper()
        if t in self._resolved:
            return self._resolved[t]
        return unconfirmed_result([f.name for f in self.feeds])

    async def run_until(self, deadline: time = DECISION_TIME) -> None:
        while self._now().astimezone(ET).time() < deadline:
            if self._watch:
                await self._tick()
            await self._sleep(self._poll_interval)
        await self._tick()  # one last sweep on the bell

    async def _tick(self) -> None:
        pending = sorted(self._watch - set(self._resolved))
        if not pending:
            return
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        since = now - timedelta(hours=self._lookback_hours)
        batches = await asyncio.gather(
            *(f.poll(pending, since) for f in self.feeds), return_exceptions=True
        )
        items: list[CatalystNews] = []
        for feed, batch in zip(self.feeds, batches):
            if isinstance(batch, Exception):
                log.warning("feed %s raised: %s", feed.name, batch)
                continue
            items.extend(batch)

        # Primary sources first, then newest.
        items.sort(key=lambda n: (not n.is_primary_source, -n.published_utc.timestamp()))

        for n in items:
            if n.key() in self._seen or n.ticker in self._resolved:
                continue
            self._seen.add(n.key())

            if n.catalyst_type in REJECTED:
                self._resolved[n.ticker] = NewsGateResult(
                    passed=False, tier="rejected",
                    reason=(f"'{n.catalyst_type}' — sentiment/dilution/analyst driven, "
                            f"not a change to forward cash flows. Setup 1/3 fade "
                            f"territory, not a long."),
                    catalyst=n, resolved_at=now,
                    feeds_tried=[f.name for f in self.feeds])
                continue

            if n.catalyst_type in CONFIRMING and n.is_primary_source:
                self._resolved[n.ticker] = NewsGateResult(
                    passed=True, tier="confirmed",
                    reason=f"{n.catalyst_type} confirmed via {n.source}",
                    catalyst=n, resolved_at=now,
                    latency_sec=(now - n.published_utc).total_seconds(),
                    feeds_tried=[f.name for f in self.feeds])
                log.info("CATALYST LATCHED %s %s (%.0fs after print, via %s)",
                         n.ticker, n.catalyst_type,
                         (now - n.published_utc).total_seconds(), n.source)


# ---------------------------------------------------------------- discovery

async def discover_feeds(session, tickers: Sequence[str]) -> dict[str, list[str]]:
    """One-time (weekly-refreshed) build of ticker -> press-release RSS URLs.

    Most issuers publish through exactly one wire and keep a company-scoped RSS
    endpoint. Probe the known patterns once, cache what returns 200, and you have
    a stable map for the whole universe. Run this offline, not in the hot path.
    """
    patterns = [
        "https://www.globenewswire.com/RssFeed/organization/{slug}/feedTitle/{slug}",
        "https://www.businesswire.com/portal/site/home/template.PAGE/news/rss/?ticker={ticker}",
        "https://www.prnewswire.com/rss/news-releases-list.rss?company={slug}",
    ]
    found: dict[str, list[str]] = {}
    http = _HttpFeed(session, max_concurrency=6)
    for t in tickers:
        hits = []
        for p in patterns:
            url = p.format(ticker=t, slug=t.lower())
            if await http._get(url):
                hits.append(url)
        if hits:
            found[t.upper()] = hits
    return found


async def load_cik_map(session) -> dict[str, str]:
    """SEC's free ticker -> CIK map. Refresh weekly."""
    http = _HttpFeed(session)
    body = await http._get(
        "https://www.sec.gov/files/company_tickers.json",
        headers={"User-Agent": SEC_UA},
    )
    if not body:
        return {}
    return {
        row["ticker"].upper(): str(row["cik_str"]).zfill(10)
        for row in json.loads(body).values()
    }


def load_cached_cik_map(
    session: Optional[requests.Session] = None,
    cache_path: Optional[str] = None,
    max_age_days: int = 7,
) -> dict[str, str]:
    """Load ticker→CIK from disk if fresh, otherwise hit SEC and cache."""
    path = Path(cache_path or os.path.join(os.getenv("DATA_DIR", "."), "cik_map.json"))
    if path.exists():
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        )
        if age <= timedelta(days=max_age_days):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return {str(k).upper(): str(v) for k, v in raw.items()}
            except (json.JSONDecodeError, OSError):
                log.warning("CIK cache unreadable: %s", path)
    try:
        mapping = asyncio.run(load_cik_map(session))
    except RuntimeError:
        # Already inside an event loop — skip refresh rather than deadlock.
        mapping = {}
    if mapping:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(mapping), encoding="utf-8")
        except OSError:
            log.warning("could not write CIK cache %s", path)
    return mapping


def feeds_from_config(
    config,
    *,
    session: Optional[requests.Session] = None,
    polygon_client=None,
    wire_map: Optional[dict[str, list[str]]] = None,
    cik_map: Optional[dict[str, str]] = None,
) -> list:
    """Build the race. Alpaca first when keyed; EDGAR spare; Massive last."""
    feeds: list = []
    alpaca_cfg = getattr(config, "alpaca_news", None)
    if alpaca_cfg is not None and getattr(alpaca_cfg, "available", False):
        feeds.append(AlpacaNewsFeed(alpaca_cfg.key_id, alpaca_cfg.secret_key, session))
    if cik_map:
        feeds.append(EdgarCurrentFeed(session, cik_map))
    if wire_map:
        feeds.append(WireRSSFeed(session, wire_map))
    if polygon_client is not None:
        feeds.append(MassiveNewsFeed(polygon_client))
    return feeds


async def resolve_watchlist(
    tickers: Sequence[str],
    feeds: Sequence[NewsFeed],
    *,
    now_fn: Optional[NowFn] = None,
    lookback_hours: float = LOOKBACK_HOURS,
) -> dict[str, NewsGateResult]:
    resolver = CatalystResolver(
        feeds, now_fn=now_fn, lookback_hours=lookback_hours,
    )
    resolver.watch(tickers)
    await resolver._tick()
    return {t.upper(): resolver.result_for(t) for t in tickers}


def resolve_watchlist_sync(
    tickers: Sequence[str],
    feeds: Sequence[NewsFeed],
    **kw,
) -> dict[str, NewsGateResult]:
    return asyncio.run(resolve_watchlist(tickers, feeds, **kw))


def default_events_path() -> str:
    here = Path(__file__).resolve().parent
    return str(here / "catalyst_events.json")


def load_catalyst_events(path: Optional[str] = None) -> list[dict]:
    p = Path(path or default_events_path())
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "events" in data:
        return list(data["events"])
    if isinstance(data, list):
        return data
    return []


async def backfill_events(
    feed: AlpacaNewsFeed,
    events: Sequence[dict],
    lookback_hours: float = LOOKBACK_HOURS,
) -> list[dict]:
    """Replay Alpaca REST history for each logged event; measure headline lag."""
    rows = []
    for ev in events:
        ticker = str(ev.get("ticker", "")).upper()
        event_date = ev.get("event_date", "")
        cat = ev.get("catalyst") or {}
        time_et = cat.get("time_et") or "06:45"
        try:
            print_local = datetime.strptime(
                f"{event_date} {time_et}", "%Y-%m-%d %H:%M"
            ).replace(tzinfo=ET)
        except ValueError:
            log.warning("skip %s: bad event_date/time_et", ticker)
            continue
        print_utc = print_local.astimezone(timezone.utc)
        start = print_utc - timedelta(hours=1)
        end = print_utc + timedelta(hours=lookback_hours)
        news = await feed.backfill([ticker], start, end)
        confirming = [
            n for n in news
            if n.catalyst_type in CONFIRMING
        ]
        confirming.sort(key=lambda n: n.published_utc)
        first = confirming[0] if confirming else None
        latency = None
        if first is not None:
            latency = (first.published_utc - print_utc).total_seconds()
        rows.append({
            "ticker": ticker,
            "event_date": event_date,
            "print_et": print_local.isoformat(),
            "headlines": len(news),
            "confirming": len(confirming),
            "first_headline": first.headline if first else None,
            "first_source": first.source if first else None,
            "first_primary": first.is_primary_source if first else None,
            "latency_sec": latency,
            "clears_0945": (
                first is not None
                and first.published_utc <= datetime(
                    print_local.year, print_local.month, print_local.day,
                    DECISION_TIME.hour, DECISION_TIME.minute, tzinfo=ET,
                ).astimezone(timezone.utc)
            ),
        })
    return rows
