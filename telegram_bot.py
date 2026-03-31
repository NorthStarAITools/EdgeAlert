#!/usr/bin/env python3
"""Edge Alert — Telegram Bot Delivery.

Sends signal alerts to a Telegram channel/group.
Supports:
  - Real-time alerts (push signals as they're found)
  - Daily digest (scheduled summary)
  - Subscriber management via bot commands

Setup:
  1. Create a bot via @BotFather on Telegram
  2. Get the bot token
  3. Create a channel/group and add the bot as admin
  4. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env or config

Usage:
    python3 telegram_bot.py --scan           # Scan and send alerts
    python3 telegram_bot.py --digest         # Send daily digest
    python3 telegram_bot.py --loop           # Continuous scan + alert
    python3 telegram_bot.py --test           # Send test message
"""

import argparse
import json
import os
import sys
import time
import requests
from datetime import datetime, timezone

from edge_engine import scan_all
from formatter import format_telegram, format_daily_digest
from edge_alert import load_todays_signals, save_signals

# ── Config ───────────────────────────────────────────────────────────────────

# Load from environment or .env file
def load_config():
    """Load Telegram config from environment or .env file."""
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
        "signals_chat_id": os.environ.get("TELEGRAM_SIGNALS_CHAT_ID", ""),
        "ops_chat_id": os.environ.get("TELEGRAM_OPS_CHAT_ID", ""),
        "ceo_chat_id": os.environ.get("TELEGRAM_CEO_CHAT_ID", ""),
    }


TELEGRAM_API = "https://api.telegram.org/bot{token}"
SCAN_INTERVAL = 60  # seconds between scans in loop mode
MIN_EDGE = 1.0       # minimum edge to alert on


# ── Telegram API ─────────────────────────────────────────────────────────────

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


# ── Commands ─────────────────────────────────────────────────────────────────

def cmd_scan(config):
    """One-shot: scan and send alerts to Signals channel + ops notification."""
    token = config["bot_token"]
    results = scan_all(min_edge_pct=MIN_EDGE)
    save_signals(results)

    n = results["total_signals"]
    if n == 0:
        print("  No signals found. Skipping Telegram send.")
        return

    # Send to Signals channel (customer-facing)
    msg = format_telegram(results)
    ok = send_telegram_message(token, config["signals_chat_id"], msg)
    if ok:
        print(f"  Sent {n} signal(s) to Signals channel.")
    else:
        print("  Failed to send to Signals channel.")

    # Ops channel — internal summary
    crypto_n = len(results.get("crypto", []))
    sports_n = len(results.get("sports", []))
    ops_msg = (
        f"📡 *Scan Complete*\n"
        f"{n} signal(s) delivered — Crypto: {crypto_n}, Sports: {sports_n}\n"
        f"_{results.get('scan_time', '')[:19]}Z_"
    )
    send_telegram_message(token, config["ops_chat_id"], ops_msg)

    # CEO DM for HIGH confidence signals
    all_sigs = results.get("crypto", []) + results.get("sports", [])
    high_sigs = [s for s in all_sigs if s.confidence == "HIGH"]
    if high_sigs and config["ceo_chat_id"]:
        high_lines = [f"🟢 *{len(high_sigs)} HIGH confidence signal(s):*\n"]
        for s in high_sigs[:5]:
            high_lines.append(
                f"• *{s.side.upper()} {s.ticker}* @ {s.our_price_cents}c — "
                f"Edge: `{s.edge_pct:+.1f}%` EV: `{s.ev_per_contract:.1f}c`"
            )
        send_telegram_message(token, config["ceo_chat_id"], "\n".join(high_lines))


