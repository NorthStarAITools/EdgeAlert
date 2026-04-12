#!/usr/bin/env python3
"""Edge Alert — Daily Digest Scheduler.

Runs at 8am ET daily. Pulls yesterday's signals, formats as HTML email,
sends to all active customers (Basic + Pro), and logs delivery.

Usage:
    python3 digest_scheduler.py            # Run once (send digest now)
    python3 digest_scheduler.py --dry-run  # Preview without sending

Scheduling:
    Use cron or Claude Code scheduled tasks:
    0 12 * * * cd /path/to/EdgeAlert && python3 digest_scheduler.py
    (12 UTC = 8am ET during EDT)
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import customer_db
import email_service
from formatter import format_daily_digest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("digest_scheduler")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SIGNALS_FILE = os.path.join(DATA_DIR, "signals.jsonl")


def load_yesterdays_signals():
    """Load all signals from the previous 24 hours."""
    if not os.path.exists(SIGNALS_FILE):
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    scans = []

    try:
        with open(SIGNALS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    scan = json.loads(line)
                    scan_time = scan.get("scan_time", "")
                    if scan_time >= cutoff.isoformat():
                        scans.append(scan)
                except (json.JSONDecodeError, KeyError):
                    continue
    except Exception as e:
        logger.error(f"Error reading signals file: {e}")

    return scans


def build_digest_html(scans):
    """Build HTML digest from scan data."""
    if not scans:
        return "<p>No signals detected in the last 24 hours.</p>"

    total_signals = sum(s.get("total_signals", 0) for s in scans)
    crypto_count = sum(len(s.get("crypto", [])) for s in scans)
    sports_count = sum(len(s.get("sports", [])) for s in scans)

    # Collect best signals
    best_crypto = []
    best_sports = []
    for scan in scans:
        for sig in scan.get("crypto", []):
            best_crypto.append(sig)
        for sig in scan.get("sports", []):
            best_sports.append(sig)

    # Sort by edge descending
    best_crypto.sort(key=lambda s: s.get("edge_pct", 0), reverse=True)
    best_sports.sort(key=lambda s: s.get("edge_pct", 0), reverse=True)

    html = f"""
<h3 style="margin:0 0 12px;">Yesterday's Summary</h3>
<table style="width:100%;border-collapse:collapse;margin-bottom:16px;">
<tr style="background:#f8f8ff;">
<td style="padding:8px;"><strong>{total_signals}</strong> total signals</td>
<td style="padding:8px;"><strong>{crypto_count}</strong> crypto</td>
<td style="padding:8px;"><strong>{sports_count}</strong> sports</td>
<td style="padding:8px;"><strong>{len(scans)}</strong> scans</td>
</tr>
</table>
"""

    if best_crypto[:5]:
        html += '<h4 style="margin:16px 0 8px;">Top Crypto Signals</h4>'
        html += '<table style="width:100%;border-collapse:collapse;font-size:14px;">'
        html += '<tr style="background:#eee;"><th style="padding:6px;text-align:left;">Ticker</th><th style="padding:6px;">Side</th><th style="padding:6px;">Edge</th><th style="padding:6px;">Confidence</th></tr>'
        for sig in best_crypto[:5]:
            html += f'<tr><td style="padding:6px;">{sig.get("ticker", "")}</td>'
            html += f'<td style="padding:6px;text-align:center;">{sig.get("side", "").upper()}</td>'
            html += f'<td style="padding:6px;text-align:center;">{sig.get("edge_pct", 0):+.1f}%</td>'
            html += f'<td style="padding:6px;text-align:center;">{sig.get("confidence", "")}</td></tr>'
        html += '</table>'

    if best_sports[:5]:
        html += '<h4 style="margin:16px 0 8px;">Top Sports Signals</h4>'
        html += '<table style="width:100%;border-collapse:collapse;font-size:14px;">'
        html += '<tr style="background:#eee;"><th style="padding:6px;text-align:left;">Ticker</th><th style="padding:6px;">Side</th><th style="padding:6px;">Edge</th><th style="padding:6px;">Sport</th></tr>'
        for sig in best_sports[:5]:
            html += f'<tr><td style="padding:6px;">{sig.get("ticker", "")}</td>'
            html += f'<td style="padding:6px;text-align:center;">{sig.get("side", "").upper()}</td>'
            html += f'<td style="padding:6px;text-align:center;">{sig.get("edge_pct", 0):+.1f}%</td>'
            html += f'<td style="padding:6px;text-align:center;">{sig.get("sport", "")}</td></tr>'
        html += '</table>'

    return html


def send_digest(dry_run=False):
    """Send daily digest to all active customers."""
    scans = load_yesterdays_signals()
    logger.info(f"Loaded {len(scans)} scans from last 24h")

    if not scans:
        logger.info("No signals to digest. Skipping.")
        return 0

    digest_html = build_digest_html(scans)

    if dry_run:
        logger.info("[DRY RUN] Digest HTML preview:")
        print(digest_html)
        return 0

    # Get all active customers
    customers = customer_db.get_active_customers()
    if not customers:
        logger.info("No active customers. Skipping digest.")
        return 0

    sent = 0
    for customer in customers:
        email = customer.get("email")
        if not email:
            continue

        tier = customer.get("tier", "basic")

        # For basic tier, only include crypto signals in digest
        if tier == "basic":
            basic_scans = []
            for scan in scans:
                basic_scan = dict(scan)
                basic_scan["sports"] = []
                basic_scan["total_signals"] = len(scan.get("crypto", []))
                basic_scans.append(basic_scan)
            customer_digest = build_digest_html(basic_scans)
        else:
            customer_digest = digest_html

        ok = email_service.send_daily_digest_email(email, customer_digest)
        if ok:
            sent += 1
            # Log delivery
            try:
                customer_db.log_delivery(
                    customer["id"],
                    f"digest_{datetime.now(timezone.utc).strftime('%Y%m%d')}",
                    "email",
                )
            except Exception:
                pass

    logger.info(f"Digest sent to {sent}/{len(customers)} customers")
    return sent


def main():
    parser = argparse.ArgumentParser(description="Edge Alert — Daily Digest Scheduler")
    parser.add_argument("--dry-run", action="store_true", help="Preview without sending")
    args = parser.parse_args()

    send_digest(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
