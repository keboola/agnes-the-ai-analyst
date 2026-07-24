"""
Configuration for the Telegram notification bot.

All values loaded from environment variables - no hardcoded defaults for secrets.
"""

import os


# Telegram Bot API token (required)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Paths
DATA_DIR = os.environ.get("DATA_DIR", "/data")
NOTIFICATIONS_DIR = os.path.join(DATA_DIR, "notifications")
TELEGRAM_USERS_FILE = os.path.join(NOTIFICATIONS_DIR, "telegram_users.json")
PENDING_CODES_FILE = os.path.join(NOTIFICATIONS_DIR, "pending_codes.json")

# Security audit F15: the /send_photo unix-socket handler used to open ANY
# caller-supplied path and deliver its bytes to a linked Telegram user, giving
# any process in the socket's group an arbitrary-file-read + exfil primitive.
# Photos it may send must live under this directory (report images written by
# the bot / notify-runner). Override with AGNES_TELEGRAM_PHOTO_DIR.
PHOTO_BASE_DIR = os.environ.get("AGNES_TELEGRAM_PHOTO_DIR", DATA_DIR)

# Unix socket for internal send API (in /run/notify-bot/, managed by systemd RuntimeDirectory)
SOCKET_PATH = "/run/notify-bot/bot.sock"

# Verification code settings
CODE_LENGTH = 6
CODE_TTL_SECONDS = 600  # 10 minutes

# Telegram polling
POLL_TIMEOUT_SECONDS = 30
POLL_ERROR_RETRY_SECONDS = 5

# Send API
MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024

# Script execution (for /status run buttons)
SCRIPT_TIMEOUT_SECONDS = 60