def cmd_digest(config):
    """Send daily digest to both Signals and Ops channels."""
    scans = load_todays_signals()
    if not scans:
        print("No signals found for today.")
        return

    # Reconstruct for formatter
    from edge_engine import Signal
    scan_history = []
    for scan_data in scans:
        reconstructed = {
            "scan_time": scan_data.get("scan_time"),
            "total_signals": scan_data.get("total_signals", 0),
            "crypto": [],
            "sports": [],
        }
        for mtype in ["crypto", "sports"]:
            for raw in scan_data.get(mtype, []):
                try:
                    sig = Signal(**{k: v for k, v in raw.items() if k != "summary"})
                    reconstructed[mtype].append(sig)
                except Exception:
                    pass
        scan_history.append(reconstructed)

    digest_text = format_daily_digest(scan_history)

    # Telegram has a 4096 char limit — truncate if needed
    if len(digest_text) > 4000:
        digest_text = digest_text[:3950] + "\n\n_...truncated. Full digest in app._"

    # Send to Signals channel (customer-facing)
    ok = send_telegram_message(config["bot_token"], config["signals_chat_id"], digest_text)
    if ok:
        print(f"  Daily digest sent to Signals channel ({len(scans)} scans, {sum(s.get('total_signals',0) for s in scans)} signals).")

    # Send summary to Ops channel (internal)
    total_sigs = sum(s.get('total_signals', 0) for s in scans)
    ops_msg = f"📊 *Daily Digest Sent*\n{len(scans)} scans | {total_sigs} signals\nDelivered to Edge Alert Signals channel."
    send_telegram_message(config["bot_token"], config["ops_chat_id"], ops_msg)


def cmd_loop(config):
    """Continuous scan and alert mode with multi-channel delivery."""
    token = config["bot_token"]
    print(f"  Edge Alert — Telegram Loop Mode")
    print(f"  Scan interval: {SCAN_INTERVAL}s")
    print(f"  Min edge: {MIN_EDGE}%")
    print(f"  Signals channel: {config['signals_chat_id']}")
    print(f"  Ops channel: {config['ops_chat_id']}")
    print(f"  Press Ctrl+C to stop.\n")

    # Startup notifications
    send_telegram_message(
        token, config["signals_chat_id"],
        "🔔 *Edge Alert Started*\nScanning prediction markets for +EV signals...",
    )
    send_telegram_message(
        token, config["ops_chat_id"],
        f"🚀 *Edge Alert Loop Started*\nInterval: {SCAN_INTERVAL}s | Min edge: {MIN_EDGE}%",
    )

    scan_count = 0
    signals_sent = 0
    seen_tickers = set()  # Deduplicate within a session

    try:
        while True:
            scan_count += 1
            results = scan_all(min_edge_pct=MIN_EDGE)
            save_signals(results)

            n = results["total_signals"]
            if n > 0:
                # Filter out signals already sent this session (same ticker+side)
                all_sigs = results.get("crypto", []) + results.get("sports", [])
                new_sigs = [s for s in all_sigs if f"{s.ticker}:{s.side}" not in seen_tickers]

                if new_sigs:
                    # Build filtered results for formatting
                    filtered = {
                        "crypto": [s for s in new_sigs if s.market_type == "crypto"],
                        "sports": [s for s in new_sigs if s.market_type == "sports"],
                        "scan_time": results["scan_time"],
                        "total_signals": len(new_sigs),
                    }
                    msg = format_telegram(filtered)
                    ok = send_telegram_message(token, config["signals_chat_id"], msg)
                    if ok:
                        signals_sent += len(new_sigs)
                        for s in new_sigs:
                            seen_tickers.add(f"{s.ticker}:{s.side}")
                        print(f"  [{scan_count}] Sent {len(new_sigs)} new signal(s) — {signals_sent} total")

                    # CEO DM for HIGH confidence new signals
                    high_new = [s for s in new_sigs if s.confidence == "HIGH"]
                    if high_new and config["ceo_chat_id"]:
                        high_lines = [f"🟢 *{len(high_new)} HIGH signal(s):*\n"]
                        for s in high_new[:5]:
                            high_lines.append(
                                f"• *{s.side.upper()} {s.ticker}* @ {s.our_price_cents}c — "
                                f"Edge: `{s.edge_pct:+.1f}%`"
                            )
                        send_telegram_message(token, config["ceo_chat_id"], "\n".join(high_lines))
                else:
                    ts = datetime.now().strftime("%H:%M:%S")
                    print(f"  [{scan_count}] {ts} — {n} signal(s), all already sent", end="\r")
            else:
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"  [{scan_count}] {ts} — no signals", end="\r")

            # Periodic ops heartbeat every 30 scans
            if scan_count % 30 == 0:
                send_telegram_message(
                    token, config["ops_chat_id"],
                    f"💓 *Heartbeat* — Scan #{scan_count} | {signals_sent} signals sent | "
                    f"{len(seen_tickers)} unique tickers",
                )

            time.sleep(SCAN_INTERVAL)

    except KeyboardInterrupt:
        shutdown_msg = f"🔕 *Edge Alert Stopped*\n{scan_count} scans, {signals_sent} signals sent."
        print(f"\n\n  Stopped. {scan_count} scans, {signals_sent} signals sent.")
        send_telegram_message(token, config["signals_chat_id"], shutdown_msg)
        send_telegram_message(token, config["ops_chat_id"], shutdown_msg)


