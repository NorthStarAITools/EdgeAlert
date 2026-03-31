#!/usr/bin/env python3
"""Edge Alert — Signal Engine.

Scans Kalshi prediction markets for +EV opportunities using calibrated
win rates from 80K+ historical markets. No trades placed — analysis only.

Supports:
  - Crypto 15-min markets (KXBTC15M)
  - Sports in-game markets (NBA, NHL, NCAAMB, MLB — moneyline, spread, totals)

Output: structured alerts with edge %, EV per contract, recommended side,
and confidence tier. Designed to feed into Telegram bot, email digest,
or web dashboard.
"""

import json
import os
import time
import requests
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field, asdict
from typing import Optional

# ── Calibration Data ─────────────────────────────────────────────────────────
# Embedded from 80K+ market backtest. These are verified actual win rates
# at each price bucket, net of selection bias, from Kalshi settlement data.

# Sports: late-game calibration (85%+ game completion, 60K markets)
LATE_GAME_CAL = {
    (1, 5): 0.0044,    (6, 10): 0.0198,   (11, 15): 0.0541,
    (16, 20): 0.0915,  (21, 25): 0.1265,  (26, 30): 0.1586,
    (31, 35): 0.2081,  (36, 40): 0.2599,  (41, 45): 0.3297,
    (46, 50): 0.4372,  (51, 55): 0.5679,  (56, 60): 0.6618,
    (61, 65): 0.7442,  (66, 70): 0.8060,  (71, 75): 0.8531,
    (76, 80): 0.8832,  (81, 85): 0.9257,  (86, 90): 0.9530,
    (91, 95): 0.9815,  (96, 100): 0.9972,
}

# Crypto 15-min: simplified calibration buckets (19K markets)
# Maps (minute_in_window, price_bucket_lo, price_bucket_hi) → win_rate
# We embed the key profitable zones only
CRYPTO_CAL = {
    # Final 3 minutes (minutes 13-15), high-price buckets
    # Format: {minute: {(lo, hi): win_rate}}
    15: {(71, 80): 0.840, (81, 90): 0.951, (91, 99): 0.985},
    14: {(71, 80): 0.815, (81, 90): 0.930, (91, 99): 0.975},
    13: {(71, 80): 0.790, (81, 90): 0.905, (91, 99): 0.960},
    12: {(71, 80): 0.770, (81, 90): 0.880, (91, 99): 0.950},
}

# ESPN endpoints for live game detection
ESPN_ENDPOINTS = {
    "KXNBAGAME": "basketball/nba",
    "KXNBASPREAD": "basketball/nba",
    "KXNBATOTAL": "basketball/nba",
    "KXNCAAMBGAME": "basketball/mens-college-basketball",
    "KXNCAAMBSPREAD": "basketball/mens-college-basketball",
    "KXNCAAMBTOTAL": "basketball/mens-college-basketball",
    "KXNHLGAME": "hockey/nhl",
    "KXNHLSPREAD": "hockey/nhl",
    "KXNHLTOTAL": "hockey/nhl",
    "KXMLBGAME": "baseball/mlb",
    "KXMLBTOTAL": "baseball/mlb",
}

GAME_DURATION = {
    "basketball/nba": 48,       # 48 minutes
    "hockey/nhl": 60,           # 60 minutes
    "basketball/mens-college-basketball": 40,  # 40 minutes
    "baseball/mlb": 27,         # 27 outs (9 innings × 3 outs) — used as unit for pct calc
}

# Kalshi API (public endpoints — no auth needed for market data)
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

# Series to scan
CRYPTO_SERIES = ["KXBTC15M"]
SPORTS_SERIES = list(ESPN_ENDPOINTS.keys())


# ── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class Signal:
    """A single trading signal / alert."""
    timestamp: str
    market_type: str          # "crypto" or "sports"
    ticker: str               # Kalshi market ticker
    title: str                # Human-readable market title
    side: str                 # "yes" or "no"
    our_price_cents: int      # What we'd pay (cents)
    edge_pct: float           # Edge net of fees (%)
    ev_per_contract: float    # Expected value per $1 risked (cents)
    actual_win_rate: float    # Calibrated win rate
    implied_prob: float       # Market-implied probability
    fee_cents: float          # Kalshi taker fee (cents)
    confidence: str           # "HIGH", "MEDIUM", "SPECULATIVE"
    minutes_left: Optional[int] = None      # For crypto: minutes left in window
    game_pct: Optional[float] = None        # For sports: game completion %
    sport: Optional[str] = None             # For sports: which sport
    yes_bid: int = 0
    yes_ask: int = 0
    spread: int = 0
    volume: int = 0
    calibration_n: int = 0    # Sample size backing the calibration

    @property
    def summary(self):
        side_label = self.side.upper()
        price = self.our_price_cents
        if self.market_type == "crypto":
            ctx = f"Min {self.minutes_left or '?'}"
        else:
            ctx = f"{(self.game_pct or 0)*100:.0f}% complete"
        return (
            f"[{self.confidence}] {side_label} {self.ticker} @ {price}c | "
            f"Edge: {self.edge_pct:+.1f}% | EV: {self.ev_per_contract:.1f}c | "
            f"WR: {self.actual_win_rate*100:.1f}% | {ctx}"
        )


# ── Fee Calculation ──────────────────────────────────────────────────────────

def kalshi_fee_cents(price_cents):
    """Kalshi taker fee: 0.07 * P * (1-P) * 100. Returns fee in cents."""
    p = price_cents / 100
    return 0.07 * p * (1 - p) * 100


# ── Sports Edge Calculation ──────────────────────────────────────────────────

def get_sports_win_rate(yes_price_cents):
    """Look up calibrated win rate for late-game sports markets."""
    for (lo, hi), rate in LATE_GAME_CAL.items():
        if lo <= yes_price_cents <= hi:
            return rate
    return yes_price_cents / 100  # Fallback: use market price as probability


def calculate_sports_edge(yes_price_cents):
    """Calculate edge for both YES and NO sides, net of Kalshi taker fee.
    Returns (best_side, edge_pct, ev_cents, actual_wr, implied_prob, fee)."""
    actual_wr = get_sports_win_rate(yes_price_cents)
    no_price = 100 - yes_price_cents

    yes_fee = kalshi_fee_cents(yes_price_cents)
    no_fee = kalshi_fee_cents(no_price)

    # YES side: win (100 - price) if correct, lose price if wrong, minus fee
    yes_ev = actual_wr * (100 - yes_price_cents) - (1 - actual_wr) * yes_price_cents - yes_fee
    yes_roi = (yes_ev / yes_price_cents * 100) if yes_price_cents > 0 else 0

    # NO side: win price if wrong outcome, lose (100 - price) if correct, minus fee
    no_ev = (1 - actual_wr) * yes_price_cents - actual_wr * no_price - no_fee
    no_roi = (no_ev / no_price * 100) if no_price > 0 else 0

    if yes_roi > no_roi:
        return "yes", yes_roi, yes_ev, actual_wr, yes_price_cents / 100, yes_fee
    else:
        return "no", no_roi, no_ev, actual_wr, yes_price_cents / 100, no_fee


# ── Crypto Edge Calculation ──────────────────────────────────────────────────

def get_crypto_win_rate(minute, yes_price_cents):
    """Look up calibrated win rate for crypto 15-min markets."""
    minute_cal = CRYPTO_CAL.get(minute)
    if not minute_cal:
        return None
    for (lo, hi), rate in minute_cal.items():
        if lo <= yes_price_cents <= hi:
            return rate
    return None


