#!/usr/bin/env python3
"""Edge Alert — CLI Runner.

Usage:
    python3 edge_alert.py                   # One-shot scan, console output
    python3 edge_alert.py --loop            # Continuous scanning (every 30s)
    python3 edge_alert.py --telegram        # Output as Telegram markdown
    python3 edge_alert.py --json            # Output as JSON
    python3 edge_alert.py --digest          # Run for a period, then output daily digest
    python3 edge_alert.py --sports-only     # Sports signals only
    python3 edge_alert.py --crypto-only     # Crypto signals only
    python3 edge_alert.py --min-edge 2.0    # Custom minimum edge threshold
    python3 edge_alert.py --save            # Save signals to JSONL file
    python3 edge_alert.py --history         # Show today's signal history

Designed to be run as a scheduled task or continuous background process.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

from edge_engine import scan_all, scan_crypto, scan_sports
from formatter import format_console, format_telegram, format_json, format_daily_digest

# ── Config ───────────────────────────────────────────────────────────────────

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SIGNALS_FILE = os.path.join(DATA_DIR, "signals.jsonl")
DIGEST_FILE = os.path.join(DATA_DIR, "daily_digest.md")
os.makedirs(DATA_DIR, exist_ok=True)

DEFAULT_SCAN_INTERVAL = 30   # seconds between scans in loop mode
DEFAULT_MIN_EDGE = 1.0       # minimum edge % to report


# ── Signal Persistence ───────────────────────────────────────────────────────

def save_signals(signals_by_type):
    """Append signals to JSONL file for history tracking."""
    json_data = format_json(signals_by_type)
    with open(SIGNALS_FILE, "a") as f:
        f.write(json.dumps(json_data) + "\n")


def load_todays_signals():
    """Load today's signals from JSONL file."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    scans = []
    if not os.path.exists(SIGNALS_FILE):
        return scans
    with open(SIGNALS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                scan_time = data.get("scan_time", "")
                if scan_time.startswith(today):
                    scans.append(data)
            except Exception:
                continue
    return scans


# ── Main ─────────────────────────────────────────────────────────────────────

def run_scan(args):
    """Execute a single scan and output results."""
    min_edge = args.min_edge

    if args.sports_only:
        results = {
            "crypto": [],
            "sports": scan_sports(min_edge_pct=min_edge),
            "scan_time": datetime.now(timezone.utc).isoformat(),
        }
        results["total_signals"] = len(results["sports"])
    elif args.crypto_only:
        results = {
            "crypto": scan_crypto(min_edge_pct=min_edge),
            "sports": [],
            "scan_time": datetime.now(timezone.utc).isoformat(),
        }
        results["total_signals"] = len(results["crypto"])
    else:
        results = scan_all(min_edge_pct=min_edge)

    # Save if requested
    if args.save:
        save_signals(results)

    # Format output
    if args.telegram:
        print(format_telegram(results))
    elif args.json:
        print(json.dumps(format_json(results), indent=2))
    else:
        print(format_console(results))

    return results


def run_loop(args):
    """Continuous scanning mode."""
    interval = args.interval or DEFAULT_SCAN_INTERVAL
    print(f"  Edge Alert — Continuous mode (scan every {interval}s)")
    print(f"  Min edge: {args.min_edge}%")
    print(f"  Saving: {'YES' if args.save else 'NO'}")
    print(f"  Press Ctrl+C to stop.\n")

    scan_count = 0
    total_signals = 0

    try:
        while True:
            scan_count += 1
            results = run_scan(args)
            n = results.get("total_signals", 0)
            total_signals += n

            if n > 0:
                print(f"  [{scan_count}] {n} signal(s) found — {total_signals} total")
            else:
                # Quiet mode for empty scans
                ts = datetime.now().strftime("%H:%M:%S")
                print(f"  [{scan_count}] {ts} — no signals", end="\r")

            time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n\n  Stopped after {scan_count} scans, {total_signals} total signals.")
        if args.save:
            print(f"  Signals saved to {SIGNALS_FILE}")


def run_digest(args):
    """Generate daily digest from today's saved signals."""
    scans = load_todays_signals()
    if not scans:
        print("No signals found for today. Run scans with --save first.")
        return

    # Reconstruct Signal objects for the formatter (simplified — use raw dicts)
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
                except:
                    pass
        scan_history.append(reconstructed)

    digest = format_daily_digest(scan_history)
    print(digest)

    # Save digest
    with open(DIGEST_FILE, "w") as f:
        f.write(digest)
    print(f"\n  Digest saved to {DIGEST_FILE}")


def show_history(args):
    """Show today's signal history."""
    scans = load_todays_signals()
    if not scans:
        print("No signals found for today.")
        return

    total = sum(s.get("total_signals", 0) for s in scans)
    print(f"\n  Today's History: {len(scans)} scans, {total} total signals\n")

    for i, scan in enumerate(scans):
        ts = scan.get("scan_time", "")[:19]
        n = scan.get("total_signals", 0)
        crypto_n = len(scan.get("crypto", []))
        sports_n = len(scan.get("sports", []))
        print(f"  Scan {i+1}: {ts}Z — {n} signals (crypto: {crypto_n}, sports: {sports_n})")

        # Show top signal per scan
        all_sigs = scan.get("crypto", []) + scan.get("sports", [])
        if all_sigs:
            best = max(all_sigs, key=lambda s: s.get("edge_pct", 0))
            print(f"    Best: {best.get('side','?').upper()} {best.get('ticker','?')} "
                  f"@ {best.get('our_price_cents',0)}c — edge {best.get('edge_pct',0):+.1f}%")


def main():
    parser = argparse.ArgumentParser(description="Edge Alert — Prediction Market Signal Feed")
    parser.add_argument("--loop", action="store_true", help="Continuous scanning mode")
    parser.add_argument("--telegram", action="store_true", help="Telegram markdown output")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--digest", action="store_true", help="Generate daily digest")
    parser.add_argument("--history", action="store_true", help="Show today's signal history")
    parser.add_argument("--sports-only", action="store_true", help="Sports signals only")
    parser.add_argument("--crypto-only", action="store_true", help="Crypto signals only")
    parser.add_argument("--min-edge", type=float, default=DEFAULT_MIN_EDGE, help="Min edge %% threshold")
    parser.add_argument("--save", action="store_true", help="Save signals to JSONL")
    parser.add_argument("--interval", type=int, default=None, help="Scan interval in seconds (loop mode)")

    args = parser.parse_args()

    if args.digest:
        run_digest(args)
    elif args.history:
        show_history(args)
    elif args.loop:
        args.save = True  # Always save in loop mode
        run_loop(args)
    else:
        run_scan(args)


if __name__ == "__main__":
    main()
