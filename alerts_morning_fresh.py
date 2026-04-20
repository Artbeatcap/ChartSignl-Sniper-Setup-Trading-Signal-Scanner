"""
alerts_morning_fresh.py
─────────────────────────────────────────────────────────────────────────
Discord / Slack / console formatters for the 8:30 AM premarket fresh scan.

Kept in a separate file so it doesn't pollute alerts.py, which is already
large and handles the nightly + morning (runner fade) formats.
"""

from typing import Any, Dict, List

from scanner_premarket_fresh import PremktMover


# ─── DIRECTION EMOJI MAP ──────────────────────────────────────────────
_DIR_EMOJI = {
    "SHORT":               "🔴",
    "SHORT (conditional)": "🟡",
    "LONG (Day Trade Only)":"🟢",
    "LONG":                "🟢",
    "WATCH":               "⚪",
}

_CONF_EMOJI = {"HIGH": "🔥", "MEDIUM": "⚡", "LOW": "💡"}


# ─── DISCORD ─────────────────────────────────────────────────────────

def build_discord_morning_fresh(result: Dict[str, Any]) -> List[dict]:
    """
    Returns a list of Discord webhook payloads — one per batch of movers.
    Discord embeds cap at 25 fields per message, so we chunk if needed.
    """
    movers: List[PremktMover] = result.get("movers", [])
    bias: Dict[str, Any] = result.get("market_bias", {})
    scan_time: str = result.get("scan_time", "")

    payloads = []

    # ── Header message (market bias) ─────────────────────────────────
    spy = bias.get("SPY", {})
    qqq = bias.get("QQQ", {})
    uvxy = bias.get("UVXY", {})
    regime = bias.get("regime", "UNKNOWN")

    regime_color = {
        "BEARISH OPEN": 0xE74C3C,
        "BULLISH OPEN": 0x2ECC71,
        "NEUTRAL OPEN": 0xF39C12,
        "VOLATILE OPEN": 0x9B59B6,
    }.get(regime, 0x95A5A6)

    spy_line = f"SPY  {spy.get('direction','?')} {spy.get('gap_pct',0):+.2f}%  •  ${spy.get('pm_price',0):.2f}" if spy else "SPY  —"
    qqq_line = f"QQQ  {qqq.get('direction','?')} {qqq.get('gap_pct',0):+.2f}%  •  ${qqq.get('pm_price',0):.2f}" if qqq else "QQQ  —"
    uvxy_line = (
        f"UVXY  ${uvxy.get('pm_price',0):.2f} (VIX proxy)" if uvxy else "UVXY  — (VIX proxy)"
    )

    if not movers:
        payloads.append({
            "embeds": [{
                "title": f"📋 8:30 AM Premarket Fresh Scan — No New Setups",
                "description": (
                    f"```\n{spy_line}\n{qqq_line}\n{uvxy_line}\n```\n"
                    f"All significant premarket movers are already on tonight's watchlist, "
                    f"or no movers qualified above the gap/volume thresholds."
                ),
                "color": regime_color,
                "footer": {"text": scan_time},
            }]
        })
        return payloads

    # ── Per-mover embeds ─────────────────────────────────────────────
    header_embed = {
        "title": f"📋 8:30 AM Premarket Fresh Scan — {len(movers)} New Mover(s)",
        "description": (
            f"**{regime}**\n"
            f"```\n{spy_line}\n{qqq_line}\n{uvxy_line}\n```"
        ),
        "color": regime_color,
        "footer": {"text": scan_time},
    }
    payloads.append({"embeds": [header_embed]})

    for mover in movers:
        dir_emoji = _DIR_EMOJI.get(mover.direction, "⚪")
        conf_emoji = _CONF_EMOJI.get(mover.confidence, "")
        tag_str = " | ".join(mover.setup_tags)

        levels_lines = [
            f"{'PM Price':<18} ${mover.pm_price:.2f}  ({mover.gap_pct:+.1f}% gap)",
            f"{'PM High':<18} ${mover.pm_high:.2f}",
            f"{'PM Low':<18} ${mover.pm_low:.2f}",
            f"{'Prev Close':<18} ${mover.prev_close:.2f}",
        ]
        if mover.ema_9_pm:
            below = "✓ below" if mover.pm_price < mover.ema_9_pm else "✗ ABOVE"
            levels_lines.append(f"{'9 EMA (PM)':<18} ${mover.ema_9_pm:.2f}  ({below})")
        if mover.ema_200_daily:
            levels_lines.append(f"{'200 EMA (daily)':<18} ${mover.ema_200_daily:.2f}")
        if mover.daily_atr:
            levels_lines.append(f"{'Daily ATR':<18} ${mover.daily_atr:.2f}")
        if mover.near_ath:
            levels_lines.append(f"{'⚠ NEAR ATH':<18} ${mover.all_time_high:.2f}")
        rn = mover.near_round_number
        if rn:
            levels_lines.append(f"{'⚠ Near $' + str(int(rn)):<18} Round number wall")
        pm_vol_pct = mover.relative_pm_volume * 100
        levels_lines.append(f"{'PM Volume':<18} {mover.pm_volume:,}  ({pm_vol_pct:.0f}% of avg day)")

        levels_block = "\n".join(levels_lines)

        embed: Dict[str, Any] = {
            "title": f"{dir_emoji} {mover.ticker}  {conf_emoji} {mover.confidence}",
            "description": (
                f"**{tag_str}**\n\n"
                f"```\n{levels_block}\n```\n"
                f"**Entry:** {mover.entry_note}\n"
                f"**Exit:**  {mover.exit_note}"
            ),
            "color": {
                "SHORT":               0xE74C3C,
                "SHORT (conditional)": 0xF39C12,
                "LONG (Day Trade Only)":0x2ECC71,
                "WATCH":               0x95A5A6,
            }.get(mover.direction, 0x95A5A6),
        }

        if mover.warning:
            embed["fields"] = [{
                "name": "⚠ Guardrails",
                "value": mover.warning,
                "inline": False,
            }]

        payloads.append({"embeds": [embed]})

    return payloads


