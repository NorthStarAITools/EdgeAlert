#!/usr/bin/env python3
"""Edge Alert — Telegram Channel Management.

Manages customer access to the signals Telegram channel:
  - Create unique invite links (member_limit=1)
  - Revoke invite links on cancellation
  - Remove members from channel
  - Send welcome DMs to new subscribers

Uses the same bot token as telegram_bot.py.
"""

import logging
import os
import time
import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}"
MAX_RETRIES = 3
REQUEST_TIMEOUT = 10


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


def _get_config():
    return {
        "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "signals_channel_id": os.environ.get("TELEGRAM_SIGNALS_CHANNEL_ID",
                                              os.environ.get("TELEGRAM_SIGNALS_CHAT_ID", "")),
    }


def _api_call(method, params, token=None):
    """Make a Telegram Bot API call with retry logic."""
    cfg = _get_config()
    tok = token or cfg["bot_token"]

    if not tok:
        logger.info(f"[TELEGRAM-OFFLINE] Would call {method} with {params}")
        return {"ok": False, "offline": True}

    url = f"{TELEGRAM_API.format(token=tok)}/{method}"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.post(url, json=params, timeout=REQUEST_TIMEOUT)
            data = r.json()
            if data.get("ok"):
                return data
            # Don't retry client errors (4xx)
            if r.status_code < 500:
                logger.warning(f"Telegram API {method} failed: {data.get('description', 'unknown')}")
                return data
            logger.warning(f"Telegram API {method} server error (attempt {attempt}): {r.status_code}")
        except requests.exceptions.Timeout:
            logger.warning(f"Telegram API {method} timeout (attempt {attempt})")
        except requests.exceptions.RequestException as e:
            logger.warning(f"Telegram API {method} error (attempt {attempt}): {e}")

        if attempt < MAX_RETRIES:
            time.sleep(2 ** attempt)

    logger.error(f"Telegram API {method} failed after {MAX_RETRIES} attempts")
    return {"ok": False, "error": "max_retries_exceeded"}


def create_invite_link(chat_id=None, customer_email=""):
    """Create a unique invite link with member_limit=1.

    Returns the invite link string, or None on failure.
    """
    cfg = _get_config()
    cid = chat_id or cfg["signals_channel_id"]

    if not cid:
        logger.info(f"[TELEGRAM-OFFLINE] Would create invite for {customer_email}")
        return None

    result = _api_call("createChatInviteLink", {
        "chat_id": cid,
        "member_limit": 1,
        "name": f"edge-alert-{customer_email[:30]}",
    })

    if result.get("ok"):
        link = result["result"]["invite_link"]
        logger.info(f"Created invite link for {customer_email}: {link}")
        return link

    logger.error(f"Failed to create invite link for {customer_email}")
    return None


def revoke_invite_link(invite_link, chat_id=None):
    """Revoke a specific invite link.

    Returns True on success.
    """
    cfg = _get_config()
    cid = chat_id or cfg["signals_channel_id"]

    if not cid or not invite_link:
        logger.info(f"[TELEGRAM-OFFLINE] Would revoke invite: {invite_link}")
        return True

    result = _api_call("revokeChatInviteLink", {
        "chat_id": cid,
        "invite_link": invite_link,
    })

    if result.get("ok"):
        logger.info(f"Revoked invite link: {invite_link}")
        return True

    logger.warning(f"Failed to revoke invite link: {invite_link}")
    return False


def remove_member(user_id, chat_id=None):
    """Remove a member from the channel (ban then unban to avoid permanent ban).

    Returns True on success.
    """
    cfg = _get_config()
    cid = chat_id or cfg["signals_channel_id"]

    if not cid or not user_id:
        logger.info(f"[TELEGRAM-OFFLINE] Would remove user {user_id}")
        return True

    # Ban (kicks)
    ban_result = _api_call("banChatMember", {
        "chat_id": cid,
        "user_id": user_id,
    })

    if not ban_result.get("ok"):
        logger.warning(f"Failed to ban user {user_id}: {ban_result}")
        return False

    # Immediately unban to allow future rejoining if they resubscribe
    time.sleep(1)
    _api_call("unbanChatMember", {
        "chat_id": cid,
        "user_id": user_id,
        "only_if_banned": True,
    })

    logger.info(f"Removed user {user_id} from channel")
    return True


def send_welcome_dm(user_id, message):
    """Send a direct message to a user.

    Note: Bot can only DM users who have started a conversation with it.
    Returns True on success.
    """
    if not user_id:
        logger.info(f"[TELEGRAM-OFFLINE] Would DM user: {message[:100]}")
        return True

    result = _api_call("sendMessage", {
        "chat_id": user_id,
        "text": message,
        "parse_mode": "Markdown",
    })

    return result.get("ok", False)


def send_to_channel(message, chat_id=None):
    """Send a message to the signals channel."""
    cfg = _get_config()
    cid = chat_id or cfg["signals_channel_id"]

    return _api_call("sendMessage", {
        "chat_id": cid,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    })
