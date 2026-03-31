# Edge Alert — Prediction Market Signal Feed

Real-time +EV signal detection for Kalshi prediction markets. Scans crypto 15-min and sports in-game markets using calibrated win rates from 80K+ historical settlements.

## What It Does

Identifies markets where the actual win rate (from calibration data) exceeds the market-implied probability, net of Kalshi's taker fee. Outputs the edge %, expected value per contract, and a confidence tier.

**Markets scanned:**
- Crypto: BTC 15-minute direction markets (final 3 minutes only)
- Sports: NBA, NHL, NCAAMB in-game markets (moneyline, spread, totals — 85%+ game completion)

**Not a trading bot.** No orders placed, no API keys needed for the scanner. This is analysis + alerts only.

## Quick Start

```bash
# One-shot scan (console output)
python3 edge_alert.py

# Continuous scanning (saves history)
python3 edge_alert.py --loop

# Telegram alerts (needs .env config)
python3 telegram_bot.py --scan

# Daily digest
python3 edge_alert.py --digest
```

## Files

| File | Purpose |
|------|---------|
| edge_engine.py | Core signal engine — calibration data, EV calculations, market scanning |
| edge_alert.py | CLI runner — one-shot, loop, digest, history modes |
| formatter.py | Output formatting — console, Telegram, JSON, daily digest |
| telegram_bot.py | Telegram delivery — push alerts to channel/group |
| .env.example | Telegram config template |
| data/ | Signal history (JSONL) and daily digests |

## Telegram Setup

1. Message @BotFather on Telegram → `/newbot` → get your token
2. Create a channel, add your bot as admin
3. Copy `.env.example` to `.env`, fill in token + chat ID
4. `python3 telegram_bot.py --test` to verify

## Pricing

- **Basic ($39/mo):** Daily digest emails, 1x morning scan
- **Pro ($49/mo):** Real-time Telegram alerts, continuous scanning
- **Premium ($79/mo):** All signals + accuracy dashboard + historical data export

## Disclaimer

Entertainment and educational purposes only. Not financial advice. Past calibration performance does not guarantee future results. Use at your own risk.
