"""
Configuration for the Telegram notification bot.

All values loaded from environment variables - no hardcoded defaults for secrets.
"""

import os
import tempfile


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
# A photo it may send must live under one of these base dirs. The default
# covers where images actually get generated today: the system temp dir (report
# generators use ``tempfile.gettempdir()`` — see test_report.py) and DATA_DIR
# (notifications output). Containment is realpath-based (symlink-safe), so this
# still blocks /etc, ~/.ssh, and other arbitrary absolute paths.
#
# Operators override with AGNES_TELEGRAM_PHOTO_DIR — an os.pathsep-separated
# (``:`` on POSIX) list of allowed base dirs — when images live elsewhere (a
# mounted output volume, a RuntimeDirectory under /run, …).
_photo_dirs_env = os.environ.get("AGNES_TELEGRAM_PHOTO_DIR", "").strip()
if _photo_dirs_env:
    PHOTO_BASE_DIRS = [d for d in _photo_dirs_env.split(os.pathsep) if d]
else:
    PHOTO_BASE_DIRS = [tempfile.gettempdir(), DATA_DIR]

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
