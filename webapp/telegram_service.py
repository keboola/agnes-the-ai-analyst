"""
Telegram service for the webapp.

Reads/writes shared JSON files in /data/notifications/ to manage
user-to-Telegram mappings and verify codes.
"""

import json
import logging
import os
import time

logger = logging.getLogger(__name__)

NOTIFICATIONS_DIR = "/data/notifications"
TELEGRAM_USERS_FILE = os.path.join(NOTIFICATIONS_DIR, "telegram_users.json")
PENDING_CODES_FILE = os.path.join(NOTIFICATIONS_DIR, "pending_codes.json")
CODE_TTL_SECONDS = 600  # 10 minutes


def _read_json(path: str) -> dict:
    """Read a JSON file, return empty dict if not found or invalid."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_json(path: str, data: dict) -> None:
    """Write JSON data to file."""
    import tempfile

    dir_path = os.path.dirname(path)
    os.makedirs(dir_path, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp_path, 0o660)  # group-readable for data-ops
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise


def get_telegram_status(username: str) -> dict:
    """Get Telegram link status for a user."""
    users = _read_json(TELEGRAM_USERS_FILE)
    entry = users.get(username)
    if entry:
        return {
            "linked": True,
            "linked_at": entry.get("linked_at", ""),
        }
    return {"linked": False}


def link_telegram(username: str, code: str) -> tuple[bool, str]:
    """Verify a code and link the user's Telegram account.

    Returns (success, message).
    """
    codes = _read_json(PENDING_CODES_FILE)

    # Cleanup expired codes
    now = time.time()
    codes = {
        c: data
        for c, data in codes.items()
        if now - data.get("created_at", 0) < CODE_TTL_SECONDS
    }

    data = codes.get(code)
    if data is None:
        return False, "Invalid or expired verification code."

    chat_id = data["chat_id"]

    # Consume the code
    del codes[code]
    _write_json(PENDING_CODES_FILE, codes)

    # Link user
    users = _read_json(TELEGRAM_USERS_FILE)
    users[username] = {
        "chat_id": chat_id,
        "linked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_json(TELEGRAM_USERS_FILE, users)

    logger.info(f"Linked user '{username}' to Telegram chat_id {chat_id}")
    return True, "Telegram linked successfully."


def unlink_telegram(username: str) -> tuple[bool, str]:
    """Unlink Telegram from a user account.

    Returns (success, message).
    """
    users = _read_json(TELEGRAM_USERS_FILE)
    if username not in users:
        return False, "Telegram is not linked."

    del users[username]
    _write_json(TELEGRAM_USERS_FILE, users)

    logger.info(f"Unlinked Telegram for user '{username}'")
    return True, "Telegram unlinked."
