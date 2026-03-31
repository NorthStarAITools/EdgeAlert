#!/usr/bin/env python3
"""Edge Alert — Accuracy Tracker.

Tracks whether Edge Alert signals were correct after market settlement.
Pulls settlement results from Kalshi API, compares to our predicted side,
and builds a running accuracy record.

This is the credibility layer for Edge Alert — publicly visible stats
showing we called X% of markets correctly.

Data flow:
  signals.jsonl  →  accuracy_tracker.py  →  outcomes.jsonl + accuracy_report.json

Usage:
    python3 accuracy_tracker.py --settle     # Check pending signals against Kalshi settlements
    python3 accuracy_tracker.py --report     # Print accuracy report to console
    python3 accuracy_tracker.py --export     # Export accuracy_report.json (for landing page)
    python3 accuracy_tracker.py --full       # Settle + export in one pass

Run this on a cron/scheduled task after each scan cycle.
Ideally: every 30 minutes while bots are running, then once at end of day.
"""

import argparse
import json
import os
import requests
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Optional

# ── Config ───────────────────────────────────────────────────────────────────

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SIGNALS_FILE = os.path.join(DATA_DIR, "signals.jsonl")
OUTCOMES_FILE = os.path.join(DATA_DIR, "outcomes.jsonl")
REPORT_FILE = os.path.join(DATA_DIR, "accuracy_report.json")

os.makedirs(DATA_DIR, exist_ok=True)

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

# How long to wait before attempting settlement lookup (markets need time to close)
MIN_AGE_FOR_SETTLEMENT_MINUTES = 20

# Kalshi market status that indicates final resolution
FINALIZED_STATUS = "finalized"


# ── Data Model ────────────────────────────────────────────────────────────────

def make_signal_id(signal: dict) -> str:
    """Deterministic ID for a signal so we can avoid duplicates."""
    ticker = signal.get("ticker", "")
    side = signal.get("side", "")
    timestamp = signal.get("timestamp", "")[:16]  # Minute precision
    return f"{ticker}_{side}_{timestamp}"


