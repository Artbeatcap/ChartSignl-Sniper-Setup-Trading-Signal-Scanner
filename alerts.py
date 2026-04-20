"""
Alert Delivery System
Handles: pretty console output, Discord webhook, email (SMTP), and file logging.
Designed to be dead simple so you actually read the alerts.
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import requests

from config import AlertConfig

logger = logging.getLogger(__name__)

# ─── ANSI colors for terminal ─────────────────────────────────────────
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def _exit_plan_with_floor(alert) -> str:
    text = getattr(alert, "exit_plan", "") or ""
    if getattr(alert, "stop_was_widened", False) and getattr(alert, "floor_source", ""):
        return f"{text} — {alert.floor_source}"
    return text


def format_alert_console(alert) -> str:
    """
    Format a setup alert for terminal display.
    Designed for quick pattern recognition at 8 AM when you're scanning.
    """
    lines = [
        "",
        f"{RED}{BOLD}{'━' * 60}{RESET}",
        f"{RED}{BOLD}  🔴 {getattr(alert, 'setup', 'SETUP')} — {alert.ticker}{RESET}",
        f"{RED}{BOLD}{'━' * 60}{RESET}",
        f"  {BOLD}Direction:{RESET}  {RED}{getattr(alert, 'direction', 'SHORT')}{RESET}",
        f"  {BOLD}Confidence:{RESET} {getattr(alert, 'confidence', '')}",
        "",
        f"  {CYAN}{BOLD}KEY LEVELS{RESET}",
    ]

    # Per-setup key levels (duck-typed)
    if hasattr(alert, "yesterday_high"):
        lines.extend([
            f"  Yesterday High:  ${alert.yesterday_high:.2f}  {DIM}(must stay below){RESET}",
            f"  PM High:         ${alert.pm_high:.2f}",
            f"  PM Low:          ${alert.pm_low:.2f}  {YELLOW}← break = entry{RESET}",
        ])
    if getattr(alert, "setup", "").upper().startswith("EARNINGS"):
        lines.extend([
            f"  PM High:         ${getattr(alert, 'pm_high', 0.0):.2f}",
            f"  PM Low:          ${getattr(alert, 'pm_low', 0.0):.2f}  {YELLOW}← break = entry{RESET}",
        ])
    if getattr(alert, "setup", "").upper().startswith("EXHAUSTION GAP"):
        lines.extend([
            f"  Prior Close:     ${getattr(alert, 'prior_close', 0.0):.2f}",
            f"  Gap Open:        ${getattr(alert, 'gap_open', 0.0):.2f}",
            f"  PM High:         ${getattr(alert, 'pm_high', 0.0):.2f}",
            f"  PM Low:          ${getattr(alert, 'pm_low', 0.0):.2f}  {YELLOW}← break = entry{RESET}",
        ])

    if hasattr(alert, "pm_price"):
        lines.append(f"  Current (PM):    ${alert.pm_price:.2f}")
    if hasattr(alert, "ema_9_current"):
        lines.append(f"  9 EMA (5m):      ${alert.ema_9_current:.2f}  {DIM}(price below ✓){RESET}")
    if getattr(alert, "ema_200_daily", None):
        lines.append(f"  200 EMA (daily):  ${alert.ema_200_daily:.2f}  {DIM}(alt entry level){RESET}")
    if getattr(alert, "daily_atr", None):
        lines.append(f"  Daily ATR:        ${alert.daily_atr:.2f}  {DIM}(expected range){RESET}")
    if getattr(alert, "stop_price", None) is not None:
        lines.append(f"  Stop:             ${alert.stop_price:.2f}")
    if getattr(alert, "target_price", None) is not None:
        lines.append(f"  Target:           ${alert.target_price:.2f}")

    universe_ctx = getattr(alert, "universe_context", "")
    if universe_ctx:
        lines.append(f"  {DIM}Universe:{RESET} {universe_ctx}")

    # Optional AI catalyst briefing
    catalyst = getattr(alert, "catalyst", "")
    if catalyst:
        lines.extend([
            "",
            f"  {DIM}📰 Catalyst: {catalyst}{RESET}",
        ])

    lines.extend([
        "",
        f"  {CYAN}{BOLD}STATUS{RESET}",
        f"  Below 9 EMA:     {'✓' if getattr(alert, 'below_9ema', False) else '✗'}",
        f"  Supertrend:      {RED}{getattr(alert, 'supertrend_direction', '')}{RESET}",
        "",
        f"  {YELLOW}{BOLD}ENTRY:{RESET}  {getattr(alert, 'entry', '')}",
        f"  {YELLOW}{BOLD}EXIT:{RESET}   {_exit_plan_with_floor(alert)}",
        "",
        f"  {DIM}\"Be patient for setups like this. This is your bread and butter trades.\"{RESET}",
        f"{RED}{BOLD}{'━' * 60}{RESET}",
        "",
    ])
    return "\n".join(lines)


def format_alert_discord(alert) -> dict:
    """Format as Discord embed for webhook delivery."""
    fields = [
        {"name": "Direction", "value": "🔴 SHORT", "inline": True},
        {"name": "Setup", "value": getattr(alert, "setup", "UNKNOWN"), "inline": True},
        {"name": "Confidence", "value": getattr(alert, "confidence", ""), "inline": True},
    ]
    if hasattr(alert, "pm_price"):
        fields.append({"name": "PM Price", "value": f"${alert.pm_price:.2f}", "inline": True})
    if hasattr(alert, "yesterday_high"):
        fields.append({"name": "Yesterday High", "value": f"${alert.yesterday_high:.2f}", "inline": True})
    if hasattr(alert, "pm_low"):
        fields.append({"name": "PM Low (entry)", "value": f"${alert.pm_low:.2f}", "inline": True})
    if hasattr(alert, "ema_9_current"):
        fields.append({"name": "9 EMA", "value": f"${alert.ema_9_current:.2f}", "inline": True})
    if getattr(alert, "daily_atr", None):
        fields.append({"name": "Daily ATR", "value": f"${alert.daily_atr:.2f}", "inline": True})
    if getattr(alert, "stop_price", None) is not None:
        fields.append({"name": "Stop", "value": f"${alert.stop_price:.2f}", "inline": True})
    if getattr(alert, "target_price", None) is not None:
        fields.append({"name": "Target", "value": f"${alert.target_price:.2f}", "inline": True})
    if getattr(alert, "gap_pct", None) is not None:
        fields.append({"name": "Gap %", "value": f"{alert.gap_pct:.1f}%", "inline": True})
    if getattr(alert, "eps_surprise_percent", None) is not None:
        fields.append({"name": "EPS Surprise %", "value": f"{alert.eps_surprise_percent:.1f}%", "inline": True})
    if getattr(alert, "revenue_surprise_percent", None) is not None:
        fields.append({"name": "Revenue Surprise %", "value": f"{alert.revenue_surprise_percent:.1f}%", "inline": True})
    if getattr(alert, "universe_context", ""):
        fields.append(
            {
                "name": "Universe",
                "value": getattr(alert, "universe_context", ""),
                "inline": False,
            }
        )

    fields.append({"name": "Entry", "value": getattr(alert, "entry", ""), "inline": False})
    fields.append({"name": "Exit", "value": _exit_plan_with_floor(alert), "inline": False})

    setup_name = getattr(alert, "setup", "SETUP")

    return {
        "embeds": [{
            "title": f"🔴 {setup_name} — {alert.ticker}",
            "color": 0xFF0000,
            "fields": fields,
            "footer": {"text": "Setup Sniper | \"Be patient. Bread and butter.\""},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    }


# ─── EMAIL TEMPLATES ───────────────────────────────────────────────────

def format_alert_email_html(alert) -> str:
    """HTML email for a single morning alert. Designed to scan in 5 seconds on your phone."""
    atr_row = ""
    if getattr(alert, "daily_atr", None):
        atr_row = f'<tr><td style="padding:4px 12px;color:#888;">Daily ATR</td><td style="padding:4px 12px;font-weight:600;">${alert.daily_atr:.2f}</td></tr>'

    ema200_row = ""
    if getattr(alert, "ema_200_daily", None):
        ema200_row = f'<tr><td style="padding:4px 12px;color:#888;">200 EMA (daily)</td><td style="padding:4px 12px;font-weight:600;">${alert.ema_200_daily:.2f}</td></tr>'

    extra_rows = ""
    if getattr(alert, "gap_pct", None) is not None:
        extra_rows += f'<tr><td style="padding:4px 12px;color:#888;">Gap %</td><td style="padding:4px 12px;font-weight:600;">{alert.gap_pct:.1f}%</td></tr>'
    if getattr(alert, "eps_surprise_percent", None) is not None:
        extra_rows += f'<tr><td style="padding:4px 12px;color:#888;">EPS Surprise %</td><td style="padding:4px 12px;font-weight:600;">{alert.eps_surprise_percent:.1f}%</td></tr>'
    if getattr(alert, "revenue_surprise_percent", None) is not None:
        extra_rows += f'<tr><td style="padding:4px 12px;color:#888;">Revenue Surprise %</td><td style="padding:4px 12px;font-weight:600;">{alert.revenue_surprise_percent:.1f}%</td></tr>'

    catalyst = getattr(alert, "catalyst", "")

    catalyst_row = ""
    if catalyst:
        catalyst_row = (
            "<tr><td colspan='2' style=\"padding:8px 12px;color:#aaa;font-size:13px;\">"
            f"📰 {catalyst}"
            "</td></tr>"
        )

    universe_row = ""
    if getattr(alert, "universe_context", ""):
        universe_row = (
            f"<tr><td colspan='2' style=\"padding:8px 12px;color:#88c;font-size:13px;\">"
            f"Universe: {getattr(alert, 'universe_context', '')}</td></tr>"
        )

    stop_target_rows = ""
    if getattr(alert, "stop_price", None) is not None:
        stop_target_rows += (
            f'<tr><td style="padding:4px 12px;color:#888;">Stop</td>'
            f'<td style="padding:4px 12px;font-weight:600;">${alert.stop_price:.2f}</td></tr>'
        )
    if getattr(alert, "target_price", None) is not None:
        stop_target_rows += (
            f'<tr style="background:rgba(255,255,255,0.05);">'
            f'<td style="padding:4px 12px;color:#888;">Target</td>'
            f'<td style="padding:4px 12px;font-weight:600;">${alert.target_price:.2f}</td></tr>'
        )

    exit_html = _exit_plan_with_floor(alert)

    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:500px;margin:0 auto;">
      <div style="background:#1a1a2e;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0;">
        <h2 style="margin:0;font-size:18px;">🔴 {alert.setup}</h2>
        <h1 style="margin:4px 0 0;font-size:28px;color:#ff4444;">{alert.ticker}</h1>
      </div>

      <div style="background:#16213e;color:#eee;padding:16px 20px;">
        <div style="display:flex;gap:20px;margin-bottom:12px;">
          <div><span style="color:#888;font-size:12px;">DIRECTION</span><br><strong style="color:#ff4444;">SHORT</strong></div>
          <div><span style="color:#888;font-size:12px;">CONFIDENCE</span><br><strong>{alert.confidence}</strong></div>
        </div>

        <table style="width:100%;border-collapse:collapse;font-size:14px;">
          <tr style="background:rgba(255,255,255,0.05);"><td style="padding:4px 12px;color:#888;">PM High</td><td style="padding:4px 12px;font-weight:600;">${getattr(alert, 'pm_high', 0.0):.2f}</td></tr>
          <tr><td style="padding:4px 12px;color:#ff4;">PM Low (ENTRY)</td><td style="padding:4px 12px;font-weight:700;color:#ffcc00;">${getattr(alert, 'pm_low', 0.0):.2f}</td></tr>
          <tr style="background:rgba(255,255,255,0.05);"><td style="padding:4px 12px;color:#888;">Current PM Price</td><td style="padding:4px 12px;font-weight:600;">${getattr(alert, 'pm_price', 0.0):.2f}</td></tr>
          <tr><td style="padding:4px 12px;color:#888;">9 EMA (5m)</td><td style="padding:4px 12px;font-weight:600;">${getattr(alert, 'ema_9_current', 0.0):.2f} ✓</td></tr>
          {ema200_row}
          {atr_row}
          {stop_target_rows}
          {extra_rows}
          <tr style="background:rgba(255,255,255,0.05);"><td style="padding:4px 12px;color:#888;">Supertrend</td><td style="padding:4px 12px;font-weight:600;color:#ff4444;">{getattr(alert, 'supertrend_direction', '')}</td></tr>
          {universe_row}
          {catalyst_row}
        </table>
      </div>

      <div style="background:#0f3460;color:#eee;padding:16px 20px;">
        <p style="margin:0 0 8px;"><strong style="color:#ffcc00;">ENTRY:</strong> {alert.entry}</p>
        <p style="margin:0;"><strong style="color:#ffcc00;">EXIT:</strong> {exit_html}</p>
      </div>

      <div style="background:#1a1a2e;color:#666;padding:12px 20px;border-radius:0 0 8px 8px;font-size:12px;text-align:center;">
        "Be patient for setups like this. This is your bread and butter trades."<br>
        Setup Sniper &mdash; {datetime.now().strftime('%Y-%m-%d %H:%M ET')}
      </div>
    </div>
    """