def calculate_crypto_edge(minute, yes_price_cents):
    """Calculate edge for crypto markets. Returns same tuple as sports."""
    actual_wr = get_crypto_win_rate(minute, yes_price_cents)
    if actual_wr is None:
        return None, 0, 0, 0, 0, 0

    no_price = 100 - yes_price_cents
    yes_fee = kalshi_fee_cents(yes_price_cents)
    no_fee = kalshi_fee_cents(no_price)

    yes_ev = actual_wr * (100 - yes_price_cents) - (1 - actual_wr) * yes_price_cents - yes_fee
    yes_roi = (yes_ev / yes_price_cents * 100) if yes_price_cents > 0 else 0

    no_ev = (1 - actual_wr) * yes_price_cents - actual_wr * no_price - no_fee
    no_roi = (no_ev / no_price * 100) if no_price > 0 else 0

    if yes_roi > no_roi:
        return "yes", yes_roi, yes_ev, actual_wr, yes_price_cents / 100, yes_fee
    else:
        return "no", no_roi, no_ev, actual_wr, yes_price_cents / 100, no_fee


# ── Live Game Detection ──────────────────────────────────────────────────────

def get_game_pct(sport, period, clock_str, detail=""):
    """Calculate game completion percentage from ESPN data.

    For time-based sports (NBA, NHL, NCAAMB): uses period + clock remaining.
    For baseball: uses inning number + top/bottom from detail string.
    Baseball has 27 outs in regulation (9 innings × 3 outs per half-inning).
    """
    # Baseball is inning-based, not time-based
    if "baseball" in sport or "mlb" in sport.lower():
        # 18 half-innings in a 9-inning game
        # period = current inning number
        # detail contains "Top 7th", "Bot 8th", "Mid 7th", "End 7th", etc.
        detail_lower = detail.lower() if detail else ""
        is_bottom = "bot" in detail_lower or "end" in detail_lower
        # Half-innings complete: (inning - 1) * 2 + 1 if bottom half
        half_innings_done = (period - 1) * 2 + (1 if is_bottom else 0)
        return min(1.0, max(0.0, half_innings_done / 18.0))

    # Time-based sports
    total = GAME_DURATION.get(sport, 48)
    try:
        if ":" in str(clock_str):
            parts = clock_str.split(":")
            mins_left = int(parts[0])
            secs_left = int(parts[1]) if len(parts) > 1 else 0
        else:
            mins_left = 0
            secs_left = float(clock_str)
        mins_left_decimal = mins_left + secs_left / 60
    except Exception:
        return -1

    if "nba" in sport:
        period_len = 12
    elif "nhl" in sport:
        period_len = 20
    elif "college" in sport:
        period_len = 20
    else:
        period_len = total / 4

    mins_played = (period - 1) * period_len + (period_len - mins_left_decimal)
    return min(1.0, max(0.0, mins_played / total))


def get_live_games():
    """Get live games from ESPN. Returns list of (sport, team_names_set1, team_names_set2, phase, game_pct)."""
    live = []
    seen = set()
    for series, sport in ESPN_ENDPOINTS.items():
        if sport in seen:
            continue
        seen.add(sport)
        try:
            now = datetime.now()
            dates = [now.strftime('%Y%m%d')]
            if now.hour < 6:
                dates.append((now - timedelta(days=1)).strftime('%Y%m%d'))

            for date_str in dates:
                params = {'dates': date_str, 'limit': 200}
                if 'college' in sport:
                    params['groups'] = '50'
                r = requests.get(
                    f"https://site.api.espn.com/apis/site/v2/sports/{sport}/scoreboard",
                    params=params, timeout=5)
                for event in r.json().get("events", []):
                    status = event.get("status", {}).get("type", {}).get("name", "")
                    if status != "STATUS_IN_PROGRESS":
                        continue
                    status_obj = event.get("status", {})
                    period = status_obj.get("period", 0)
                    if period < 1:
                        continue
                    clock = status_obj.get("displayClock", "0:00")
                    detail = status_obj.get("type", {}).get("detail", "")
                    game_pct = get_game_pct(sport, period, clock, detail=detail)
                    if game_pct <= 0.01:
                        continue
                    phase = "late" if game_pct >= 0.85 else ("mid" if game_pct >= 0.5 else "early")

                    teams = []
                    for comp in event.get("competitions", [{}]):
                        for team in comp.get("competitors", []):
                            t = team.get("team", {})
                            names = set()
                            for f in ["displayName", "shortDisplayName", "abbreviation"]:
                                v = t.get(f, "")
                                if v:
                                    names.add(v.lower())
                            loc = t.get("location", "").lower()
                            if loc and loc not in ("los angeles", "new york", "la"):
                                names.add(loc)
                            teams.append(names)
                    if len(teams) == 2:
                        live.append((sport, teams[0], teams[1], phase, game_pct))
        except Exception as e:
            print(f"  [!] ESPN error for {sport}: {e}")
    return live