def load_outcomes() -> dict:
    """Load all tracked outcomes. Returns dict keyed by signal_id."""
    outcomes = {}
    if not os.path.exists(OUTCOMES_FILE):
        return outcomes
    with open(OUTCOMES_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                outcomes[rec["signal_id"]] = rec
            except Exception:
                continue
    return outcomes


def append_outcome(record: dict):
    """Append a single outcome record to the JSONL file."""
    with open(OUTCOMES_FILE, "a") as f:
        f.write(json.dumps(record) + "\n")


def update_outcome(signal_id: str, outcome: str, settled_at: str, settlement_yes_price: Optional[int]):
    """Overwrite an outcome record with settlement data.
    Because JSONL is append-only, we write a full replacement record.
    The loader uses the last-written record for each signal_id (via dict assignment).
    """
    outcomes = load_outcomes()
    if signal_id not in outcomes:
        return
    rec = outcomes[signal_id]
    rec["outcome"] = outcome          # "WIN", "LOSS", or "PUSH"
    rec["settled_at"] = settled_at
    rec["settlement_yes_price"] = settlement_yes_price
    rec["updated_at"] = datetime.now(timezone.utc).isoformat()
    append_outcome(rec)  # Append updated version — loader picks up last write per ID


# ── Signal Ingestion ─────────────────────────────────────────────────────────

def ingest_signals():
    """Read signals.jsonl and register any new signals into outcomes.jsonl.

    This does NOT fetch settlements — it just makes sure every known signal
    has a PENDING record so it can be settled later.
    """
    if not os.path.exists(SIGNALS_FILE):
        print("  No signals file found. Run edge_alert.py --save first.")
        return 0

    existing = load_outcomes()
    new_count = 0

    with open(SIGNALS_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                scan = json.loads(line)
            except Exception:
                continue

            scan_time = scan.get("scan_time", "")

            for mtype in ["crypto", "sports"]:
                for sig in scan.get(mtype, []):
                    sig_id = make_signal_id(sig)
                    if sig_id in existing:
                        continue  # Already registered

                    record = {
                        "signal_id": sig_id,
                        "ticker": sig.get("ticker", ""),
                        "market_type": mtype,
                        "side": sig.get("side", ""),
                        "our_price_cents": sig.get("our_price_cents", 0),
                        "edge_pct": sig.get("edge_pct", 0),
                        "ev_per_contract": sig.get("ev_per_contract", 0),
                        "actual_win_rate": sig.get("actual_win_rate", 0),
                        "confidence": sig.get("confidence", ""),
                        "title": sig.get("title", ""),
                        "sport": sig.get("sport", None),
                        "game_pct": sig.get("game_pct", None),
                        "minutes_left": sig.get("minutes_left", None),
                        "signal_timestamp": sig.get("timestamp", scan_time),
                        "scan_time": scan_time,
                        "outcome": "PENDING",
                        "settled_at": None,
                        "settlement_yes_price": None,
                        "registered_at": datetime.now(timezone.utc).isoformat(),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    append_outcome(record)
                    existing[sig_id] = record
                    new_count += 1

    return new_count


# ── Settlement Resolution ─────────────────────────────────────────────────────

def fetch_market_settlement(ticker: str) -> tuple[Optional[str], Optional[int]]:
    """Fetch a market from Kalshi and return (result, yes_price_at_close).

    result is "yes" or "no" if settled, None if still open.
    yes_price_at_close is the final YES price (cents), or None.
    """
    try:
        r = requests.get(
            f"{KALSHI_API}/markets/{ticker}",
            timeout=8
        )
        if r.status_code == 404:
            return None, None  # Market doesn't exist or expired from API
        if r.status_code != 200:
            return None, None

        data = r.json().get("market", {})
        status = data.get("status", "")

        if status != FINALIZED_STATUS:
            return None, None  # Still open

        result = data.get("result", "")  # "yes" or "no"
        if result not in ("yes", "no"):
            return None, None

        # Final prices: finalized markets typically show the settled price
        yes_price = data.get("yes_bid", None) or data.get("last_price", None)

        return result, yes_price

    except Exception as e:
        print(f"  [!] Error fetching {ticker}: {e}")
        return None, None


def settle_pending(verbose=True) -> tuple[int, int]:
    """Check all PENDING outcomes against Kalshi API and settle them.

    Returns (settled_count, still_pending_count).
    """
    outcomes = load_outcomes()
    pending = [r for r in outcomes.values() if r["outcome"] == "PENDING"]

    if not pending:
        if verbose:
            print("  No pending signals to settle.")
        return 0, 0

    if verbose:
        print(f"  Checking {len(pending)} pending signal(s)...")

    settled = 0
    still_pending = 0
    now = datetime.now(timezone.utc)

    for rec in pending:
        # Don't try to settle very recent signals — market may still be open
        signal_ts = rec.get("signal_timestamp", "")
        try:
            sig_dt = datetime.fromisoformat(signal_ts.replace("Z", "+00:00"))
            age_minutes = (now - sig_dt).total_seconds() / 60
            if age_minutes < MIN_AGE_FOR_SETTLEMENT_MINUTES:
                still_pending += 1
                continue
        except Exception:
            still_pending += 1
            continue

        ticker = rec["ticker"]
        our_side = rec["side"]

        result, yes_price = fetch_market_settlement(ticker)

        if result is None:
            still_pending += 1
            continue

        # Determine WIN or LOSS
        if result == our_side:
            outcome = "WIN"
        else:
            outcome = "LOSS"

        settled_at = datetime.now(timezone.utc).isoformat()
        update_outcome(rec["signal_id"], outcome, settled_at, yes_price)

        if verbose:
            emoji = "✓" if outcome == "WIN" else "✗"
            print(f"  {emoji} {ticker} | Predicted: {our_side.upper()} | Result: {result.upper()} | {outcome}")

        settled += 1

    if verbose:
        print(f"\n  Settled: {settled} | Still pending: {still_pending}")

    return settled, still_pending


# ── Accuracy Report ───────────────────────────────────────────────────────────

def build_accuracy_report() -> dict:
    """Build a structured accuracy report from all settled outcomes."""
    outcomes = load_outcomes()
    settled = [r for r in outcomes.values() if r["outcome"] in ("WIN", "LOSS")]

    if not settled:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_signals": len(outcomes),
            "total_settled": 0,
            "total_pending": len([r for r in outcomes.values() if r["outcome"] == "PENDING"]),
            "overall_accuracy": None,
            "by_market_type": {},
            "by_confidence": {},
            "by_price_bucket": {},
            "recent_30d": {},
            "note": "No settled signals yet. Accuracy updates as markets close.",
        }

    wins = [r for r in settled if r["outcome"] == "WIN"]
    overall_accuracy = len(wins) / len(settled) if settled else 0

    # By market type
    by_type = {}
    for mtype in ["crypto", "sports"]:
        type_settled = [r for r in settled if r["market_type"] == mtype]
        type_wins = [r for r in type_settled if r["outcome"] == "WIN"]
        if type_settled:
            by_type[mtype] = {
                "signals": len(type_settled),
                "wins": len(type_wins),
                "accuracy": round(len(type_wins) / len(type_settled), 4),
            }

    # By confidence tier
    by_confidence = {}
    for tier in ["HIGH", "MEDIUM", "SPECULATIVE"]:
        tier_settled = [r for r in settled if r["confidence"] == tier]
        tier_wins = [r for r in tier_settled if r["outcome"] == "WIN"]
        if tier_settled:
            by_confidence[tier] = {
                "signals": len(tier_settled),
                "wins": len(tier_wins),
                "accuracy": round(len(tier_wins) / len(tier_settled), 4),
            }

    # By price bucket (10-cent buckets)
    bucket_data = defaultdict(lambda: {"signals": 0, "wins": 0})
    for rec in settled:
        price = rec.get("our_price_cents", 0)
        bucket = f"{(price // 10) * 10}-{(price // 10) * 10 + 9}c"
        bucket_data[bucket]["signals"] += 1
        if rec["outcome"] == "WIN":
            bucket_data[bucket]["wins"] += 1

    by_bucket = {}
    for bucket, data in sorted(bucket_data.items()):
        by_bucket[bucket] = {
            "signals": data["signals"],
            "wins": data["wins"],
            "accuracy": round(data["wins"] / data["signals"], 4) if data["signals"] else 0,
        }

    # Last 30 days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    recent = [r for r in settled if (r.get("signal_timestamp") or "") >= cutoff]
    recent_wins = [r for r in recent if r["outcome"] == "WIN"]
    recent_30d = {
        "signals": len(recent),
        "wins": len(recent_wins),
        "accuracy": round(len(recent_wins) / len(recent), 4) if recent else None,
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_signals": len(outcomes),
        "total_settled": len(settled),
        "total_pending": len([r for r in outcomes.values() if r["outcome"] == "PENDING"]),
        "total_wins": len(wins),
        "total_losses": len(settled) - len(wins),
        "overall_accuracy": round(overall_accuracy, 4),
        "overall_accuracy_pct": round(overall_accuracy * 100, 1),
        "by_market_type": by_type,
        "by_confidence": by_confidence,
        "by_price_bucket": by_bucket,
        "recent_30d": recent_30d,
        "calibration_note": (
            "Calibrated on 80,000+ historical Kalshi markets. "
            "Accuracy figures reflect live signal performance, not backtested results."
        ),
    }


def print_report(report: dict):
    """Print a human-readable accuracy report to console."""
    print("\n" + "="*60)
    print("  EDGE ALERT — ACCURACY REPORT")
    print("="*60)
    print(f"  Generated: {report['generated_at'][:19]}Z")
    print(f"  Total signals tracked: {report['total_signals']}")
    print(f"  Settled: {report['total_settled']} | Pending: {report['total_pending']}")

    if report["overall_accuracy"] is None:
        print("\n  No settled signals yet.\n")
        return

    print(f"\n  OVERALL ACCURACY: {report['overall_accuracy_pct']:.1f}%")
    print(f"  ({report['total_wins']}W / {report['total_losses']}L)\n")

    # By type
    if report["by_market_type"]:
        print("  By Market Type:")
        for mtype, data in report["by_market_type"].items():
            print(f"    {mtype.upper():8s} — {data['accuracy']*100:.1f}%  ({data['wins']}W/{data['signals']-data['wins']}L)")

    # By confidence
    if report["by_confidence"]:
        print("\n  By Confidence Tier:")
        for tier, data in report["by_confidence"].items():
            print(f"    {tier:12s} — {data['accuracy']*100:.1f}%  ({data['wins']}W/{data['signals']-data['wins']}L)")

    # Recent 30d
    r30 = report.get("recent_30d", {})
    if r30.get("signals"):
        acc = r30["accuracy"]
        print(f"\n  Last 30 Days: {acc*100:.1f}%  ({r30['wins']}W / {r30['signals']-r30['wins']}L)")

    print("\n" + "="*60 + "\n")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Edge Alert — Accuracy Tracker")
    parser.add_argument("--settle", action="store_true", help="Check pending signals and settle resolved markets")
    parser.add_argument("--report", action="store_true", help="Print accuracy report to console")
    parser.add_argument("--export", action="store_true", help="Export accuracy_report.json")
    parser.add_argument("--ingest", action="store_true", help="Register new signals from signals.jsonl")
    parser.add_argument("--full", action="store_true", help="Ingest + settle + export in one pass")
    parser.add_argument("--quiet", action="store_true", help="Suppress verbose output")

    args = parser.parse_args()
    verbose = not args.quiet

    if args.full or args.ingest:
        new = ingest_signals()
        if verbose:
            print(f"  Ingested {new} new signal(s) from signals.jsonl")

    if args.full or args.settle:
        settle_pending(verbose=verbose)

    if args.full or args.report:
        report = build_accuracy_report()
        print_report(report)

    if args.full or args.export:
        report = build_accuracy_report()
        with open(REPORT_FILE, "w") as f:
            json.dump(report, f, indent=2)
        if verbose:
            print(f"  Report exported to {REPORT_FILE}")


if __name__ == "__main__":
    main()