# ─── SLACK ────────────────────────────────────────────────────────────

def build_slack_morning_fresh(result: Dict[str, Any]) -> dict:
    """
    Returns a single Slack Block Kit payload for the full morning fresh scan.
    All movers in one message to keep the channel clean.
    """
    movers: List[PremktMover] = result.get("movers", [])
    bias: Dict[str, Any] = result.get("market_bias", {})
    scan_time: str = result.get("scan_time", "")

    spy = bias.get("SPY", {})
    qqq = bias.get("QQQ", {})
    uvxy = bias.get("UVXY", {})
    regime = bias.get("regime", "UNKNOWN")

    spy_line = f"SPY  {spy.get('direction','?')} {spy.get('gap_pct',0):+.2f}%   ${spy.get('pm_price',0):.2f}" if spy else "SPY  —"
    qqq_line = f"QQQ  {qqq.get('direction','?')} {qqq.get('gap_pct',0):+.2f}%   ${qqq.get('pm_price',0):.2f}" if qqq else "QQQ  —"
    uvxy_line = (
        f"UVXY  ${uvxy.get('pm_price',0):.2f} (VIX proxy)" if uvxy else "UVXY  — (VIX proxy)"
    )

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📋 8:30 AM Premarket Fresh Scan — {len(movers)} New Mover(s)",
                "emoji": True,
            }
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{regime}*\n```{spy_line}\n{qqq_line}\n{uvxy_line}```"
            }
        },
        {"type": "divider"},
    ]

    if not movers:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "No new setups — all qualified movers already on the nightly watchlist, or nothing passed gap/volume filters."
            }
        })
    else:
        for mover in movers:
            dir_emoji = _DIR_EMOJI.get(mover.direction, "⚪")
            conf_emoji = _CONF_EMOJI.get(mover.confidence, "")
            tag_str = " | ".join(mover.setup_tags)

            row = lambda label, val: f"{label:<18} {val}"
            rows = [
                row("PM Price", f"${mover.pm_price:.2f}  ({mover.gap_pct:+.1f}% gap)"),
                row("PM High/Low", f"${mover.pm_high:.2f} / ${mover.pm_low:.2f}"),
                row("Prev Close", f"${mover.prev_close:.2f}"),
            ]
            if mover.ema_9_pm:
                below = "below ✓" if mover.pm_price < mover.ema_9_pm else "ABOVE ✗"
                rows.append(row("9 EMA (PM)", f"${mover.ema_9_pm:.2f}  ({below})"))
            if mover.daily_atr:
                rows.append(row("Daily ATR", f"${mover.daily_atr:.2f}"))
            if mover.near_ath:
                rows.append(row("⚠ NEAR ATH", f"${mover.all_time_high:.2f}"))
            pm_vol_pct = mover.relative_pm_volume * 100
            rows.append(row("PM Volume", f"{mover.pm_volume:,}  ({pm_vol_pct:.0f}% of avg day)"))

            table = "\n".join(rows)

            body = (
                f"*{tag_str}*\n"
                f"```{table}```\n"
                f"*Entry:* {mover.entry_note}\n"
                f"*Exit:*  {mover.exit_note}"
            )
            if mover.warning:
                body += f"\n⚠ _{mover.warning}_"

            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{dir_emoji} *{mover.ticker}*  {conf_emoji} {mover.confidence}\n{body}"
                }
            })
            blocks.append({"type": "divider"})

    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"Scan time: {scan_time}"}]
    })

    return {"blocks": blocks}


