"""
Setup Sniper — Configuration
All tuneable parameters live here. No magic numbers buried in logic.
"""
import json
import os
from dataclasses import dataclass, field
from typing import Optional


def _default_range_index_etfs() -> list[str]:
    return [
        "SPY", "QQQ", "IWM", "DIA", "XLF", "XLE", "XLK",
    ]


def _default_range_price_tiers() -> list[tuple]:
    return [
        (10.0, 0.30, 0.40),
        (20.0, 0.60, 0.80),
        (50.0, 1.00, 1.25),
        (100.0, 1.50, 2.00),
        (250.0, 2.50, 3.00),
        (999999, 4.00, 5.00),
    ]


@dataclass
class RangeConfig:
    """
    Minimum stop/target distance floors (ATR, price tier, index ETF %).
    Override via env: RANGE_INDEX_STOP_MIN_PCT, RANGE_INDEX_ETFS, etc.
    """

    index_etf_tickers: list = field(default_factory=_default_range_index_etfs)
    index_stop_min_pct: float = 0.005
    index_target_min_pct: float = 0.005
    price_tiers: list = field(default_factory=_default_range_price_tiers)
    atr_stop_fraction: float = 0.25
    atr_target_fraction: float = 0.33
    max_stop_pct_of_price: float = 0.05
    max_target_pct_of_price: float = 0.08
    # Structural stop cap (× daily ATR) before floors; R:R gate; smart target floor
    max_stop_atr_multiple: float = 1.5
    min_rr_ratio: float = 0.8
    target_floor_activation_threshold: float = 0.25

    def __post_init__(self):
        v = os.getenv("RANGE_INDEX_STOP_MIN_PCT")
        if v is not None and v != "":
            self.index_stop_min_pct = float(v)
        v = os.getenv("RANGE_INDEX_TARGET_MIN_PCT")
        if v is not None and v != "":
            self.index_target_min_pct = float(v)
        v = os.getenv("RANGE_ATR_STOP_FRACTION")
        if v is not None and v != "":
            self.atr_stop_fraction = float(v)
        v = os.getenv("RANGE_ATR_TARGET_FRACTION")
        if v is not None and v != "":
            self.atr_target_fraction = float(v)
        v = os.getenv("RANGE_MAX_STOP_PCT_OF_PRICE")
        if v is not None and v != "":
            self.max_stop_pct_of_price = float(v)
        v = os.getenv("RANGE_MAX_TARGET_PCT_OF_PRICE")
        if v is not None and v != "":
            self.max_target_pct_of_price = float(v)
        v = os.getenv("RANGE_MAX_STOP_ATR_MULTIPLE")
        if v is not None and v != "":
            self.max_stop_atr_multiple = float(v)
        v = os.getenv("RANGE_MIN_RR_RATIO")
        if v is not None and v != "":
            self.min_rr_ratio = float(v)
        v = os.getenv("RANGE_TARGET_FLOOR_ACTIVATION")
        if v is not None and v != "":
            self.target_floor_activation_threshold = float(v)

        etf_s = os.getenv("RANGE_INDEX_ETFS")
        if etf_s:
            self.index_etf_tickers = [
                x.strip().upper() for x in etf_s.split(",") if x.strip()
            ]

        tiers_s = os.getenv("RANGE_PRICE_TIERS_JSON")
        if tiers_s:
            raw = json.loads(tiers_s)
            self.price_tiers = [tuple(row) for row in raw]


@dataclass
class APIConfig:
    """Polygon.io (Massive) API settings."""
    api_key: str = ""
    base_url: str = "https://api.polygon.io"
    rate_limit_per_min: int = 100  # Paid Polygon plan. Free tier = 5/min.
    timeout_seconds: int = 10

    def __post_init__(self):
        self.api_key = self.api_key or os.getenv("MASSIVE_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "MASSIVE_API_KEY not set. Export it or add to .env:\n"
                "  export MASSIVE_API_KEY=your_key_here"
            )


@dataclass
class SupertrendConfig:
    """
    Supertrend indicator settings.
    >>> CONFIRM WITH YOUR CHART SETTINGS <<<
    Common combos: (10, 3), (10, 2), (7, 3)
    """
    period: int = 10
    multiplier: float = 3.0