def is_game_live(title, live_games, series_ticker=""):
    """Check if a market title matches a live game."""
    title_lower = title.lower()
    series_sport = ESPN_ENDPOINTS.get(series_ticker, "")
    for sport, team1, team2, phase, game_pct in live_games:
        if series_sport and sport != series_sport:
            continue
        t1 = any(n in title_lower for n in team1 if len(n) > 3)
        t2 = any(n in title_lower for n in team2 if len(n) > 3)
        if t1 and t2:
            return True, phase, game_pct, sport
    return False, None, 0, None


# ── Market Scanner ───────────────────────────────────────────────────────────

def fetch_kalshi_markets(series_list, status="open"):
    """Fetch markets from Kalshi public API. No auth needed for market data."""
    all_markets = []
    for series in series_list:
        try:
            # Get events for this series
            r = requests.get(
                f"{KALSHI_API}/events",
                params={"series_ticker": series, "status": "open", "limit": 10},
                timeout=10)
            if r.status_code != 200:
                continue
            events = r.json().get("events", [])

            for evt in events:
                event_ticker = evt.get("event_ticker", "")
                if not event_ticker:
                    continue
                # Get markets for this event
                mr = requests.get(
                    f"{KALSHI_API}/markets",
                    params={"event_ticker": event_ticker, "limit": 50},
                    timeout=10)
                if mr.status_code != 200:
                    continue
                for m in mr.json().get("markets", []):
                    m["_series"] = series
                    all_markets.append(m)
        except Exception as e:
            print(f"  [!] Kalshi API error for {series}: {e}")
    return all_markets


def scan_sports(min_edge_pct=1.0, min_game_pct=0.85, max_spread=10):
    """Scan sports markets for +EV signals. Only late-game markets.
    MLB uses 90% completion threshold (top of 9th or later) — more
    conservative because baseball has higher variance in earlier innings."""
    # Per-sport minimum game completion thresholds
    SPORT_MIN_PCT = {
        "baseball/mlb": 0.88,   # Top 9th+ (16/18 half-innings = 0.889)
    }
    signals = []
    live_games = get_live_games()

    if not live_games:
        return signals

    late_games = [(s, t1, t2, ph, gp) for s, t1, t2, ph, gp in live_games
                  if gp >= SPORT_MIN_PCT.get(s, min_game_pct)]
    if not late_games:
        return signals

    markets = fetch_kalshi_markets(SPORTS_SERIES)

    for m in markets:
        ticker = m.get("ticker", "")
        title = m.get("title", "")
        status = m.get("status", "")
        series = m.get("_series", "")

        if status != "active":
            continue

        is_live, phase, game_pct, sport = is_game_live(title, live_games, series)
        # Apply sport-specific threshold
        effective_min_pct = SPORT_MIN_PCT.get(sport, min_game_pct)
        if not is_live or game_pct < effective_min_pct:
            continue

        yes_bid = m.get("yes_bid", 0) or 0
        yes_ask = m.get("yes_ask", 0) or 0
        if not yes_bid or not yes_ask:
            continue

        spread = yes_ask - yes_bid
        if spread > max_spread:
            continue

        yes_mid = (yes_bid + yes_ask) // 2
        side, edge, ev, wr, implied, fee = calculate_sports_edge(yes_mid)

        if edge < min_edge_pct:
            continue

        our_price = yes_mid if side == "yes" else (100 - yes_mid)

        # Confidence tier
        if edge >= 5.0 and wr >= 0.85:
            confidence = "HIGH"
        elif edge >= 2.0:
            confidence = "MEDIUM"
        else:
            confidence = "SPECULATIVE"

        signals.append(Signal(
            timestamp=datetime.now(timezone.utc).isoformat(),
            market_type="sports",
            ticker=ticker,
            title=title,
            side=side,
            our_price_cents=our_price,
            edge_pct=round(edge, 2),
            ev_per_contract=round(ev, 2),
            actual_win_rate=round(wr, 4),
            implied_prob=round(implied, 4),
            fee_cents=round(fee, 2),
            confidence=confidence,
            game_pct=round(game_pct, 3),
            sport=sport,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            spread=spread,
            volume=m.get("volume", 0) or 0,
        ))

    # Sort by edge descending
    signals.sort(key=lambda s: s.edge_pct, reverse=True)
    return signals


