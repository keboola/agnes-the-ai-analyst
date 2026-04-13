"""
Telegram Notification Bot - main entry point.

Runs two concurrent tasks:
1. Telegram polling - handles /start commands, generates verification codes
2. HTTP server on unix socket - accepts send requests from notify-runner

Usage:
    python -m telegram_bot.bot
"""

import asyncio
import grp
import json
import logging
import os
import sys
from pathlib import Path

from aiohttp import web

from . import config
from .dispatch import dispatch_to_ws_gateway
from .runner import run_user_script
from .sender import (
    answer_callback_query,
    get_updates,
    send_message,
    send_message_with_buttons,
    send_photo,
)
from .status import get_notification_status, get_script_buttons
from .storage import create_verification_code, get_chat_id, get_username_by_chat_id
from .test_report import generate_test_report

# Load instance branding for bot messages
try:
    from config.loader import load_instance_config, get_instance_value
    _bot_config = load_instance_config()
    _bot_instance_name = get_instance_value(_bot_config, "instance", "name", default="Data Analyst")
    _bot_server_hostname = get_instance_value(_bot_config, "server", "hostname", default="your-server")
    _bot_domain_suffix = get_instance_value(_bot_config, "telegram", "domain_suffix", default="")
except Exception:
    _bot_instance_name = "Data Analyst"
    _bot_server_hostname = "your-server"
    _bot_domain_suffix = ""

# Configure logging
_log_handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
try:
    os.makedirs(os.path.dirname(config.BOT_LOG_FILE), exist_ok=True)
    _log_handlers.append(logging.FileHandler(config.BOT_LOG_FILE, mode="a"))
except OSError:
    pass  # File logging unavailable (e.g., read-only filesystem in CI)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=_log_handlers,
)
logger = logging.getLogger("notify-bot")


# --- Telegram Polling ---

async def handle_message(message: dict) -> None:
    """Handle an incoming Telegram message."""
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()

    if not chat_id:
        return

    if text == "/start":
        username = get_username_by_chat_id(chat_id)
        if username:
            await send_message(
                chat_id,
                f"You are already linked as *{username}*.\n"
                f"Use /help to see available commands.",
            )
            return

        code = create_verification_code(chat_id)
        await send_message(
            chat_id,
            f"Welcome to {_bot_instance_name} Notifications!\n\n"
            f"Your verification code: *{code}*\n\n"
            f"Enter this code on your dashboard at {_bot_server_hostname}\n"
            f"Code expires in 10 minutes.",
        )
        logger.info(f"Sent verification code to chat_id {chat_id}")

    elif text == "/whoami":
        username = get_username_by_chat_id(chat_id)
        if username:
            # Username is derived from email
            if _bot_domain_suffix:
                email = f"{username}@{_bot_domain_suffix}"
            else:
                email = username
            await send_message(
                chat_id,
                f"*{username}*\n{email}",
            )
        else:
            await send_message(
                chat_id,
                "No linked account. Use /start to link.",
                parse_mode="",
            )

    elif text == "/status":
        username = get_username_by_chat_id(chat_id)
        if not username:
            await send_message(
                chat_id,
                "Link your account first using /start and the dashboard.",
                parse_mode="",
            )
            return

        status_text = get_notification_status(username)
        buttons = get_script_buttons(username)
        if buttons:
            await send_message_with_buttons(chat_id, status_text, buttons)
        else:
            await send_message(chat_id, status_text)

    elif text == "/test":
        username = get_username_by_chat_id(chat_id)
        if not username:
            await send_message(
                chat_id,
                "Link your account first using /start and the dashboard.",
                parse_mode="",
            )
            return

        await send_message(chat_id, "Generating test report...", parse_mode="")
        try:
            image_path, caption = generate_test_report(username)
            await send_photo(chat_id, image_path, caption)
            # Cleanup temp file
            os.unlink(image_path)
            logger.info(f"Sent test report to {username}")
        except Exception:
            logger.exception(f"Failed to generate test report for {username}")
            await send_message(chat_id, "Failed to generate report. Check server logs.", parse_mode="")

    elif text == "/help":
        await send_message(
            chat_id,
            f"*{_bot_instance_name} Bot*\n\n"
            "/start - Link your Telegram account\n"
            "/whoami - Show your username and chat ID\n"
            "/status - List your notification scripts\n"
            "/test - Send a demo report\n"
            "/help - Show this help",
        )

    else:
        await send_message(
            chat_id,
            "Unknown command. Type /help for available commands.",
            parse_mode="",
        )