@dataclass
class RunnerFadeConfig:
    """Setup 6: Prior Day Runner Fade — thresholds from your journal."""

    # --- NIGHTLY SCAN ---
    min_pct_change: float = 80.0          # 80%+ move yesterday
    min_volume_multiplier: float = 5.0     # Volume 5x the 20-day avg
    volume_lookback_days: int = 20         # "Normal" volume baseline
    min_price: float = 1.00               # No sub-dollar junk
    max_price: float = 500.0              # Upper bound
    min_dollar_volume: float = 5_000_000  # $5M minimum — ensures liquidity/shortability

    # --- MORNING PREMARKET CHECK ---
    ema_period: int = 9                    # Your anchor
    ema_timeframe_minutes: int = 5         # 5-min chart
    supertrend: SupertrendConfig = field(default_factory=SupertrendConfig)

    # --- ALERT CONTEXT ---
    exit_time: str = "10:00 AM ET"
    entry_description: str = "Short on break below PM low or 200 EMA"


@dataclass
class EarningsMissConfig:
    """Setup 3: Earnings Miss Short — thresholds and earnings filters."""

    # Earnings surprise filters (both must miss)
    eps_surprise_percent_max: float = 0.0        # <= 0 → EPS miss
    revenue_surprise_percent_max: float = 0.0    # <= 0 → revenue miss
    report_date_lookback_days: int = 2           # Yesterday + today

    # Liquidity filters
    min_price: float = 1.0
    max_price: float = 500.0
    min_dollar_volume: float = 5_000_000

    # Intraday confirmation
    ema_period: int = 9
    ema_timeframe_minutes: int = 5
    supertrend: SupertrendConfig = field(default_factory=SupertrendConfig)


@dataclass
class ExhaustionGapConfig:
    """Setup 7: Exhaustion Gap Short — gap and volume thresholds."""

    # Gap + liquidity filters
    min_gap_up_pct: float = 5.0
    min_volume_multiplier: float = 3.0
    volume_lookback_days: int = 20
    min_price: float = 1.0
    max_price: float = 500.0
    min_dollar_volume: float = 5_000_000

    # Intraday confirmation
    ema_period: int = 9
    ema_timeframe_minutes: int = 5
    supertrend: SupertrendConfig = field(default_factory=SupertrendConfig)


@dataclass
class AlertConfig:
    """Where and how to deliver alerts."""
    console_output: bool = True
    discord_webhook_url: Optional[str] = None
    slack_webhook_url: Optional[str] = None
    log_file: str = ""

    # Email alerts via SMTP (Gmail-friendly defaults)
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    email_from: str = ""        # e.g. "you@gmail.com" (use an App Password)
    email_to: str = ""          # Your personal email for receiving alerts
    email_password: str = ""    # Loaded from ALERT_EMAIL_PASSWORD
    email_login: str = ""       # Optional override for SMTP login username
    email_sender_name: str = "" # Display name in From header

    def __post_init__(self):
        self.discord_webhook_url = self.discord_webhook_url or os.getenv("DISCORD_WEBHOOK_URL")
        self.slack_webhook_url = self.slack_webhook_url or os.getenv("SLACK_WEBHOOK_URL")
        self.email_from = self.email_from or os.getenv("ALERT_EMAIL_FROM", "")
        self.email_to = self.email_to or os.getenv("ALERT_EMAIL_TO", "")
        self.email_password = self.email_password or os.getenv("ALERT_EMAIL_PASSWORD", "")
        self.email_login = self.email_login or os.getenv("ALERT_EMAIL_LOGIN", self.email_from)
        self.email_sender_name = self.email_sender_name or os.getenv("ALERT_EMAIL_SENDER_NAME", "Setup Sniper")
        if not self.log_file:
            log_dir = os.getenv("LOG_DIR", ".")
            self.log_file = os.path.join(log_dir, "alerts.log")

    @property
    def email_enabled(self) -> bool:
        return bool(self.email_from and self.email_password and self.email_to)


@dataclass
class FmpConfig:
    api_key: str = ""
    timeout_seconds: int = 10

    def __post_init__(self):
        self.api_key = self.api_key or os.getenv("FMP_API_KEY", "")

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass
class UniverseConfig:
    """Top-N liquid universe built from Polygon all-tickers snapshot."""

    top_n: int = 500
    min_price: float = 1.0
    min_dollar_volume: float = 0.0
    cache_ttl_seconds: int = 3600
    universe_file: str = ""


