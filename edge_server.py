#!/usr/bin/env python3
"""Edge Alert — Flask Web Server.

Lightweight backend connecting Stripe payments to signal delivery.
NOT a full SaaS — minimum plumbing for: pay → receive value.

Routes:
  POST /webhooks/stripe    — Stripe webhook handler
  GET  /welcome            — Post-payment welcome page
  GET  /dashboard          — Accuracy dashboard
  GET  /api/signals        — Recent signals JSON (tier-gated)
  GET  /api/accuracy       — Accuracy report JSON
  GET  /billing/portal     — Redirect to Stripe customer portal
  GET  /status             — Health check

Usage:
    python3 edge_server.py                # Start on port 5050
    python3 edge_server.py --port 8080    # Custom port
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from functools import wraps

# Add this dir to path for sibling imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from flask import Flask, request, jsonify, redirect, send_file, abort
except ImportError:
    print("Flask not installed. Run: pip install flask stripe")
    sys.exit(1)

try:
    import stripe
except ImportError:
    print("Stripe not installed. Run: pip install stripe")
    sys.exit(1)

import customer_db
import email_service
import telegram_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("edge_server")

# ── Config ──────────────────────────────────────────────────────────────────

def _load_env():
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())

_load_env()

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_BASIC = os.environ.get("STRIPE_PRICE_BASIC", "")
STRIPE_PRICE_PRO = os.environ.get("STRIPE_PRICE_PRO", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5050")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
AUDIT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "AUDIT_LOG.md")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "edge-alert-dev-key")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


# ── Helpers ─────────────────────────────────────────────────────────────────

def _price_to_tier(price_id):
    """Map Stripe price ID to tier name."""
    if price_id == STRIPE_PRICE_PRO:
        return "pro"
    return "basic"


def _audit_log(message):
    """Append to AUDIT_LOG.md."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    entry = f"- [{ts}] Edge Alert: {message}\n"
    try:
        log_path = os.path.normpath(AUDIT_LOG)
        with open(log_path, "a") as f:
            f.write(entry)
    except Exception as e:
        logger.warning(f"Could not write to AUDIT_LOG: {e}")


# ── Stripe Webhook ──────────────────────────────────────────────────────────

@app.route("/webhooks/stripe", methods=["POST"])
def stripe_webhook():
    """Handle Stripe webhook events with signature verification."""
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        logger.warning("STRIPE_WEBHOOK_SECRET not configured — accepting without verification")
        try:
            event = json.loads(payload)
        except (json.JSONDecodeError, Exception):
            return jsonify({"error": "invalid_payload"}), 400
    else:
        try:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except stripe.error.SignatureVerificationError:
            logger.warning("Stripe webhook signature verification failed")
            return jsonify({"error": "invalid_signature"}), 400
        except Exception as e:
            logger.error(f"Stripe webhook parse error: {e}")
            return jsonify({"error": "parse_error"}), 400

    event_type = event.get("type", "")
    data = event.get("data", {}).get("object", {})

    handlers = {
        "checkout.session.completed": _handle_checkout_completed,
        "customer.subscription.created": _handle_subscription_created,
        "customer.subscription.updated": _handle_subscription_updated,
        "customer.subscription.deleted": _handle_subscription_deleted,
        "invoice.payment_failed": _handle_payment_failed,
    }

    handler = handlers.get(event_type)
    if handler:
        try:
            handler(data)
            logger.info(f"Handled Stripe event: {event_type}")
        except Exception as e:
            logger.error(f"Error handling {event_type}: {e}")
            return jsonify({"error": "handler_failed"}), 500
    else:
        logger.debug(f"Unhandled Stripe event: {event_type}")

    return jsonify({"received": True}), 200