async def handle_callback_query(callback_query: dict) -> None:
    """Handle inline keyboard button press."""
    callback_id = callback_query.get("id")
    chat_id = callback_query.get("message", {}).get("chat", {}).get("id")
    data = callback_query.get("data", "")

    if not chat_id or not data:
        return

    # Parse callback data: "run:{script_name}"
    if not data.startswith("run:"):
        await answer_callback_query(callback_id, "Unknown action")
        return

    script_name = data[4:]  # strip "run:"
    username = get_username_by_chat_id(chat_id)
    if not username:
        await answer_callback_query(callback_id, "Account not linked")
        return

    await answer_callback_query(callback_id, f"Running {script_name}...")
    await send_message(chat_id, f"Running `{script_name}`...", parse_mode="Markdown")

    logger.info(f"On-demand run: {script_name} for {username}")
    output = await asyncio.to_thread(run_user_script, username, script_name)

    if output is None:
        await send_message(chat_id, f"`{script_name}` failed. Check server logs.", parse_mode="Markdown")
        return

    if not output.get("notify", False):
        await send_message(chat_id, f"`{script_name}` returned notify=false (no alert).", parse_mode="Markdown")
        return

    # Format and send the notification
    parts = []
    title = output.get("title", "")
    message_text = output.get("message", "")
    if title:
        parts.append(f"*{title}*")
    if message_text:
        parts.append(message_text)
    text = "\n".join(parts)

    image_path = output.get("image_path", "")
    if image_path and os.path.isfile(image_path):
        await send_photo(chat_id, image_path, caption=text)
    elif text:
        await send_message(chat_id, text)
    else:
        await send_message(chat_id, f"`{script_name}` produced no output.", parse_mode="Markdown")

    # Also dispatch to WebSocket gateway for desktop app
    await asyncio.to_thread(dispatch_to_ws_gateway, username, output, script_name)


async def polling_loop() -> None:
    """Long-poll Telegram for updates."""
    logger.info("Starting Telegram polling loop")
    offset = 0

    while True:
        try:
            updates, offset = await get_updates(offset)
            for update in updates:
                message = update.get("message")
                if message:
                    await handle_message(message)
                callback_query = update.get("callback_query")
                if callback_query:
                    await handle_callback_query(callback_query)
        except Exception:
            logger.exception("Polling loop error")
            await asyncio.sleep(config.POLL_ERROR_RETRY_SECONDS)


# --- HTTP Send API (unix socket) ---

async def handle_send(request: web.Request) -> web.Response:
    """Handle POST /send - send text message."""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    username = data.get("user")
    text = data.get("text")
    parse_mode = data.get("parse_mode", "Markdown")

    if not username or not text:
        return web.json_response(
            {"error": "Missing required fields: user, text"}, status=400
        )

    chat_id = get_chat_id(username)
    if not chat_id:
        return web.json_response(
            {"error": f"User '{username}' has no linked Telegram"}, status=404
        )

    success = await send_message(chat_id, text, parse_mode)
    if success:
        return web.json_response({"ok": True})
    return web.json_response({"error": "Failed to send message"}, status=502)


async def handle_send_photo(request: web.Request) -> web.Response:
    """Handle POST /send_photo - send image with optional caption."""
    try:
        data = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    username = data.get("user")
    photo_path = data.get("photo_path")
    caption = data.get("caption", "")
    parse_mode = data.get("parse_mode", "Markdown")

    if not username or not photo_path:
        return web.json_response(
            {"error": "Missing required fields: user, photo_path"}, status=400
        )

    if not os.path.isfile(photo_path):
        return web.json_response(
            {"error": f"Photo file not found: {photo_path}"}, status=400
        )

    chat_id = get_chat_id(username)
    if not chat_id:
        return web.json_response(
            {"error": f"User '{username}' has no linked Telegram"}, status=404
        )

    success = await send_photo(chat_id, photo_path, caption, parse_mode)
    if success:
        return web.json_response({"ok": True})
    return web.json_response({"error": "Failed to send photo"}, status=502)


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({"status": "ok"})


def create_app() -> web.Application:
    """Create the aiohttp application."""
    app = web.Application()
    app.router.add_post("/send", handle_send)
    app.router.add_post("/send_photo", handle_send_photo)
    app.router.add_get("/health", handle_health)
    return app


async def start_http_server() -> None:
    """Start HTTP server on unix socket."""
    # Remove stale socket
    socket_path = config.SOCKET_PATH
    if os.path.exists(socket_path):
        os.unlink(socket_path)

    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.UnixSite(runner, socket_path)
    await site.start()

    # Set socket permissions: group-writable for dataread group (analysts send via notify-runner)
    # Socket lives in /run/notify-bot/ (systemd RuntimeDirectory, mode 0755)
    os.chmod(socket_path, 0o660)
    # Change group ownership to dataread (deploy user is member of dataread group)
    os.chown(socket_path, -1, grp.getgrnam('dataread').gr_gid)

    logger.info(f"HTTP server listening on {socket_path}")


# --- Main ---

async def main() -> None:
    """Run bot polling and HTTP server concurrently."""
    if not config.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set. Exiting.")
        sys.exit(1)

    # Ensure notifications directory exists
    os.makedirs(config.NOTIFICATIONS_DIR, exist_ok=True)

    await start_http_server()
    await polling_loop()


if __name__ == "__main__":
    asyncio.run(main())
