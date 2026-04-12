#!/usr/bin/env python3
"""Edge Alert — Polymarket Scanner Module.

Scans Polymarket prediction markets for +EV opportunities using Z-score
analysis on probability movements. No trades placed — analysis only.

APIs used (all public, no auth required):
  - Gamma API: https://gamma-api.polymarket.com — market discovery
  - CLOB API: https://clob.polymarket.com — orderbook pricing, price history

Data flow:
  Gamma API (active markets) → CLOB API (price history) → Z-score engine
  → Signal objects → existing Edge Alert pipeline (Telegram, accuracy tracker)

Rate limit: ~1,000 calls/hr on Gamma/CLOB (generous for scanning).
"""

import json
import os
import time
import requests
import math
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

# Import Signal dataclass from edge_engine for compatibility
from edge_engine import Signal, kalshi_fee_cents

# ── API Config ──────────────────────────────────────────────────────────────

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Request timeout (seconds)
API_TIMEOUT = 10

# Rate limiting: minimum seconds between API calls
MIN_DELAY = 0.1

# ── Z-Score Parameters ──────────────────────────────────────────────────────

# Minimum number of price history points needed to calculate Z-score
MIN_HISTORY_POINTS = 20

# Z-score thresholds for signal generation
Z_SCORE_HIGH = 2.5       # HIGH confidence: probability deviated 2.5+ std devs
Z_SCORE_MEDIUM = 2.0     # MEDIUM confidence
Z_SCORE_SPECULATIVE = 1.5  # SPECULATIVE: still notable deviation

# Minimum volume (in USD) for a market to be worth scanning
MIN_VOLUME_USD = 10000

# Maximum number of markets to scan per cycle (API budget)
MAX_MARKETS_PER_SCAN = 50

# Price range to consider (avoid extreme/dead markets)
MIN_PROB = 0.05   # Skip markets below 5% probability
MAX_PROB = 0.95   # Skip markets above 95% probability

# Minimum edge % to report (net of estimated spread cost)
DEFAULT_MIN_EDGE = 1.0

# Polymarket fee: 2% on winnings (not on entry like Kalshi)
POLYMARKET_FEE_RATE = 0.02


# ── Polymarket Fee Calculation ──────────────────────────────────────────────

def polymarket_fee_cents(price_cents):
    """Polymarket fee: ~2% on winnings. Returns estimated fee in cents.

    Polymarket charges fees on net winnings, not on entry.
    For a YES contract at price P: if you win, you pay 2% of (100 - P).
    Expected fee = win_rate * 0.02 * (100 - P).
    We approximate win_rate ≈ P/100 for fee estimation.
    """
    p = price_cents / 100
    if p <= 0 or p >= 1:
        return 0
    # Expected fee = P * 0.02 * (1 - P) * 100 cents
    return p * POLYMARKET_FEE_RATE * (1 - p) * 100


# ── API Helpers ─────────────────────────────────────────────────────────────