@dataclass
class IntradayConfig:
    """Phase 2: Intraday scanner settings."""

    # Polling
    poll_interval_seconds: int = 300    # 5 minutes (matches your 5-min chart)
    market_open: str = "09:30"          # ET
    market_close: str = "16:00"         # ET
    scan_start: str = "09:25"           # Start polling 5 min early for premarket context
    scan_end: str = "16:05"             # Run 5 min past close to catch final bar

    # Setup 8: ORL + VWAP + 9 EMA
    opening_range_bars: int = 3         # 3 bars × 5 min = 09:30-09:45
    ema_period: int = 9
    ema_slope_lookback: int = 3         # Bars to measure slope over
    patience_timer_minutes: int = 15    # Rule 2: delay non-critical alerts 15 min after open

    # VIX thresholds (Setups 1 & 2)
    vix_spike_threshold: float = 40.0   # VIX above this = extreme fear
    vix_spike_pct: float = 30.0         # 30%+ rise in 14 days
    vix_spike_lookback_days: int = 14
    vix_crash_pct: float = -30.0        # 30%+ drop in 3 sessions
    vix_crash_lookback_days: int = 3

    # Ticker universe
    core_tickers: str = "SPY,QQQ"       # Always scan these
    custom_tickers: str = ""            # Additional tickers from .env
    include_phase1_watchlist: bool = True  # Also scan today's Phase 1 candidates

    def __post_init__(self):
        self.custom_tickers = self.custom_tickers or os.getenv("INTRADAY_TICKERS", "")

    @property
    def all_core_tickers(self) -> list[str]:
        tickers = [t.strip() for t in self.core_tickers.split(",") if t.strip()]
        if self.custom_tickers:
            tickers.extend(t.strip() for t in self.custom_tickers.split(",") if t.strip())
        return list(dict.fromkeys(tickers))  # Dedupe preserving order


@dataclass
class CatalystConfig:
    """
    AI catalyst briefing settings.
    Uses Anthropic (Claude) to generate short catalyst blurbs for alerts.
    """

    anthropic_api_key: str = ""
    model: str = "claude-3-5-sonnet-latest"

    def __post_init__(self):
        # Reuse existing Anthropic key from env; no new key required.
        self.anthropic_api_key = self.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY", "")

    @property
    def available(self) -> bool:
        """True when catalyst briefing can be used."""
        return bool(self.anthropic_api_key)


@dataclass
class AlpacaNewsConfig:
    """Optional Alpaca news keys. Missing keys must not block ScannerConfig()."""

    key_id: str = ""
    secret_key: str = ""

    def __post_init__(self):
        self.key_id = self.key_id or os.getenv("APCA_API_KEY_ID", "")
        self.secret_key = self.secret_key or os.getenv("APCA_API_SECRET_KEY", "")

    @property
    def available(self) -> bool:
        return bool(self.key_id and self.secret_key)


@dataclass
class Setup11Config:
    """Setup 11: Day-1 Catalyst Gap Long — research-only until 20+ events."""

    min_gap_pct: float = 20.0
    min_rvol: float = 10.0
    lookback_hours: int = 18
    min_extension_pct: float = 3.0
    ema_period: int = 9
    decision_hour: int = 9
    decision_minute: int = 45


@dataclass
class ScannerConfig:
    """Master config."""
    api: APIConfig = field(default_factory=APIConfig)
    range_floor: RangeConfig = field(default_factory=RangeConfig)
    runner_fade: RunnerFadeConfig = field(default_factory=RunnerFadeConfig)
    earnings_miss: EarningsMissConfig = field(default_factory=EarningsMissConfig)
    exhaustion_gap: ExhaustionGapConfig = field(default_factory=ExhaustionGapConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    fmp: FmpConfig = field(default_factory=FmpConfig)
    intraday: IntradayConfig = field(default_factory=IntradayConfig)
    catalyst: CatalystConfig = field(default_factory=CatalystConfig)
    alpaca_news: AlpacaNewsConfig = field(default_factory=AlpacaNewsConfig)
    setup11: Setup11Config = field(default_factory=Setup11Config)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    watchlist_file: str = ""
    earnings_watchlist_file: str = ""
    exhaustion_watchlist_file: str = ""
    timezone: str = "US/Eastern"

    def __post_init__(self):
        if not self.watchlist_file:
            data_dir = os.getenv("DATA_DIR", ".")
            # Backwards-compatible: original runner fade watchlist
            self.watchlist_file = os.path.join(data_dir, "watchlist.json")

        data_dir = os.getenv("DATA_DIR", ".")
        if not self.earnings_watchlist_file:
            self.earnings_watchlist_file = os.path.join(data_dir, "watchlist_earnings_miss.json")
        if not self.exhaustion_watchlist_file:
            self.exhaustion_watchlist_file = os.path.join(data_dir, "watchlist_exhaustion_gap.json")

        universe_size = int(os.getenv("UNIVERSE_SIZE", "500"))
        self.universe.top_n = universe_size
        if not self.universe.universe_file:
            self.universe.universe_file = os.path.join(data_dir, "universe.json")
