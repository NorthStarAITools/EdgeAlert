#!/usr/bin/env python3
"""Edge Alert — Telegram Preview Channel.

Free tier marketing funnel: sends DELAYED (1+ hour old) signals to a public
preview channel. Paid users get real-time alerts in a private channel.

Usage:
    python3 telegram_preview.py                # Scan and send delayed signals
    python3 telegram_preview.py --reset-sent   # Clear preview_sent.jsonl (debug)
    python3 telegram_preview.py --test         # Send test message to preview channel
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ── Config ───────────────────────────────────────────────────────────────────

def load_config():
    """Load config from .env file."""
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())

    return {
        "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "preview_channel_id": os.environ.get("TELEGRAM_PREVIEW_CHANNEL_ID", ""),
    }


TELEGRAM_API = "https://api.telegram.org/bot{token}"
PREVIEW_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PREVIEW_DIR, "data")
SIGNALS_FILE = os.path.join(DATA_DIR, "signals.jsonl")
PREVIEW_SENT_FILE = os.path.join(DATA_DIR, "preview_sent.jsonl")

# Signals older than this are eligible to send to preview
PREVIEW_DELAY_MINUTES = 60

# Stripe links (from CLAUDE.md)
STRIPE_BASIC = "https://buy.stripe.com/8x2cN72UBanhgjN93r00000"
STRIPE_PRO = "https://buy.stripe.com/3cIbJ3fHn2UP4B50wV00001"


# ── Utilities ────────────────────────────────────────────────────────────────

def load_preview_sent():
    """Load set of (ticker, side) already sent to preview."""
    if not os.path.exists(PREVIEW_SENT_FILE):
        return set()
    sent = set()
    with open(PREVIEW_SENT_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                sent.add(line)
    return sent


def save_preview_sent(sent_set):
    """Save set of (ticker, side) sent to preview."""
    with open(PREVIEW_SENT_FILE, "w") as f:
        for item in sorted(sent_set):
            f.write(item + "\n")


def load_all_signals():
    """Load all signals from signals.jsonl."""
    signals = []
    if not os.path.exists(SIGNALS_FILE):
        return signals
    with open(SIGNALS_FILE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    signals.append(json.loads(line))
                except:
                    pass
    return signals


def send_telegram_message(token, chat_id, text, parse_mode="Markdown"):
    """Send a message to Telegram. Returns True on success."""
    url = f"{TELEGRAM_API.format(token=token)}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            return True
        else:
            print(f"  [!] Telegram error {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        print(f"  [!] Telegram send failed: {e}")
        return False


# ── Preview Logic ────────────────────────────────────────────────────────────

def format_preview_signal(sig):
    """Format a single signal for preview channel."""
    ticker = sig.get("ticker", "?")
    side = sig.get("side", "?").upper()
    edge = sig.get("edge_pct", 0)
    confidence = sig.get("confidence", "?")
    market_type = sig.get("market_type", "?").upper()

    # Color emoji by confidence
    conf_emoji = {
        "HIGH": "🟢",
        "MEDIUM": "🟡",
        "SPECULATIVE": "🔴",
    }.get(confidence, "⚪")

    msg = (
        f"{conf_emoji} *{side} {ticker}*\n"
        f"Market: {market_type} | Edge: {edge:+.1f}% | Tier: {confidence}\n"
        f"_(Signal from ~1 hour ago)_"
    )
    return msg


def cmd_scan(config):
    """Scan signals and send old ones to preview channel."""
    token = config["bot_token"]
    preview_channel = config["preview_channel_id"]

    if not token or not preview_channel:
        print("ERROR: Set TELEGRAM_BOT_TOKEN and TELEGRAM_PREVIEW_CHANNEL_ID in .env")
        sys.exit(1)

    # Load all signals
    signals = load_all_signals()
    if not signals:
        print("  No signals found.")
        return

    # Load already-sent set
    sent = load_preview_sent()

    # Filter for old signals not yet sent
    now = datetime.now(timezone.utc)
    old_signals = []

    for sig in signals:
        # Get signal timestamp
        scan_time_str = sig.get("scan_time", "")
        try:
            sig_time = datetime.fromisoformat(scan_time_str.replace("Z", "+00:00"))
        except:
            continue

        age = now - sig_time
        if age < timedelta(minutes=PREVIEW_DELAY_MINUTES):
            continue  # Too fresh

        # Check if already sent
        key = f"{sig.get('ticker')}:{sig.get('side')}"
        if key in sent:
            continue

        old_signals.append((sig, key))

    if not old_signals:
        print("  No new delayed signals to send.")
        return

    # Send to preview channel, batch by market type for readability
    sent_keys = set(sent)
    crypto_sigs = [s for s, _ in old_signals if s.get("market_type") == "crypto"]
    sports_sigs = [s for s, _ in old_signals if s.get("market_type") == "sports"]

    for batch_name, batch in [("Crypto", crypto_sigs), ("Sports", sports_sigs)]:
        if not batch:
            continue

        # Build batch message
        lines = [f"🔔 *{batch_name} Signals (Delayed)*"]
        for sig in batch[:5]:  # Limit to 5 per batch
            lines.append(format_preview_signal(sig))
            key = f"{sig.get('ticker')}:{sig.get('side')}"
            sent_keys.add(key)

        lines.append("")
        lines.append(
            f"🔒 *Want real-time alerts?*\n"
            f"[Basic Plan - $39/mo]({STRIPE_BASIC}) | [Pro Plan - $79/mo]({STRIPE_PRO})"
        )

        msg = "\n".join(lines)
        ok = send_telegram_message(token, preview_channel, msg)
        if ok:
            print(f"  Sent {len(batch)} {batch_name.lower()} signal(s) to preview channel.")
        else:
            print(f"  Failed to send {batch_name.lower()} signals.")
            return

    # Save updated sent set
    save_preview_sent(sent_keys)


def cmd_test(config):
    """Send a test message to preview channel."""
    token = config["bot_token"]
    preview_channel = config["preview_channel_id"]

    if not token or not preview_channel:
        print("ERROR: Set TELEGRAM_BOT_TOKEN and TELEGRAM_PREVIEW_CHANNEL_ID in .env")
        sys.exit(1)

    ts = datetime.now(timezone.utc).isoformat()[:19]
    msg = (
        f"🧪 *Edge Alert — Preview Channel Test*\n\n"
        f"If you see this, the preview channel is ready.\n"
        f"Delayed signals will appear here before real-time subscribers get them.\n\n"
        f"_Test sent: {ts}Z_"
    )
    ok = send_telegram_message(token, preview_channel, msg)
    print(f"  Test message: {'OK' if ok else 'FAILED'}")


def cmd_reset_sent():
    """Clear preview_sent.jsonl (debug only)."""
    if os.path.exists(PREVIEW_SENT_FILE):
        os.remove(PREVIEW_SENT_FILE)
        print(f"  Cleared {PREVIEW_SENT_FILE}")
    else:
        print("  preview_sent.jsonl not found.")


def main():
    parser = argparse.ArgumentParser(description="Edge Alert — Preview Channel")
    parser.add_argument("--scan", action="store_true", help="Scan and send delayed signals (default)")
    parser.add_argument("--test", action="store_true", help="Send test message")
    parser.add_argument("--reset-sent", action="store_true", help="Clear preview_sent.jsonl (debug)")

    args = parser.parse_args()

    if args.reset_sent:
        cmd_reset_sent()
        return

    config = load_config()

    if args.test:
        cmd_test(config)
    else:
        # Default: scan
        cmd_scan(config)


if __name__ == "__main__":
    main()