def scan_crypto(min_edge_pct=1.0, max_spread=12):
    """Scan crypto 15-min markets for +EV signals."""
    signals = []
    markets = fetch_kalshi_markets(CRYPTO_SERIES)

    now = datetime.now(timezone.utc)

    for m in markets:
        ticker = m.get("ticker", "")
        title = m.get("title", "")
        status = m.get("status", "")

        if status != "active":
            continue

        yes_bid = m.get("yes_bid", 0) or 0
        yes_ask = m.get("yes_ask", 0) or 0
        if not yes_bid or not yes_ask:
            continue

        spread = yes_ask - yes_bid
        if spread > max_spread:
            continue

        yes_mid = (yes_bid + yes_ask) // 2

        # Skip the dead zone (31-70c YES mid)
        if 31 <= yes_mid <= 70:
            continue

        # Determine minute in window from close_time
        close_time_str = m.get("close_time", "")
        if not close_time_str:
            continue

        try:
            close_time = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            mins_left = (close_time - now).total_seconds() / 60
            minute = 15 - int(mins_left)  # minute 1 = start, minute 15 = final
            if mins_left <= 0 or minute < 12 or minute > 15:
                continue  # Only signal on final 3 minutes, skip expired
        except Exception:
            continue

        side, edge, ev, wr, implied, fee = calculate_crypto_edge(minute, yes_mid)
        if side is None or edge < min_edge_pct:
            continue

        our_price = yes_mid if side == "yes" else (100 - yes_mid)

        if edge >= 5.0:
            confidence = "HIGH"
        elif edge >= 2.0:
            confidence = "MEDIUM"
        else:
            confidence = "SPECULATIVE"

        signals.append(Signal(
            timestamp=datetime.now(timezone.utc).isoformat(),
            market_type="crypto",
            ticker=ticker,
            title=title,
            side=side,
            our_price_cents=our_price,
            edge_pct=round(edge, 2),
            ev_per_contract=round(ev, 2),
            actual_win_rate=round(wr, 4),
            implied_prob=round(implied, 4),
            fee_cents=round(fee, 2),
            confidence=confidence,
            minutes_left=15 - minute,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            spread=spread,
            volume=m.get("volume", 0) or 0,
        ))

    signals.sort(key=lambda s: s.edge_pct, reverse=True)
    return signals


def scan_all(min_edge_pct=1.0):
    """Run full scan across all market types. Returns dict of signal lists."""
    results = {
        "crypto": scan_crypto(min_edge_pct=min_edge_pct),
        "sports": scan_sports(min_edge_pct=min_edge_pct),
        "scan_time": datetime.now(timezone.utc).isoformat(),
        "total_signals": 0,
    }
    results["total_signals"] = len(results["crypto"]) + len(results["sports"])
    return results
