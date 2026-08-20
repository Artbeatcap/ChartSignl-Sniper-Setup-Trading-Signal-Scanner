#!/usr/bin/env python3
"""Copy injected process env vars into a gitignored .env (mode 0600).

Cursor Cloud Secrets arrive as environment variables. Docker Compose and
python-dotenv both read `.env`, so this keeps local/container runs in sync
without committing credentials.

Existing .env keys are preserved unless the process environment has a
non-empty value for that key. Values are never printed.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

# Keys the scanner actually reads. Keep in sync with .env.template / config.py.
SYNC_KEYS = (
    "MASSIVE_API_KEY",
    "DISCORD_WEBHOOK_URL",
    "SLACK_WEBHOOK_URL",
    "ALERT_EMAIL_FROM",
    "ALERT_EMAIL_PASSWORD",
    "ALERT_EMAIL_TO",
    "ALERT_EMAIL_LOGIN",
    "ALERT_EMAIL_SENDER_NAME",
    "FMP_API_KEY",
    "ANTHROPIC_API_KEY",
    "INTRADAY_TICKERS",
    "UNIVERSE_SIZE",
    "USE_CONSOLIDATOR",
    "SHADOW_MODE",
    "DATA_DIR",
    "LOG_DIR",
)


def _parse_env_file(text: str) -> tuple[list[str], dict[str, str]]:
    """Return (ordered keys including comments as sentinels, assignments)."""
    keys: list[str] = []
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            keys.append(line)
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        if not name:
            keys.append(line)
            continue
        values[name] = value
        keys.append(name)
    return keys, values


def _write_env(path: Path, keys_order: list[str], values: dict[str, str]) -> None:
    lines: list[str] = []
    seen: set[str] = set()
    for item in keys_order:
        if item in values and item not in seen:
            lines.append(f"{item}={values[item]}")
            seen.add(item)
        elif item not in values:
            lines.append(item)
    for name, value in values.items():
        if name not in seen:
            lines.append(f"{name}={value}")
            seen.add(name)
    body = "\n".join(lines).rstrip() + "\n"
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o600)


def main() -> int:
    keys_order: list[str] = []
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        keys_order, values = _parse_env_file(ENV_PATH.read_text(encoding="utf-8"))

    changed = False
    for name in SYNC_KEYS:
        injected = os.environ.get(name)
        if not injected:
            continue
        if values.get(name) != injected:
            values[name] = injected
            if name not in keys_order:
                keys_order.append(name)
            changed = True

    if not ENV_PATH.exists() and not values:
        return 0

    if changed or not ENV_PATH.exists():
        _write_env(ENV_PATH, keys_order, values)

    has_key = bool(values.get("MASSIVE_API_KEY") or os.environ.get("MASSIVE_API_KEY"))
    status = "present" if has_key else "missing"
    print(f"materialize_env: MASSIVE_API_KEY {status} (.env mode 0600)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