def gamma_get(endpoint, params=None):
    """GET request to Polymarket Gamma API."""
    url = f"{GAMMA_API}{endpoint}"
    try:
        r = requests.get(url, params=params, timeout=API_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        print(f"  [!] Gamma API error: {e}")
        return None


def clob_get(endpoint, params=None):
    """GET request to Polymarket CLOB API."""
    url = f"{CLOB_API}{endpoint}"
    try:
        r = requests.get(url, params=params, timeout=API_TIMEOUT)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        print(f"  [!] CLOB API error: {e}")
        return None


# ── Market Discovery ────────────────────────────────────────────────────────

def fetch_active_markets(limit=MAX_MARKETS_PER_SCAN):
    """Fetch active, liquid Polymarket markets from Gamma API.

    Returns list of market dicts with: condition_id, question, tokens,
    volume, outcome_prices, etc.
    """
    # Gamma API: /markets endpoint with filters
    params = {
        "active": "true",
        "closed": "false",
        "limit": limit,
        "order": "volume",        # Sort by volume descending
        "ascending": "false",
    }
    data = gamma_get("/markets", params=params)
    if not data:
        return []

    markets = []
    for m in data:
        try:
            # Parse volume
            volume_str = m.get("volume", "0")
            volume = float(volume_str) if volume_str else 0

            if volume < MIN_VOLUME_USD:
                continue

            # Get condition_id (needed for CLOB API)
            condition_id = m.get("conditionId") or m.get("condition_id", "")
            if not condition_id:
                continue

            # Parse current prices from outcome_prices or tokens
            outcome_prices = m.get("outcomePrices", "") or m.get("outcome_prices", "")
            if isinstance(outcome_prices, str) and outcome_prices:
                try:
                    prices = json.loads(outcome_prices)
                except:
                    prices = []
            elif isinstance(outcome_prices, list):
                prices = outcome_prices
            else:
                prices = []

            # Get CLOB token IDs for price history lookup
            tokens = m.get("clobTokenIds", "") or m.get("clob_token_ids", "")
            if isinstance(tokens, str) and tokens:
                try:
                    token_ids = json.loads(tokens)
                except:
                    token_ids = []
            elif isinstance(tokens, list):
                token_ids = tokens
            else:
                token_ids = []

            if not token_ids:
                continue

            # Current YES probability
            yes_prob = float(prices[0]) if prices else 0
            if yes_prob < MIN_PROB or yes_prob > MAX_PROB:
                continue

            markets.append({
                "condition_id": condition_id,
                "question": m.get("question", ""),
                "slug": m.get("slug", ""),
                "volume": volume,
                "yes_prob": yes_prob,
                "no_prob": 1 - yes_prob,
                "token_ids": token_ids,
                "end_date": m.get("endDate") or m.get("end_date", ""),
                "category": m.get("groupSlug") or m.get("group_slug", ""),
                "market_id": m.get("id", ""),
            })
        except Exception as e:
            continue

    return markets[:limit]


# ── Price History & Z-Score ─────────────────────────────────────────────────

def fetch_price_history(token_id, interval="1h", fidelity=60):
    """Fetch price history for a CLOB token.

    Args:
        token_id: CLOB token ID (from Gamma API market data)
        interval: Time interval — we use hourly data for Z-score
        fidelity: Number of data points to request

    Returns list of (timestamp, price) tuples, or empty list on failure.
    """
    params = {
        "market": token_id,
        "interval": interval,
        "fidelity": fidelity,
    }
    data = clob_get("/prices-history", params=params)
    if not data or not isinstance(data, dict):
        return []

    history = data.get("history", [])
    if not history:
        return []

    points = []
    for point in history:
        try:
            t = point.get("t", 0)
            p = float(point.get("p", 0))
            if p > 0:
                points.append((t, p))
        except:
            continue

    return points


def calculate_z_score(prices, current_price):
    """Calculate Z-score of current price relative to recent history.

    Z-score = (current - mean) / std_dev

    A high positive Z-score means the market has moved UP sharply.
    A high negative Z-score means the market has moved DOWN sharply.

    We use this to detect when a market's probability has deviated
    significantly from its recent behavior — potential overreaction
    or momentum signal.
    """
    if len(prices) < MIN_HISTORY_POINTS:
        return None, None, None

    mean = sum(prices) / len(prices)
    variance = sum((p - mean) ** 2 for p in prices) / len(prices)
    std_dev = math.sqrt(variance)

    if std_dev < 0.001:  # Market hasn't moved — no signal
        return None, None, None

    z = (current_price - mean) / std_dev
    return z, mean, std_dev


def calculate_polymarket_edge(current_prob, historical_mean, z_score):
    """Calculate expected edge based on Z-score reversion.

    The thesis: when a market's probability deviates significantly from
    its recent mean (high |Z-score|), there's a statistical tendency for
    reversion. The "edge" is the difference between where the market IS
    and where our model says it SHOULD be (the historical mean, adjusted
    for the magnitude of deviation).

    For a positive Z-score (market moved UP):
      - If we think it's overpriced → sell YES (buy NO)
      - Edge = current_prob - fair_value

    For a negative Z-score (market moved DOWN):
      - If we think it's underpriced → buy YES
      - Edge = fair_value - current_prob

    We use a dampened reversion target: we don't assume full reversion
    to the mean, but partial reversion proportional to |Z-score|.
    """
    if z_score is None:
        return None, None, None, None

    abs_z = abs(z_score)

    # Dampened reversion: expect 30-60% reversion based on Z magnitude
    # Higher Z → more expected reversion (overreaction is more likely)
    if abs_z >= Z_SCORE_HIGH:
        reversion_pct = 0.50  # Expect 50% reversion for extreme moves
    elif abs_z >= Z_SCORE_MEDIUM:
        reversion_pct = 0.40
    else:
        reversion_pct = 0.30

    deviation = current_prob - historical_mean
    expected_reversion = deviation * reversion_pct
    fair_value = current_prob - expected_reversion

    # Clamp fair value to valid probability range
    fair_value = max(0.01, min(0.99, fair_value))

    if z_score > 0:
        # Market moved UP → overpriced → sell YES / buy NO
        side = "no"
        our_price_cents = int((1 - current_prob) * 100)
        edge_pct = (current_prob - fair_value) / (1 - current_prob) * 100 if current_prob < 1 else 0
    else:
        # Market moved DOWN → underpriced → buy YES
        side = "yes"
        our_price_cents = int(current_prob * 100)
        edge_pct = (fair_value - current_prob) / current_prob * 100 if current_prob > 0 else 0

    # Subtract estimated fee
    fee_cents = polymarket_fee_cents(our_price_cents)
    edge_pct -= (fee_cents / our_price_cents * 100) if our_price_cents > 0 else 0

    # EV per dollar risked (cents)
    ev_cents = edge_pct * our_price_cents / 100

    return side, edge_pct, ev_cents, fee_cents


# ── Main Scanner ────────────────────────────────────────────────────────────

def scan_polymarket(min_edge_pct=DEFAULT_MIN_EDGE, max_markets=MAX_MARKETS_PER_SCAN):
    """Scan Polymarket for +EV signals using Z-score analysis.

    Process:
    1. Fetch top markets by volume from Gamma API
    2. For each market, pull hourly price history from CLOB API
    3. Calculate Z-score of current price vs recent history
    4. If |Z-score| exceeds threshold, calculate edge and generate signal
    5. Return list of Signal objects compatible with Edge Alert pipeline

    Returns: list of Signal objects
    """
    signals = []
    markets = fetch_active_markets(limit=max_markets)

    if not markets:
        return signals

    for market in markets:
        try:
            # Get price history for YES token
            token_id = market["token_ids"][0] if market["token_ids"] else None
            if not token_id:
                continue

            history = fetch_price_history(token_id, interval="1h", fidelity=72)
            if len(history) < MIN_HISTORY_POINTS:
                continue

            # Extract just the prices for Z-score calculation
            prices = [p for _, p in history]
            current_prob = market["yes_prob"]

            z_score, hist_mean, hist_std = calculate_z_score(prices, current_prob)
            if z_score is None:
                continue

            abs_z = abs(z_score)
            if abs_z < Z_SCORE_SPECULATIVE:
                continue  # Not enough deviation

            # Calculate edge
            side, edge_pct, ev_cents, fee_cents = calculate_polymarket_edge(
                current_prob, hist_mean, z_score
            )
            if side is None or edge_pct < min_edge_pct:
                continue

            our_price_cents = int(current_prob * 100) if side == "yes" else int((1 - current_prob) * 100)

            # Confidence tier based on Z-score magnitude
            if abs_z >= Z_SCORE_HIGH:
                confidence = "HIGH"
            elif abs_z >= Z_SCORE_MEDIUM:
                confidence = "MEDIUM"
            else:
                confidence = "SPECULATIVE"

            # Build ticker from slug (Polymarket doesn't have Kalshi-style tickers)
            slug = market.get("slug", "")
            poly_ticker = f"POLY_{slug[:40].upper().replace('-', '_')}" if slug else f"POLY_{market['condition_id'][:12]}"

            signals.append(Signal(
                timestamp=datetime.now(timezone.utc).isoformat(),
                market_type="polymarket",
                ticker=poly_ticker,
                title=market.get("question", "")[:120],
                side=side,
                our_price_cents=our_price_cents,
                edge_pct=round(edge_pct, 2),
                ev_per_contract=round(ev_cents, 2),
                actual_win_rate=round(hist_mean, 4),  # Historical mean as "calibrated" rate
                implied_prob=round(current_prob, 4),
                fee_cents=round(fee_cents, 2),
                confidence=confidence,
                volume=int(market.get("volume", 0)),
                # Store Z-score and Polymarket-specific data in available fields
                # game_pct repurposed for Z-score magnitude (for display)
                game_pct=round(abs_z, 3),
                sport=market.get("category", "event"),  # category field
            ))

            # Rate limit between API calls
            time.sleep(MIN_DELAY)

        except Exception as e:
            print(f"  [!] Polymarket scan error for {market.get('question', '?')[:50]}: {e}")
            continue

    # Sort by absolute edge descending
    signals.sort(key=lambda s: abs(s.edge_pct), reverse=True)
    return signals


# ── Polymarket Settlement Check ─────────────────────────────────────────────

def check_polymarket_settlement(condition_id):
    """Check if a Polymarket market has resolved.

    Returns (result, final_price) or (None, None) if still open.
    result is "yes" or "no".
    """
    params = {"id": condition_id}
    data = gamma_get("/markets", params=params)
    if not data:
        return None, None

    market = data[0] if isinstance(data, list) and data else data
    if not isinstance(market, dict):
        return None, None

    resolved = market.get("resolved", False)
    if not resolved:
        return None, None

    # Polymarket resolution: outcomePrices will be [1, 0] or [0, 1]
    outcome_prices = market.get("outcomePrices", "")
    if isinstance(outcome_prices, str):
        try:
            prices = json.loads(outcome_prices)
        except:
            return None, None
    else:
        prices = outcome_prices

    if not prices or len(prices) < 2:
        return None, None

    yes_price = float(prices[0])
    if yes_price >= 0.99:
        return "yes", 100
    elif yes_price <= 0.01:
        return "no", 0
    else:
        return None, None  # Ambiguous resolution


# ── Standalone Testing ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Polymarket Scanner — Z-Score Edge Detection")
    parser.add_argument("--min-edge", type=float, default=1.0, help="Min edge %% to report")
    parser.add_argument("--max-markets", type=int, default=30, help="Max markets to scan")
    parser.add_argument("--verbose", action="store_true", help="Show scan progress")
    args = parser.parse_args()

    print(f"\n  Polymarket Scanner — scanning top {args.max_markets} markets...")
    print(f"  Min edge: {args.min_edge}% | Z-score thresholds: {Z_SCORE_SPECULATIVE}/{Z_SCORE_MEDIUM}/{Z_SCORE_HIGH}\n")

    signals = scan_polymarket(min_edge_pct=args.min_edge, max_markets=args.max_markets)

    if not signals:
        print("  No signals found. Markets may not have sufficient Z-score deviation.")
    else:
        print(f"  Found {len(signals)} signal(s):\n")
        for s in signals:
            z_display = s.game_pct  # We stored |Z-score| in game_pct
            print(f"  [{s.confidence}] {s.side.upper()} {s.ticker}")
            print(f"    {s.title}")
            print(f"    Price: {s.our_price_cents}c | Edge: {s.edge_pct:+.1f}% | EV: {s.ev_per_contract:.1f}c")
            print(f"    Z-score: {z_display:.2f} | Category: {s.sport}")
            print(f"    Volume: ${s.volume:,.0f}")
            print()