def _dir_ascii(d: str) -> str:
    """Bias direction for terminals that cannot render Unicode arrows."""
    return {"▲": "up", "▼": "dn", "—": "flat"}.get(d, d if d else "?")


# ─── CONSOLE ─────────────────────────────────────────────────────────

def build_console_morning_fresh(result: Dict[str, Any]) -> str:
    """Terminal output for local testing / log review (ASCII-safe for Windows consoles)."""
    movers: List[PremktMover] = result.get("movers", [])
    bias: Dict[str, Any] = result.get("market_bias", {})
    scan_time: str = result.get("scan_time", "")

    lines = [
        "",
        "+" + "-" * 58 + "+",
        "|     8:30 AM PREMARKET FRESH SCAN                      |",
        "+" + "-" * 58 + "+",
        "",
    ]

    spy = bias.get("SPY", {})
    qqq = bias.get("QQQ", {})
    uvxy = bias.get("UVXY", {})
    regime = bias.get("regime", "?")

    lines += [
        f"  MARKET BIAS: {regime}",
        f"  SPY  {_dir_ascii(spy.get('direction','?'))} {spy.get('gap_pct',0):+.2f}%   ${spy.get('pm_price',0):.2f}" if spy else "  SPY  --",
        f"  QQQ  {_dir_ascii(qqq.get('direction','?'))} {qqq.get('gap_pct',0):+.2f}%   ${qqq.get('pm_price',0):.2f}" if qqq else "  QQQ  --",
        f"  UVXY  ${uvxy.get('pm_price',0):.2f} (VIX proxy)" if uvxy else "  UVXY  -- (VIX proxy)",
        "",
        f"  {len(movers)} NEW MOVER(S) -- {scan_time}",
        "  " + "-" * 56,
    ]

    dir_mark = {
        "SHORT": "[S]",
        "SHORT (conditional)": "[S?]",
        "LONG (Day Trade Only)": "[L]",
        "LONG": "[L]",
        "WATCH": "[W]",
    }

    if not movers:
        lines.append("  No new setups this morning.")
    else:
        for m in movers:
            mark = dir_mark.get(m.direction, "[?]")
            lines += [
                "",
                f"  {mark}  {m.ticker}  [{m.confidence}]  {m.gap_pct:+.1f}% gap",
                f"     {' | '.join(m.setup_tags)}",
                f"     PM: ${m.pm_price:.2f}  H: ${m.pm_high:.2f}  L: ${m.pm_low:.2f}",
            ]
            if m.ema_9_pm:
                flag = "below 9EMA" if m.pm_price < m.ema_9_pm else "ABOVE 9EMA"
                lines.append(f"     9 EMA: ${m.ema_9_pm:.2f}  {flag}")
            if m.daily_atr:
                lines.append(f"     ATR: ${m.daily_atr:.2f}")
            lines.append(f"     Entry: {m.entry_note}")
            lines.append(f"     Exit:  {m.exit_note}")
            if m.warning:
                lines.append(f"     ! {m.warning}")
            lines.append("     " + "-" * 52)

    lines.append("")
    return "\n".join(lines)