def format_nightly_email_html(candidates: list) -> str:
    """HTML email for the nightly watchlist summary."""
    if not candidates:
        return "<p>No runners found today. Clean slate for tomorrow.</p>"

    rows = ""
    for c in candidates:
        ticker = getattr(c, "ticker", "?")
        pct = getattr(c, "pct_change", getattr(c, "gap_pct", 0))
        rvol = getattr(c, "relative_vol", getattr(c, "volume_ratio", 0))
        close = getattr(c, "close_price", 0)
        high = getattr(c, "yesterday_high", getattr(c, "close_price", 0))
        atr_v = getattr(c, "daily_atr", None)
        atr_str = f"${atr_v:.2f}" if atr_v else "—"
        catalyst = getattr(c, "catalyst", "")

        rows += f"""
        <tr style="border-bottom:1px solid #2a2a3e;">
          <td style="padding:8px 12px;font-weight:700;color:#00ff88;">{ticker}</td>
          <td style="padding:8px 12px;text-align:right;">+{pct:.1f}%</td>
          <td style="padding:8px 12px;text-align:right;">{rvol:.1f}x</td>
          <td style="padding:8px 12px;text-align:right;">${close:.2f}</td>
          <td style="padding:8px 12px;text-align:right;">${high:.2f}</td>
          <td style="padding:8px 12px;text-align:right;">{atr_str}</td>
        </tr>"""
        if catalyst:
            rows += f"""
        <tr>
          <td colspan="6" style="padding:4px 12px 10px 12px;color:#aaa;font-size:12px;">
            📰 {catalyst}
          </td>
        </tr>"""

    return f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:600px;margin:0 auto;">
      <div style="background:#1a1a2e;color:#fff;padding:16px 20px;border-radius:8px 8px 0 0;">
        <h2 style="margin:0;font-size:18px;">📋 Runner Watchlist — {datetime.now().strftime('%Y-%m-%d')}</h2>
        <p style="margin:4px 0 0;color:#888;font-size:13px;">{len(candidates)} ticker(s) qualified. Morning check runs at 8:00 AM ET.</p>
      </div>

      <div style="background:#16213e;color:#eee;padding:0;">
        <table style="width:100%;border-collapse:collapse;font-size:13px;">
          <thead>
            <tr style="background:#0f3460;">
              <th style="padding:8px 12px;text-align:left;">Ticker</th>
              <th style="padding:8px 12px;text-align:right;">Change</th>
              <th style="padding:8px 12px;text-align:right;">Rel Vol</th>
              <th style="padding:8px 12px;text-align:right;">Close</th>
              <th style="padding:8px 12px;text-align:right;">High</th>
              <th style="padding:8px 12px;text-align:right;">ATR</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>

      <div style="background:#1a1a2e;color:#666;padding:12px 20px;border-radius:0 0 8px 8px;font-size:12px;text-align:center;">
        Setup Sniper &mdash; Nightly Scan
      </div>
    </div>
    """


def format_nightly_summary_console(candidates: list) -> str:
    """Format the nightly scan results for terminal display."""
    if not candidates:
        return f"\n{DIM}  No runners found today.{RESET}\n"

    lines = [
        "",
        f"{CYAN}{BOLD}{'━' * 60}{RESET}",
        f"{CYAN}{BOLD}  📋 RUNNER WATCHLIST — {datetime.now().strftime('%Y-%m-%d')}{RESET}",
        f"{CYAN}{BOLD}{'━' * 60}{RESET}",
        "",
        f"  {'Ticker':<8} {'Change':>8} {'RelVol':>8} {'Close':>8} {'High':>10} {'ATR':>8}",
        f"  {'─' * 52}",
    ]
    for c in candidates:
        ticker = getattr(c, "ticker", "?")
        pct = getattr(c, "pct_change", getattr(c, "gap_pct", 0))
        rvol = getattr(c, "relative_vol", getattr(c, "volume_ratio", 0))
        close = getattr(c, "close_price", 0)
        high = getattr(c, "yesterday_high", getattr(c, "close_price", 0))
        atr_v = getattr(c, "daily_atr", None)
        atr_str = f"${atr_v:.2f}" if atr_v else "N/A"
        catalyst = getattr(c, "catalyst", "")

        lines.append(
            f"  {GREEN}{BOLD}{ticker:<8}{RESET} "
            f"{pct:>+7.1f}% "
            f"{rvol:>7.1f}x "
            f"${close:>7.2f} "
            f"${high:>9.2f} "
            f"{atr_str:>8}"
        )
        if catalyst:
            lines.append(f"    {DIM}📰 {catalyst}{RESET}")

    lines.extend([
        "",
        f"  {DIM}Morning check will evaluate these at 8:00 AM ET{RESET}",
        f"{CYAN}{BOLD}{'━' * 60}{RESET}",
        "",
    ])
    return "\n".join(lines)


# ─── ALERT MANAGER ─────────────────────────────────────────────────────

class AlertManager:
    """Dispatch alerts to all configured channels: console, Discord, email, log."""

    def __init__(
        self,
        config: AlertConfig,
        briefing=None,
        universe_builder=None,
        universe_top_n: int = 500,
        consolidator=None,
    ):
        self.config = config
        self.log_path = Path(config.log_file)
        self.briefing = briefing
        self.universe_builder = universe_builder
        self.universe_top_n = universe_top_n
        self.consolidator = consolidator

        # Log which channels are active on init
        channels = ["console"] if config.console_output else []
        if config.discord_webhook_url:
            channels.append("discord")
        if getattr(config, "slack_webhook_url", None):
            channels.append("slack")
        if config.email_enabled:
            channels.append(f"email ({config.email_to})")
        channels.append("logfile")
        logger.info(f"Alert channels: {', '.join(channels)}")

    # ─── Catalyst enrichment helpers ─────────────────────────────────────

    def enrich_alert(self, alert):
        """Attach AI catalyst text to a single alert, if briefing is available."""
        if self.universe_builder:
            try:
                ud = self.universe_builder.get_ticker_data(
                    getattr(alert, "ticker", "") or ""
                )
                if ud:
                    parts = [f"Rank #{ud.rank}/{self.universe_top_n}"]
                    if ud.relative_volume >= 1.5:
                        parts.append(f"{ud.relative_volume:.1f}x rel vol")
                    if abs(ud.gap_pct) > 1:
                        parts.append(f"Gap {ud.gap_pct:+.1f}%")
                    setattr(alert, "universe_context", " | ".join(parts))
            except Exception as e:
                logger.debug(f"Universe context skipped: {e}")

        if not self.briefing:
            return
        try:
            text = self.briefing.brief_alert(alert)
            if text:
                setattr(alert, "catalyst", text.strip())
        except Exception as e:
            logger.error(f"Failed to enrich alert with catalyst: {e}")

    def enrich_candidates(self, candidates: list):
        """Attach AI catalyst text to nightly candidates in-place."""
        if not self.briefing or not candidates:
            return
        try:
            self.briefing.brief_candidates(candidates)
        except Exception as e:
            logger.error(f"Failed to enrich nightly candidates with catalyst: {e}")

    def send_alerts_batch(self, alerts: list, deliver_fn=None):
        """
        Optional consolidation + shadow mode. deliver_fn defaults to send_morning_alert.
        USE_CONSOLIDATOR / SHADOW_MODE read from environment each call.
        """
        if not alerts:
            return
        deliver = deliver_fn or self.send_morning_alert
        use_c = os.getenv("USE_CONSOLIDATOR", "false").lower() == "true"
        shadow = os.getenv("SHADOW_MODE", "false").lower() == "true"
        if self.consolidator and (use_c or shadow):
            r = self.consolidator.process_batch(alerts, datetime.now())
            if shadow:
                self.consolidator.append_shadow(r.shadow_rows)
            if use_c:
                for a in r.delivery_order:
                    deliver(a)
                return
        for a in alerts:
            deliver(a)

    def send_morning_alert(self, alert):
        """Send a setup alert to all configured channels."""
        # Optionally attach catalyst context before formatting
        self.enrich_alert(alert)
        if self.config.console_output:
            print(format_alert_console(alert))

        # Build levels table used by both Discord and Slack
        levels = []
        if hasattr(alert, "pm_low"):
            levels.append(f"{'PM Low (entry)':<18} ${alert.pm_low:.2f}")
        if hasattr(alert, "pm_high"):
            levels.append(f"{'PM High':<18} ${alert.pm_high:.2f}")
        if hasattr(alert, "pm_price"):
            levels.append(f"{'PM Price':<18} ${alert.pm_price:.2f}")
        if hasattr(alert, "ema_9_current"):
            levels.append(f"{'9 EMA':<18} ${alert.ema_9_current:.2f}")
        if getattr(alert, "daily_atr", None):
            levels.append(f"{'Daily ATR':<18} ${alert.daily_atr:.2f}")
        if getattr(alert, "gap_pct", None) is not None:
            levels.append(f"{'Gap':<18} {alert.gap_pct:+.1f}%")
        if getattr(alert, "eps_surprise_percent", None) is not None:
            levels.append(f"{'EPS Surprise':<18} {alert.eps_surprise_percent:+.1f}%")
        if getattr(alert, "universe_context", ""):
            levels.append(f"{'Universe':<18} {alert.universe_context}")
        levels_table = "\n".join(levels) if levels else ""
        setup_name = getattr(alert, "setup", "SETUP")
        direction = getattr(alert, "direction", "SHORT")
        entry = getattr(alert, "entry", "")
        exit_plan = getattr(alert, "exit_plan", "")

        if self.config.discord_webhook_url:
            content = (
                f"🔴 **{setup_name} — {alert.ticker}**\n"
                f"```\n{levels_table}\n```\n"
                f"**Entry:** {entry}\n"
                f"**Exit:** {exit_plan}"
            )
            self._send_discord({"content": content})

        if self.config.slack_webhook_url:
            self._send_slack(
                {
                    "blocks": [
                        {
                            "type": "header",
                            "text": {
                                "type": "plain_text",
                                "text": f"🔴 {setup_name} — {alert.ticker}",
                                "emoji": True,
                            },
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"```\n{levels_table}\n```",
                            },
                        },
                        {
                            "type": "context",
                            "elements": [
                                {
                                    "type": "mrkdwn",
                                    "text": f"*Entry:* {entry}   *Exit:* {exit_plan}",
                                }
                            ],
                        },
                    ]
                }
            )

        if self.config.email_enabled:
            self._send_email(
                subject=f"🔴 {getattr(alert, 'setup', 'SETUP')} — {alert.ticker} ({getattr(alert, 'direction', 'SHORT')})",
                html=format_alert_email_html(alert),
            )

        self._log_alert(alert)

    def send_nightly_summary(self, candidates: list):
        """Send the nightly scan summary."""
        # Optionally attach catalyst context per candidate before formatting
        self.enrich_candidates(candidates)
        if self.config.console_output:
            print(format_nightly_summary_console(candidates))

        # Build shared table for Discord and Slack
        table = ""
        if candidates:
            tickers_str = ", ".join(getattr(c, "ticker", "?") for c in candidates)
            header = f"{'Ticker':<8} {'Change':>8} {'RelVol':>8} {'Close':>8} {'ATR':>8}"
            separator = "─" * 44
            rows = []
            for c in candidates:
                ticker = getattr(c, "ticker", "?")
                pct = getattr(c, "pct_change", getattr(c, "gap_pct", 0))
                rvol = getattr(c, "relative_vol", getattr(c, "volume_ratio", 0))
                close = getattr(c, "close_price", 0)
                atr_v = getattr(c, "daily_atr", None)
                atr_str = f"${atr_v:.2f}" if atr_v else "  N/A"
                rows.append(f"{ticker:<8} {pct:>+7.1f}% {rvol:>7.1f}x ${close:>7.2f} {atr_str:>8}")
            table = f"{header}\n{separator}\n" + "\n".join(rows)

        if self.config.discord_webhook_url and candidates:
            payload = {
                "content": f"📋 **Watchlist — {len(candidates)} ticker(s)**\n```\n{table}\n```\nMorning check at 8:00 AM ET."
            }
            self._send_discord(payload)

        if self.config.slack_webhook_url:
            if candidates:
                self._send_slack(
                    {
                        "blocks": [
                            {
                                "type": "header",
                                "text": {
                                    "type": "plain_text",
                                    "text": f"📋 Watchlist — {len(candidates)} ticker(s)",
                                    "emoji": True,
                                },
                            },
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"```\n{table}\n```",
                                },
                            },
                            {
                                "type": "context",
                                "elements": [
                                    {
                                        "type": "mrkdwn",
                                        "text": "Morning check at 8:00 AM ET.",
                                    }
                                ],
                            },
                        ]
                    }
                )
            else:
                self._send_slack(
                    {
                        "blocks": [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": "📋 *Watchlist* — No runners today.",
                                },
                            }
                        ]
                    }
                )

        if self.config.email_enabled:
            count = len(candidates)
            if candidates:
                tickers = ", ".join(getattr(c, "ticker", "?") for c in candidates)
                subject = f"📋 Watchlist — {count} runner(s): {tickers}"
            else:
                subject = "📋 Watchlist — No runners today"
            self._send_email(
                subject=subject,
                html=format_nightly_email_html(candidates),
            )

    # ─── Discord ──────────────────────────────────────────────────────

    def _send_discord(self, payload: dict):
        """POST to Discord webhook."""
        try:
            resp = requests.post(
                self.config.discord_webhook_url,
                json=payload,
                timeout=5,
            )
            if resp.status_code not in (200, 204):
                logger.warning(f"Discord webhook returned {resp.status_code}")
            else:
                logger.debug("Discord alert sent")
        except Exception as e:
            logger.error(f"Discord delivery failed: {e}")

    def _send_slack(self, payload: dict):
        """POST to Slack incoming webhook."""
        try:
            resp = requests.post(
                self.config.slack_webhook_url,
                json=payload,
                timeout=5,
            )
            if resp.status_code != 200:
                logger.warning(f"Slack webhook returned {resp.status_code}: {resp.text[:100]}")
            else:
                logger.debug("Slack alert sent")
        except Exception as e:
            logger.error(f"Slack delivery failed: {e}")

    def _send_email(self, subject: str, html: str):
        """Send HTML email via SMTP (Gmail-friendly)."""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{self.config.email_sender_name} <{self.config.email_from}>"
        msg["To"] = self.config.email_to
        msg.attach(MIMEText(html, "html"))

        try:
            with smtplib.SMTP(self.config.smtp_server, self.config.smtp_port, timeout=10) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self.config.email_login, self.config.email_password)
                server.sendmail(self.config.email_from, [self.config.email_to], msg.as_string())
                logger.debug(f"Email sent to {self.config.email_to}")
        except smtplib.SMTPAuthenticationError as e:
            logger.error(
                "SMTP authentication failed when sending email. "
                "If you're using Gmail, create an App Password and set ALERT_EMAIL_PASSWORD to that value. "
                f"Details: {e}"
            )
        except Exception as e:
            logger.error(f"Email delivery failed: {e}")

    # ─── Log File ─────────────────────────────────────────────────────

    def _log_alert(self, alert):
        """Append alert to log file for journaling. Duck-typed for all setup types."""
        level_fields = [
            "pm_price",
            "pm_high",
            "pm_low",
            "yesterday_high",
            "ema_9_current",
            "ema_200_daily",
            "daily_atr",
            "gap_pct",
            "eps_surprise_percent",
            "revenue_surprise_percent",
            "five_day_pct_change",
            "volume_ratio",
            "universe_context",
        ]
        levels = {}
        for field in level_fields:
            val = getattr(alert, field, None)
            if val is not None:
                levels[field] = val

        entry = {
            "timestamp": datetime.now().isoformat(),
            "ticker": getattr(alert, "ticker", "UNKNOWN"),
            "setup": getattr(alert, "setup", "UNKNOWN"),
            "direction": getattr(alert, "direction", ""),
            "levels": levels,
            "entry": getattr(alert, "entry", ""),
            "exit": getattr(alert, "exit_plan", ""),
            "confidence": getattr(alert, "confidence", ""),
        }
        try:
            with open(self.log_path, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.error(f"Failed to write alert log: {e}")
