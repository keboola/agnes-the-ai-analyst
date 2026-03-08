"""
Telegram Bot API sender - sends messages and photos via HTTP API.
"""

import logging

import httpx

from . import config

logger = logging.getLogger(__name__)

BASE_URL = "https://api.telegram.org/bot{token}"


def _api_url(method: str) -> str:
    """Build Telegram Bot API URL."""
    return f"{BASE_URL.format(token=config.TELEGRAM_BOT_TOKEN)}/{method}"


async def send_message(
    chat_id: int,
    text: str,
    parse_mode: str = "Markdown",
) -> bool:
    """Send a text message to a Telegram chat. Returns True on success."""
    # Truncate if too long
    if len(text) > config.MAX_MESSAGE_LENGTH:
        text = text[: config.MAX_MESSAGE_LENGTH - 3] + "..."

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                _api_url("sendMessage"),
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                },
            )
            if resp.status_code == 200 and resp.json().get("ok"):
                return True

            logger.error(f"sendMessage failed: {resp.status_code} {resp.text}")
            # Retry without parse_mode if Markdown parsing failed
            if resp.status_code == 400 and "parse" in resp.text.lower():
                resp2 = await client.post(
                    _api_url("sendMessage"),
                    json={"chat_id": chat_id, "text": text},
                )
                return resp2.status_code == 200 and resp2.json().get("ok")
            return False
    except Exception:
        logger.exception(f"Failed to send message to chat_id {chat_id}")
        return False


async def send_message_with_buttons(
    chat_id: int,
    text: str,
    buttons: list[list[dict]],
    parse_mode: str = "Markdown",
) -> bool:
    """Send a text message with inline keyboard buttons. Returns True on success.

    buttons format: [[{"text": "Label", "callback_data": "data"}], ...]
    Each inner list is a row of buttons.
    """
    if len(text) > config.MAX_MESSAGE_LENGTH:
        text = text[: config.MAX_MESSAGE_LENGTH - 3] + "..."

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                _api_url("sendMessage"),
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "reply_markup": {"inline_keyboard": buttons},
                },
            )
            if resp.status_code == 200 and resp.json().get("ok"):
                return True
            logger.error(f"sendMessage (buttons) failed: {resp.status_code} {resp.text}")
            return False
    except Exception:
        logger.exception(f"Failed to send message with buttons to chat_id {chat_id}")
        return False


async def answer_callback_query(
    callback_query_id: str,
    text: str = "",
) -> bool:
    """Answer a callback query (acknowledge button press)."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                _api_url("answerCallbackQuery"),
                json={
                    "callback_query_id": callback_query_id,
                    "text": text,
                },
            )
            return resp.status_code == 200
    except Exception:
        logger.exception("Failed to answer callback query")
        return False


async def send_photo(
    chat_id: int,
    photo_path: str,
    caption: str = "",
    parse_mode: str = "Markdown",
) -> bool:
    """Send a photo to a Telegram chat. Returns True on success."""
    if caption and len(caption) > config.MAX_CAPTION_LENGTH:
        caption = caption[: config.MAX_CAPTION_LENGTH - 3] + "..."

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            with open(photo_path, "rb") as photo_file:
                data = {"chat_id": str(chat_id)}
                if caption:
                    data["caption"] = caption
                    data["parse_mode"] = parse_mode
                resp = await client.post(
                    _api_url("sendPhoto"),
                    data=data,
                    files={"photo": ("chart.png", photo_file, "image/png")},
                )
            if resp.status_code == 200 and resp.json().get("ok"):
                return True

            logger.error(f"sendPhoto failed: {resp.status_code} {resp.text}")
            return False
    except Exception:
        logger.exception(f"Failed to send photo to chat_id {chat_id}")
        return False


async def get_updates(offset: int = 0) -> tuple[list[dict], int]:
    """Long-poll for updates. Returns (updates, new_offset)."""
    try:
        async with httpx.AsyncClient(timeout=config.POLL_TIMEOUT_SECONDS + 10) as client:
            resp = await client.get(
                _api_url("getUpdates"),
                params={
                    "offset": offset,
                    "timeout": config.POLL_TIMEOUT_SECONDS,
                    "allowed_updates": '["message","callback_query"]',
                },
            )
            if resp.status_code != 200:
                logger.error(f"getUpdates failed: {resp.status_code}")
                return [], offset

            data = resp.json()
            if not data.get("ok"):
                return [], offset

            updates = data.get("result", [])
            if updates:
                new_offset = updates[-1]["update_id"] + 1
                return updates, new_offset
            return [], offset
    except httpx.TimeoutException:
        return [], offset
    except Exception:
        logger.exception("getUpdates error")
        return [], offset
