#!/usr/bin/env python3
"""Edge Alert — Customer Database (SQLite).

Models:
  - Customer: subscription state, Stripe IDs, Telegram info
  - SignalDelivery: tracks what was sent to whom and when

All secrets (API keys, tokens) live in .env — this module only stores
customer metadata and delivery records.
"""

import os
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager

DB_PATH = os.environ.get(
    "DATABASE_URL",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "edge_alert.db"),
).replace("sqlite:///", "")

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    stripe_customer_id TEXT UNIQUE,
    stripe_subscription_id TEXT UNIQUE,
    tier TEXT NOT NULL DEFAULT 'basic' CHECK(tier IN ('basic', 'pro')),
    telegram_chat_id TEXT,
    telegram_invite_link TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'cancelled', 'past_due')),
    current_period_end TEXT
);

CREATE TABLE IF NOT EXISTS signal_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    signal_id TEXT NOT NULL,
    channel TEXT NOT NULL CHECK(channel IN ('telegram', 'email')),
    delivered_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'sent' CHECK(status IN ('sent', 'failed', 'pending')),
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE INDEX IF NOT EXISTS idx_customers_stripe ON customers(stripe_customer_id);
CREATE INDEX IF NOT EXISTS idx_customers_email ON customers(email);
CREATE INDEX IF NOT EXISTS idx_customers_status ON customers(status);
CREATE INDEX IF NOT EXISTS idx_deliveries_customer ON signal_deliveries(customer_id);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_db():
    """Context manager for database connections with WAL mode."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create tables if they don't exist."""
    with get_db() as conn:
        conn.executescript(SCHEMA)


# ── Customer CRUD ───────────────────────────────────────────────────────────

def create_customer(email, stripe_customer_id=None, stripe_subscription_id=None,
                    tier="basic", telegram_chat_id=None, telegram_invite_link=None,
                    current_period_end=None):
    """Create a new customer record. Returns the new customer ID."""
    now = _now()
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO customers
               (email, stripe_customer_id, stripe_subscription_id, tier,
                telegram_chat_id, telegram_invite_link, created_at, updated_at,
                status, current_period_end)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
            (email, stripe_customer_id, stripe_subscription_id, tier,
             telegram_chat_id, telegram_invite_link, now, now, current_period_end),
        )
        return cursor.lastrowid


def get_customer_by_email(email):
    """Look up customer by email. Returns dict or None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE email = ?", (email,)
        ).fetchone()
        return dict(row) if row else None


def get_customer_by_stripe_id(stripe_customer_id):
    """Look up customer by Stripe customer ID."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM customers WHERE stripe_customer_id = ?",
            (stripe_customer_id,),
        ).fetchone()
        return dict(row) if row else None


def get_active_customers(tier=None):
    """Get all active customers, optionally filtered by tier."""
    with get_db() as conn:
        if tier:
            rows = conn.execute(
                "SELECT * FROM customers WHERE status = 'active' AND tier = ?",
                (tier,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM customers WHERE status = 'active'"
            ).fetchall()
        return [dict(r) for r in rows]


def update_customer(customer_id, **fields):
    """Update customer fields. Only updates provided fields."""
    allowed = {
        "email", "stripe_customer_id", "stripe_subscription_id", "tier",
        "telegram_chat_id", "telegram_invite_link", "status", "current_period_end",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    updates["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [customer_id]
    with get_db() as conn:
        conn.execute(
            f"UPDATE customers SET {set_clause} WHERE id = ?", values
        )


def update_customer_by_stripe_id(stripe_customer_id, **fields):
    """Update customer by Stripe customer ID."""
    customer = get_customer_by_stripe_id(stripe_customer_id)
    if customer:
        update_customer(customer["id"], **fields)
    return customer


# ── Signal Delivery Tracking ────────────────────────────────────────────────

def log_delivery(customer_id, signal_id, channel, status="sent"):
    """Log a signal delivery attempt."""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO signal_deliveries
               (customer_id, signal_id, channel, delivered_at, status)
               VALUES (?, ?, ?, ?, ?)""",
            (customer_id, signal_id, channel, _now(), status),
        )


def get_deliveries(customer_id, limit=50):
    """Get recent deliveries for a customer."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM signal_deliveries
               WHERE customer_id = ?
               ORDER BY delivered_at DESC LIMIT ?""",
            (customer_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# Initialize on import
init_db()
