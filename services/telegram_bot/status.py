"""
Notification status reporting for Telegram bot /status command.

Uses the notify-scripts helper (via sudo -u) to read user's notification
scripts and cooldown state without needing direct filesystem access to
user home directories.
"""

import json
import logging
import subprocess

logger = logging.getLogger(__name__)

NOTIFY_SCRIPTS_BIN = "/usr/local/bin/notify-scripts"


def get_notification_status(username: str) -> str:
    """Build a status message listing user's notification scripts and their state."""
    scripts = _fetch_script_list(username)

    if scripts is None:
        return "Failed to read notification scripts."

    if not scripts:
        return "No notification scripts found.\nAdd `.py` scripts to `~/user/notifications/`."

    lines = [f"*Notifications for {username}*\n"]

    for s in scripts:
        last_run = s.get("last_run")
        info = f"- last sent {last_run}" if last_run else "- never sent"
        lines.append(f"- `{s['name']}` {info}")

    lines.append(f"\n{len(scripts)} script(s) in `~/user/notifications/`")
    return "\n".join(lines)


def get_script_buttons(username: str) -> list[list[dict]]:
    """Build inline keyboard buttons for running each notification script.

    Returns list of button rows: [[{"text": "...", "callback_data": "run:script.py"}], ...]
    """
    scripts = _fetch_script_list(username)
    if not scripts:
        return []

    buttons = []
    for s in scripts:
        buttons.append(
            [
                {
                    "text": f"Run {s['stem']}",
                    "callback_data": f"run:{s['name']}",
                }
            ]
        )

    return buttons


def get_script_list_structured(username: str) -> list[dict]:
    """Return structured list of user notification scripts for API responses."""
    scripts = _fetch_script_list(username)
    if scripts is None:
        return []
    return scripts


def _fetch_script_list(username: str) -> list[dict] | None:
    """Call notify-scripts list as the target user and return parsed JSON.

    Returns None on error, empty list if no scripts, or list of dicts.
    """
    try:
        result = subprocess.run(
            ["/usr/bin/sudo", "-u", username, NOTIFY_SCRIPTS_BIN, "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning(f"notify-scripts list failed for {username}: {result.stderr[:300]}")
            return None
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        logger.error(f"notify-scripts list timed out for {username}")
        return None
    except (json.JSONDecodeError, Exception) as e:
        logger.error(f"notify-scripts list error for {username}: {e}")
        return None
