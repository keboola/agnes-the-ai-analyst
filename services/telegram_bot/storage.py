"""
JSON file storage for Telegram user mappings and pending verification codes.

Thread-safe file operations with atomic writes.
"""

import json
import logging
import os
import random
import string
import tempfile
import time

from . import config

logger = logging.getLogger(__name__)


def _read_json(path: str) -> dict:
    """Read a JSON file, return empty dict if not found or invalid."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_json(path: str, data: dict) -> None:
    """Atomically write JSON data to file."""
    dir_path = os.path.dirname(path)
    os.makedirs(dir_path, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.chmod(tmp_path, 0o660)  # group-readable for data-ops (webapp)
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise


# --- Telegram Users ---


def get_chat_id(username: str) -> int | None:
    """Get Telegram chat_id for a linked username."""
    users = _read_json(config.TELEGRAM_USERS_FILE)
    entry = users.get(username)
    if entry:
        return entry.get("chat_id")
    return None


def get_username_by_chat_id(chat_id: int) -> str | None:
    """Reverse lookup: get username for a Telegram chat_id."""
    users = _read_json(config.TELEGRAM_USERS_FILE)
    for username, entry in users.items():
        if entry.get("chat_id") == chat_id:
            return username
    return None


def link_user(username: str, chat_id: int) -> None:
    """Link a username to a Telegram chat_id."""
    users = _read_json(config.TELEGRAM_USERS_FILE)
    users[username] = {
        "chat_id": chat_id,
        "linked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_json(config.TELEGRAM_USERS_FILE, users)
    logger.info(f"Linked user '{username}' to chat_id {chat_id}")


def unlink_user(username: str) -> bool:
    """Unlink a username from Telegram. Returns True if was linked."""
    users = _read_json(config.TELEGRAM_USERS_FILE)
    if username in users:
        del users[username]
        _write_json(config.TELEGRAM_USERS_FILE, users)
        logger.info(f"Unlinked user '{username}'")
        return True
    return False


def get_user_status(username: str) -> dict | None:
    """Get link status for a username. Returns dict with linked_at or None."""
    users = _read_json(config.TELEGRAM_USERS_FILE)
    return users.get(username)


# --- Verification Codes ---


def _generate_code() -> str:
    """Generate a random numeric verification code."""
    return "".join(random.choices(string.digits, k=config.CODE_LENGTH))


def _cleanup_expired(codes: dict) -> dict:
    """Remove expired codes."""
    now = time.time()
    return {code: data for code, data in codes.items() if now - data.get("created_at", 0) < config.CODE_TTL_SECONDS}


def create_verification_code(chat_id: int) -> str:
    """Create a new verification code for a Telegram chat_id."""
    codes = _read_json(config.PENDING_CODES_FILE)
    codes = _cleanup_expired(codes)

    # Remove any existing code for this chat_id
    codes = {code: data for code, data in codes.items() if data.get("chat_id") != chat_id}

    code = _generate_code()
    # Ensure uniqueness
    while code in codes:
        code = _generate_code()

    codes[code] = {
        "chat_id": chat_id,
        "created_at": time.time(),
    }
    _write_json(config.PENDING_CODES_FILE, codes)
    logger.info(f"Created verification code for chat_id {chat_id}")
    return code


def verify_code(code: str) -> int | None:
    """Verify a code and return the chat_id if valid. Consumes the code."""
    codes = _read_json(config.PENDING_CODES_FILE)
    codes = _cleanup_expired(codes)

    data = codes.get(code)
    if data is None:
        return None

    chat_id = data["chat_id"]

    # Consume the code
    del codes[code]
    _write_json(config.PENDING_CODES_FILE, codes)

    logger.info(f"Verified code for chat_id {chat_id}")
    return chat_id
