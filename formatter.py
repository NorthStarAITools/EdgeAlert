#!/usr/bin/env python3
"""Edge Alert — Output Formatter.

Formats signals into multiple output formats:
  - Console (colored terminal output)
  - Telegram markdown
  - JSON (for API/webhook delivery)
  - Daily digest (summary report)
"""

from datetime import datetime, timezone
from dataclasses import asdict


# ── Console Formatter ────────────────────────────────────────────────────────

def format_console(signals_by_type):
    """Format signals for terminal output with color coding."""
    lines = []
    scan_time = signals_by_type.get("scan_time", "")
    total = signals_by_type.get("total_signals", 0)

    lines.append("")
    lines.append("=" * 72)
    lines.append(f"  EDGE ALERT SCAN — {scan_time[:19]}Z")
    lines.append(f"  {total} signal{'s' if total != 1 else ''} found")
    lines.append("=" * 72)

    for market_type in ["crypto", "sports"]:
        sigs = signals_by_type.get(market_type, [])
        if not sigs:
            continue

        label = "CRYPTO 15-MIN" if market_type == "crypto" else "SPORTS IN-GAME"
        lines.append(f"\n  ┌─ {label} ({len(sigs)} signal{'s' if len(sigs) != 1 else ''}) ─┐")

        for s in sigs:
            conf_icon = {"HIGH": "🟢", "MEDIUM": "🟡", "SPECULATIVE": "🔴"}.get(s.confidence, "⚪")

            lines.append(f"  │")
            lines.append(f"  │  {conf_icon} {s.confidence} — {s.side.upper()} {s.ticker}")
            lines.append(f"  │  Title:  {s.title}")
            lines.append(f"  │  Price:  {s.our_price_cents}c  |  Edge: {s.edge_pct:+.1f}%  |  EV: {s.ev_per_contract:.1f}c/contract")
            lines.append(f"  │  WR:     {s.actual_win_rate*100:.1f}% actual vs {s.implied_prob*100:.1f}% implied  |  Fee: {s.fee_cents:.1f}c")
            lines.append(f"  │  Spread: {s.spread}c ({s.yes_bid}/{s.yes_ask})  |  Vol: {s.volume:,}")

            if market_type == "crypto" and s.minutes_left is not None:
                lines.append(f"  │  Window: {s.minutes_left} min left")
            elif market_type == "sports" and s.game_pct is not None:
                lines.append(f"  │  Game:   {s.game_pct*100:.0f}% complete ({s.sport or 'unknown'})")

        lines.append(f"  └{'─' * 40}┘")

    if total == 0:
        lines.append("\n  No +EV signals found this scan.")
        lines.append("  Markets may be closed or no edge detected above threshold.")

    lines.append("")
    return "\n".join(lines)


# ── Telegram Formatter ───────────────────────────────────────────────────────

def format_telegram(signals_by_type):
    """Format signals as Telegram-friendly markdown."""
    lines = []
    scan_time = signals_by_type.get("scan_time", "")[:16]
    total = signals_by_type.get("total_signals", 0)

    lines.append(f"⚡ *EDGE ALERT* — {total} signal{'s' if total != 1 else ''}")
    lines.append(f"_{scan_time}Z_\n")

    for market_type in ["crypto", "sports"]:
        sigs = signals_by_type.get(market_type, [])
        if not sigs:
            continue

        label = "🪙 CRYPTO" if market_type == "crypto" else "🏀 SPORTS"
        lines.append(f"*{label}*\n")

        for s in sigs:
            conf = {"HIGH": "🟢", "MEDIUM": "🟡", "SPECULATIVE": "🔴"}.get(s.confidence, "⚪")
            lines.append(f"{conf} *{s.side.upper()} {s.ticker}* @ {s.our_price_cents}c")
            lines.append(f"Edge: `{s.edge_pct:+.1f}%` | EV: `{s.ev_per_contract:.1f}c`")
            lines.append(f"WR: {s.actual_win_rate*100:.1f}% vs {s.implied_prob*100:.1f}% implied")

            if market_type == "crypto":
                lines.append(f"⏱ {s.minutes_left or '?'} min left")
            elif s.game_pct:
                lines.append(f"🎯 {s.game_pct*100:.0f}% complete")
            lines.append("")

    if total == 0:
        lines.append("_No signals this scan. Markets closed or no edge._")

    lines.append("---")
    lines.append("_Edge Alert by North Star AI Tools_")
    lines.append("_Not financial advice. Entertainment only._")

    return "\n".join(lines)


# ── JSON Formatter ───────────────────────────────────────────────────────────

def format_json(signals_by_type):
    """Format signals as JSON for API/webhook delivery."""
    output = {
        "scan_time": signals_by_type.get("scan_time"),
        "total_signals": signals_by_type.get("total_signals", 0),
        "crypto": [asdict(s) for s in signals_by_type.get("crypto", [])],
        "sports": [asdict(s) for s in signals_by_type.get("sports", [])],
    }
    return output


# ── Daily Digest ─────────────────────────────────────────────────────────────

def format_daily_digest(scan_history):
    """Format a daily digest from multiple scans.

    Args:
        scan_history: list of signals_by_type dicts from multiple scans
    """
    lines = []
    today = datetime.now().strftime("%Y-%m-%d")
    total_scans = len(scan_history)
    total_signals = sum(s.get("total_signals", 0) for s in scan_history)

    all_crypto = []
    all_sports = []
    for scan in scan_history:
        all_crypto.extend(scan.get("crypto", []))
        all_sports.extend(scan.get("sports", []))

    lines.append(f"# Edge Alert — Daily Digest ({today})")
    lines.append(f"**{total_scans} scans** | **{total_signals} total signals**")
    lines.append(f"Crypto: {len(all_crypto)} | Sports: {len(all_sports)}")
    lines.append("")

    # Top signals by edge
    all_signals = all_crypto + all_sports
    if all_signals:
        all_signals.sort(key=lambda s: s.edge_pct, reverse=True)
        lines.append("## Top Signals (by edge)")
        for s in all_signals[:10]:
            lines.append(f"- **{s.side.upper()} {s.ticker}** @ {s.our_price_cents}c — "
                        f"Edge: {s.edge_pct:+.1f}%, EV: {s.ev_per_contract:.1f}c, "
                        f"WR: {s.actual_win_rate*100:.1f}% [{s.confidence}]")
        lines.append("")

    # Stats
    if all_signals:
        high = sum(1 for s in all_signals if s.confidence == "HIGH")
        med = sum(1 for s in all_signals if s.confidence == "MEDIUM")
        spec = sum(1 for s in all_signals if s.confidence == "SPECULATIVE")
        avg_edge = sum(s.edge_pct for s in all_signals) / len(all_signals)

        lines.append("## Summary Stats")
        lines.append(f"- Avg edge: {avg_edge:.1f}%")
        lines.append(f"- HIGH: {high} | MEDIUM: {med} | SPECULATIVE: {spec}")
        lines.append("")

    lines.append("---")
    lines.append("*Edge Alert by North Star AI Tools — Not financial advice.*")

    return "\n".join(lines)
