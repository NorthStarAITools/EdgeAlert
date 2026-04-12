#!/usr/bin/env python3
"""Edge Alert — Email Delivery Service.

Sends transactional emails via SMTP (Gmail or configurable).
Falls back to logging if SMTP isn't configured.

Templates:
  - Welcome (post-payment)
  - Daily digest
  - Payment failed
  - Cancellation confirmation

All templates include unsubscribe via Stripe billing portal.
"""

import logging
import os
import smtplib
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

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

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5050")

MAX_RETRIES = 3
SMTP_TIMEOUT = 30


def is_configured():
    """Check if SMTP is configured."""
    return bool(SMTP_USER and SMTP_PASSWORD and FROM_EMAIL)


# ── Core send function ──────────────────────────────────────────────────────

def send_email(to_email, subject, html_body, text_body=None):
    """Send an email via SMTP with retry logic.

    If SMTP isn't configured, logs what would be sent and returns True.
    Returns True on success, False on failure.
    """
    if not is_configured():
        logger.info(f"[EMAIL-OFFLINE] Would send to {to_email}: {subject}")
        logger.info(f"[EMAIL-OFFLINE] Body preview: {(text_body or html_body)[:200]}")
        return True

    msg = MIMEMultipart("alternative")
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg["Subject"] = subject

    if text_body:
        msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.send_message(msg)
            logger.info(f"Email sent to {to_email}: {subject}")
            return True
        except smtplib.SMTPAuthenticationError as e:
            logger.error(f"SMTP auth failed (attempt {attempt}/{MAX_RETRIES}): {e}")
            return False  # Don't retry auth failures
        except (smtplib.SMTPException, OSError) as e:
            logger.warning(f"SMTP error (attempt {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)  # Exponential backoff
            else:
                logger.error(f"Failed to send email to {to_email} after {MAX_RETRIES} attempts")
                return False
    return False


# ── Email Templates ─────────────────────────────────────────────────────────

def _base_html(content, unsubscribe_url=None):
    """Wrap content in a simple, clean HTML email template."""
    unsub = ""
    if unsubscribe_url:
        unsub = f'<p style="margin-top:32px;font-size:12px;color:#888;">To manage your subscription or unsubscribe, visit your <a href="{unsubscribe_url}" style="color:#6c63ff;">billing portal</a>.</p>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<div style="max-width:560px;margin:0 auto;padding:32px 20px;">
<div style="background:#fff;border-radius:12px;padding:32px;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
<div style="font-size:20px;font-weight:700;margin-bottom:4px;">Edge Alert</div>
<div style="font-size:12px;color:#888;margin-bottom:24px;">by North Star AI Tools</div>
{content}
{unsub}
</div>
<p style="text-align:center;font-size:11px;color:#aaa;margin-top:16px;">
Not financial advice. Signals are for educational and entertainment purposes only.
</p>
</div>
</body></html>"""


def send_welcome_email(to_email, tier, telegram_invite_link, dashboard_url=None):
    """Send welcome email after successful payment."""
    billing_url = f"{APP_BASE_URL}/billing/portal"
    dash_url = dashboard_url or f"{APP_BASE_URL}/dashboard"

    tier_label = "Pro" if tier == "pro" else "Basic"
    tier_desc = (
        "all real-time signals (crypto + sports) via Telegram, daily digest emails, API access, and the full accuracy dashboard"
        if tier == "pro"
        else "crypto signal alerts via Telegram and daily digest emails"
    )

    content = f"""
<h2 style="margin:0 0 16px;font-size:22px;">Welcome to Edge Alert!</h2>
<p style="color:#333;line-height:1.6;">
You're now subscribed to <strong>Edge Alert {tier_label}</strong>. Here's what you get:
</p>
<p style="color:#555;line-height:1.7;">{tier_desc}</p>

<h3 style="margin:24px 0 12px;font-size:16px;">Get started in 3 steps:</h3>

<div style="background:#f8f8ff;border-radius:8px;padding:16px;margin-bottom:16px;">
<p style="margin:0 0 12px;"><strong>Step 1: Join the Telegram signals channel</strong></p>
<a href="{telegram_invite_link}" style="display:inline-block;background:#6c63ff;color:#fff;padding:10px 20px;border-radius:8px;text-decoration:none;font-weight:600;">Join Telegram Channel</a>
<p style="font-size:12px;color:#888;margin-top:8px;">This invite link is unique to you. Don't share it.</p>
</div>

<div style="background:#f8f8ff;border-radius:8px;padding:16px;margin-bottom:16px;">
<p style="margin:0;"><strong>Step 2: Check the accuracy dashboard</strong></p>
<p style="margin:4px 0 0;"><a href="{dash_url}" style="color:#6c63ff;">{dash_url}</a></p>
</div>

<div style="background:#f8f8ff;border-radius:8px;padding:16px;">
<p style="margin:0;"><strong>Step 3: Manage your billing</strong></p>
<p style="margin:4px 0 0;"><a href="{billing_url}" style="color:#6c63ff;">Billing Portal</a></p>
</div>

<p style="margin-top:24px;color:#555;line-height:1.6;">
Signals arrive as they're detected — typically during market hours for sports
and 24/7 for crypto. You'll also get a daily digest summary each morning.
</p>
"""
    return send_email(
        to_email,
        f"Welcome to Edge Alert {tier_label}!",
        _base_html(content, billing_url),
        text_body=f"Welcome to Edge Alert {tier_label}! Join Telegram: {telegram_invite_link} | Dashboard: {dash_url} | Billing: {billing_url}",
    )


def send_daily_digest_email(to_email, digest_html):
    """Send the daily digest summary email."""
    billing_url = f"{APP_BASE_URL}/billing/portal"
    content = f"""
<h2 style="margin:0 0 16px;font-size:22px;">Daily Signal Digest</h2>
<div style="color:#333;line-height:1.7;">
{digest_html}
</div>
<p style="margin-top:20px;">
<a href="{APP_BASE_URL}/dashboard" style="color:#6c63ff;">View full accuracy dashboard &rarr;</a>
</p>
"""
    return send_email(
        to_email,
        f"Edge Alert — Daily Digest ({datetime.now().strftime('%b %d')})",
        _base_html(content, billing_url),
    )


def send_payment_failed_email(to_email):
    """Send payment failure notification."""
    billing_url = f"{APP_BASE_URL}/billing/portal"
    content = f"""
<h2 style="margin:0 0 16px;font-size:22px;color:#e53935;">Payment Failed</h2>
<p style="color:#333;line-height:1.6;">
Your Edge Alert payment didn't go through. This usually means your card
needs to be updated.
</p>
<p style="color:#333;line-height:1.6;">
Your signal access will be paused until payment is resolved. Update your
payment method to restore access:
</p>
<a href="{billing_url}" style="display:inline-block;background:#6c63ff;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;margin-top:12px;">Update Payment Method</a>
"""
    return send_email(
        to_email,
        "Edge Alert — Payment Failed",
        _base_html(content, billing_url),
    )


def send_cancellation_email(to_email, period_end=None):
    """Send cancellation confirmation."""
    billing_url = f"{APP_BASE_URL}/billing/portal"
    end_note = ""
    if period_end:
        end_note = f"<p style='color:#555;'>You'll retain access until <strong>{period_end}</strong>.</p>"

    content = f"""
<h2 style="margin:0 0 16px;font-size:22px;">Subscription Cancelled</h2>
<p style="color:#333;line-height:1.6;">
Your Edge Alert subscription has been cancelled.
</p>
{end_note}
<p style="color:#555;line-height:1.6;">
After your access period ends, you'll no longer receive signals via Telegram
or daily digest emails. Your accuracy dashboard data will remain available.
</p>
<p style="color:#555;line-height:1.6;">
Changed your mind? You can resubscribe anytime:
</p>
<a href="{APP_BASE_URL}" style="display:inline-block;background:#6c63ff;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;">Resubscribe</a>
"""
    return send_email(
        to_email,
        "Edge Alert — Subscription Cancelled",
        _base_html(content, billing_url),
    )