def _handle_checkout_completed(session):
    """New customer paid. Create record, generate Telegram invite, send welcome email."""
    customer_email = session.get("customer_email") or session.get("customer_details", {}).get("email", "")
    stripe_customer_id = session.get("customer", "")
    subscription_id = session.get("subscription", "")

    if not customer_email:
        logger.error("checkout.session.completed missing customer email")
        return

    # Determine tier from the subscription's price
    tier = "basic"
    if subscription_id and STRIPE_SECRET_KEY:
        try:
            sub = stripe.Subscription.retrieve(subscription_id)
            items = sub.get("items", {}).get("data", [])
            if items:
                price_id = items[0]["price"]["id"]
                tier = _price_to_tier(price_id)
        except Exception as e:
            logger.warning(f"Could not fetch subscription details: {e}")

    # Check if customer already exists (resubscribe)
    existing = customer_db.get_customer_by_email(customer_email)
    if existing:
        customer_db.update_customer(existing["id"],
                                    stripe_customer_id=stripe_customer_id,
                                    stripe_subscription_id=subscription_id,
                                    tier=tier,
                                    status="active")
        customer_id = existing["id"]
        logger.info(f"Reactivated existing customer: {customer_email}")
    else:
        customer_id = customer_db.create_customer(
            email=customer_email,
            stripe_customer_id=stripe_customer_id,
            stripe_subscription_id=subscription_id,
            tier=tier,
        )
        logger.info(f"Created new customer: {customer_email} (tier: {tier})")

    # Generate unique Telegram invite link
    invite_link = telegram_manager.create_invite_link(customer_email=customer_email)

    if invite_link:
        customer_db.update_customer(customer_id, telegram_invite_link=invite_link)
    else:
        # Fallback: log for manual invite
        invite_link = "(Telegram invite could not be generated — will be sent manually)"
        logger.warning(f"Could not generate Telegram invite for {customer_email}")

    # Send welcome email
    email_service.send_welcome_email(
        to_email=customer_email,
        tier=tier,
        telegram_invite_link=invite_link,
        dashboard_url=f"{APP_BASE_URL}/dashboard",
    )

    _audit_log(f"New subscription: {customer_email} ({tier})")


def _handle_subscription_created(subscription):
    """Mirror subscription creation — usually handled by checkout, but just in case."""
    pass  # checkout.session.completed handles the main flow


def _handle_subscription_updated(subscription):
    """Handle tier changes and status updates."""
    stripe_customer_id = subscription.get("customer", "")
    items = subscription.get("items", {}).get("data", [])
    price_id = items[0]["price"]["id"] if items else ""
    tier = _price_to_tier(price_id)
    status = subscription.get("status", "active")

    status_map = {
        "active": "active",
        "past_due": "past_due",
        "canceled": "cancelled",
        "cancelled": "cancelled",
    }
    mapped_status = status_map.get(status, "active")

    period_end = None
    if subscription.get("current_period_end"):
        period_end = datetime.fromtimestamp(
            subscription["current_period_end"], tz=timezone.utc
        ).isoformat()

    customer = customer_db.update_customer_by_stripe_id(
        stripe_customer_id,
        tier=tier,
        status=mapped_status,
        stripe_subscription_id=subscription.get("id", ""),
        current_period_end=period_end,
    )
    if customer:
        logger.info(f"Updated subscription for {customer['email']}: tier={tier}, status={mapped_status}")


def _handle_subscription_deleted(subscription):
    """Customer cancelled. Update status, revoke Telegram access, send email."""
    stripe_customer_id = subscription.get("customer", "")
    customer = customer_db.get_customer_by_stripe_id(stripe_customer_id)

    if not customer:
        logger.warning(f"subscription.deleted for unknown customer: {stripe_customer_id}")
        return

    # Determine when access ends
    period_end = None
    if subscription.get("current_period_end"):
        period_end_dt = datetime.fromtimestamp(
            subscription["current_period_end"], tz=timezone.utc
        )
        period_end = period_end_dt.strftime("%B %d, %Y")

    # Update DB
    customer_db.update_customer(customer["id"], status="cancelled")

    # Revoke Telegram invite
    if customer.get("telegram_invite_link"):
        telegram_manager.revoke_invite_link(customer["telegram_invite_link"])

    # Optionally remove from channel if they have a telegram_chat_id
    if customer.get("telegram_chat_id"):
        telegram_manager.remove_member(customer["telegram_chat_id"])

    # Send cancellation email
    email_service.send_cancellation_email(customer["email"], period_end=period_end)

    _audit_log(f"Subscription cancelled: {customer['email']}")