def cmd_test(config):
    """Send a test message to verify Telegram setup for all channels."""
    token = config["bot_token"]
    ts = datetime.now(timezone.utc).isoformat()[:19]

    # Test Signals channel
    signals_msg = (
        "🧪 *Edge Alert — Test Message*\n\n"
        "If you see this, signal delivery is configured correctly.\n\n"
        f"Time: {ts}Z"
    )
    ok_sig = send_telegram_message(token, config["signals_chat_id"], signals_msg)
    print(f"  Signals channel: {'OK' if ok_sig else 'FAILED'}")

    # Test Ops channel
    ops_msg = f"🧪 *Ops Channel Test* — {ts}Z\nBot is connected and ready."
    ok_ops = send_telegram_message(token, config["ops_chat_id"], ops_msg)
    print(f"  Ops channel:     {'OK' if ok_ops else 'FAILED'}")

    # Test CEO DM
    if config["ceo_chat_id"]:
        ceo_msg = f"🧪 *CEO DM Test* — {ts}Z\nEdge Alert bot is connected."
        ok_ceo = send_telegram_message(token, config["ceo_chat_id"], ceo_msg)
        print(f"  CEO DM:          {'OK' if ok_ceo else 'FAILED'}")

    all_ok = ok_sig and ok_ops
    if all_ok:
        print("\n  All channels connected.")
    else:
        print("\n  Some channels FAILED. Check bot token and chat IDs in .env.")


def cmd_status(config):
    """Send a status check to Ops channel."""
    token = config["bot_token"]
    scans = load_todays_signals()
    total_sigs = sum(s.get("total_signals", 0) for s in scans)

    msg = (
        f"📊 *Edge Alert Status*\n\n"
        f"Today's scans: {len(scans)}\n"
        f"Total signals: {total_sigs}\n"
        f"Time: {datetime.now(timezone.utc).isoformat()[:19]}Z\n"
        f"Bot: online"
    )
    ok = send_telegram_message(token, config["ops_chat_id"], msg)
    print(f"  Status sent to Ops: {'OK' if ok else 'FAILED'}")


def main():
    parser = argparse.ArgumentParser(description="Edge Alert — Telegram Bot")
    parser.add_argument("--scan", action="store_true", help="One-shot scan + send")
    parser.add_argument("--digest", action="store_true", help="Send daily digest")
    parser.add_argument("--loop", action="store_true", help="Continuous scan + send")
    parser.add_argument("--test", action="store_true", help="Send test message")
    parser.add_argument("--status", action="store_true", help="Send status to Ops channel")

    args = parser.parse_args()

    config = load_config()
    if not config["bot_token"] or not config["signals_chat_id"]:
        print("ERROR: Set TELEGRAM_BOT_TOKEN and TELEGRAM_SIGNALS_CHAT_ID in .env")
        print("\nRequired in EdgeAlert/.env:")
        print("  TELEGRAM_BOT_TOKEN=your_bot_token_here")
        print("  TELEGRAM_SIGNALS_CHAT_ID=your_channel_id_here")
        print("  TELEGRAM_OPS_CHAT_ID=your_ops_channel_id_here")
        sys.exit(1)

    if args.scan:
        cmd_scan(config)
    elif args.digest:
        cmd_digest(config)
    elif args.loop:
        cmd_loop(config)
    elif args.test:
        cmd_test(config)
    elif args.status:
        cmd_status(config)
    else:
        # Default: one-shot scan
        cmd_scan(config)


if __name__ == "__main__":
    main()