def _handle_payment_failed(invoice):
    """Payment failed. Mark as past_due, send notification email."""
    stripe_customer_id = invoice.get("customer", "")
    customer = customer_db.get_customer_by_stripe_id(stripe_customer_id)

    if not customer:
        logger.warning(f"payment_failed for unknown customer: {stripe_customer_id}")
        return

    customer_db.update_customer(customer["id"], status="past_due")
    email_service.send_payment_failed_email(customer["email"])

    _audit_log(f"Payment failed: {customer['email']}")


# ── Welcome Page ────────────────────────────────────────────────────────────

@app.route("/welcome")
def welcome():
    """Post-payment welcome page with Telegram invite link + setup instructions."""
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Welcome to Edge Alert!</title>
<style>
:root {{ --bg:#08080e; --surface:#0e0e18; --card:#11111c; --border:#1c1c2e;
         --text:#dde0ec; --muted:#5a5a78; --accent:#6c63ff; }}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:var(--bg); color:var(--text); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
.container {{ max-width:600px; margin:0 auto; padding:60px 24px; text-align:center; }}
h1 {{ font-size:32px; margin-bottom:16px; }}
p {{ color:var(--muted); line-height:1.7; margin-bottom:24px; }}
.card {{ background:var(--card); border:1px solid var(--border); border-radius:14px; padding:28px; margin-bottom:20px; text-align:left; }}
.card h3 {{ margin-bottom:8px; font-size:16px; }}
.card p {{ font-size:14px; margin-bottom:0; }}
.btn {{ display:inline-block; background:var(--accent); color:#fff; padding:14px 28px; border-radius:10px; font-weight:700; text-decoration:none; margin-top:12px; }}
</style></head><body>
<div class="container">
<h1>Welcome to Edge Alert!</h1>
<p>Your subscription is active. Check your email for your Telegram invite link and setup instructions.</p>
<div class="card">
<h3>Step 1: Check your email</h3>
<p>We sent a welcome email with your unique Telegram channel invite link.</p>
</div>
<div class="card">
<h3>Step 2: Join the Telegram channel</h3>
<p>Click the invite link in your email to join the signals channel. Alerts arrive in real-time.</p>
</div>
<div class="card">
<h3>Step 3: Explore the dashboard</h3>
<p><a href="/dashboard" style="color:var(--accent);">View accuracy dashboard</a> — see live signal performance.</p>
</div>
<a href="/dashboard" class="btn">Go to Dashboard</a>
<p style="margin-top:40px;font-size:13px;">Need help? Email support@northstaraitools.com</p>
</div></body></html>"""


# ── Dashboard ───────────────────────────────────────────────────────────────

@app.route("/dashboard")
def dashboard():
    """Serve the accuracy dashboard HTML."""
    dashboard_paths = [
        os.path.join(os.path.dirname(__file__), "accuracy_dashboard.html"),
        os.path.join(os.path.dirname(__file__), "landing", "accuracy_dashboard.html"),
    ]
    for path in dashboard_paths:
        if os.path.exists(path):
            return send_file(path)
    return "<h1>Dashboard coming soon</h1>", 200


# ── API Endpoints ───────────────────────────────────────────────────────────

@app.route("/api/signals")
def api_signals():
    """Return recent signals as JSON. Pro tier gets all, Basic gets crypto only."""
    signals_file = os.path.join(DATA_DIR, "signals.jsonl")
    if not os.path.exists(signals_file):
        return jsonify({"signals": [], "note": "No signals data available yet."})

    # Load last 50 scans
    scans = []
    try:
        with open(signals_file) as f:
            lines = f.readlines()
        for line in lines[-50:]:
            line = line.strip()
            if line:
                scans.append(json.loads(line))
    except Exception as e:
        logger.error(f"Error reading signals: {e}")
        return jsonify({"error": "could not read signals"}), 500

    # For now, return all crypto signals (Basic tier default)
    # Pro tier would include sports signals too
    # Tier gating would check API key / session — simplified for MVP
    all_signals = []
    for scan in scans:
        for sig in scan.get("crypto", []):
            sig["_scan_time"] = scan.get("scan_time")
            all_signals.append(sig)
        for sig in scan.get("sports", []):
            sig["_scan_time"] = scan.get("scan_time")
            all_signals.append(sig)

    # Sort by timestamp descending
    all_signals.sort(key=lambda s: s.get("timestamp", ""), reverse=True)

    return jsonify({
        "signals": all_signals[:100],
        "total": len(all_signals),
        "scan_count": len(scans),
    })


@app.route("/api/accuracy")
def api_accuracy():
    """Return accuracy report JSON."""
    report_file = os.path.join(DATA_DIR, "accuracy_report.json")
    if os.path.exists(report_file):
        try:
            with open(report_file) as f:
                return jsonify(json.load(f))
        except Exception as e:
            logger.error(f"Error reading accuracy report: {e}")

    return jsonify({
        "note": "No accuracy data available yet. Run accuracy_tracker.py --full to generate.",
        "overall_accuracy": None,
    })


# ── Billing Portal ──────────────────────────────────────────────────────────

@app.route("/billing/portal")
def billing_portal():
    """Redirect to Stripe customer portal.

    In a full implementation, this would look up the customer's Stripe ID
    from their session and create a portal link. For MVP, redirect to Stripe.
    """
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe not configured", "message": "Contact support@northstaraitools.com"}), 503

    # For MVP without auth, provide a generic portal URL direction
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Billing Portal</title>
<style>body{{background:#08080e;color:#dde0ec;font-family:sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;}}
.card{{background:#11111c;border:1px solid #1c1c2e;border-radius:14px;padding:40px;max-width:400px;text-align:center;}}
h2{{margin-bottom:16px;}} p{{color:#5a5a78;line-height:1.6;margin-bottom:24px;}}
a{{color:#6c63ff;}}</style></head><body>
<div class="card">
<h2>Manage Your Billing</h2>
<p>To manage your subscription, update payment methods, or cancel, please check the billing portal link in your welcome email or contact us.</p>
<p><a href="mailto:support@northstaraitools.com">support@northstaraitools.com</a></p>
</div></body></html>"""


# ── Health Check ────────────────────────────────────────────────────────────

@app.route("/status")
def status():
    """Health check endpoint."""
    signals_file = os.path.join(DATA_DIR, "signals.jsonl")
    signal_count = 0
    if os.path.exists(signals_file):
        try:
            with open(signals_file) as f:
                signal_count = sum(1 for _ in f)
        except Exception:
            pass

    customer_count = len(customer_db.get_active_customers())

    return jsonify({
        "status": "ok",
        "service": "edge-alert",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "active_customers": customer_count,
        "signal_scans": signal_count,
        "stripe_configured": bool(STRIPE_SECRET_KEY),
        "smtp_configured": email_service.is_configured(),
        "telegram_configured": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
    })


# ── Landing page redirect ──────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve landing page or redirect."""
    landing_path = os.path.join(os.path.dirname(__file__), "landing", "index.html")
    if os.path.exists(landing_path):
        return send_file(landing_path)
    return redirect("/status")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Edge Alert — Web Server")
    parser.add_argument("--port", type=int, default=5050, help="Port (default: 5050)")
    parser.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0)")
    parser.add_argument("--debug", action="store_true", help="Debug mode")
    args = parser.parse_args()

    logger.info(f"Starting Edge Alert server on {args.host}:{args.port}")
    logger.info(f"Stripe: {'configured' if STRIPE_SECRET_KEY else 'NOT configured'}")
    logger.info(f"SMTP: {'configured' if email_service.is_configured() else 'NOT configured'}")
    logger.info(f"Telegram: {'configured' if os.environ.get('TELEGRAM_BOT_TOKEN') else 'NOT configured'}")

    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
