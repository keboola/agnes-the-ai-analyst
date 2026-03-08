"""
Account details service for the dashboard widget.

Gathers notification scripts, cron schedule, and last sync info
for a user's account card on the dashboard.
"""

import json
import logging
import re
import subprocess

logger = logging.getLogger(__name__)

NOTIFY_SCRIPTS_BIN = "/usr/local/bin/notify-scripts"
USER_CRONTAB_BIN = "/usr/local/bin/user-crontab"

def _load_username_mapping():
    """Load username mapping from instance config."""
    try:
        from config.loader import load_instance_config, get_instance_value
        config = load_instance_config()
        return get_instance_value(config, "username_mapping", default={})
    except Exception:
        return {}


WEBAPP_TO_SERVER_USERNAME = _load_username_mapping()

SUBPROCESS_TIMEOUT_SECONDS = 10

# Username validation: only allow safe characters (same pattern as user_service.py)
USERNAME_RE = re.compile(r"^[a-z][a-z0-9._-]{0,31}$")


def get_account_details(username: str) -> dict | None:
    """Gather account widget data for dashboard display.

    Args:
        username: webapp-style username (email-derived)

    Returns:
        dict with notification_scripts, script_count, cron_schedule,
        last_sync_display, sync_datasets_enabled - or None on invalid user.
    """
    if not username or not USERNAME_RE.match(username):
        return None

    server_user = _get_server_username(username)

    scripts = _get_notification_scripts(server_user)
    cron_schedule = _get_cron_schedule(server_user)
    last_sync = _get_last_sync(server_user)

    # Enabled optional datasets from sync_settings
    sync_datasets = _get_enabled_datasets(username)

    return {
        "notification_scripts": scripts,
        "script_count": len(scripts),
        "cron_schedule": cron_schedule,
        "last_sync_display": last_sync,
        "sync_datasets_enabled": sync_datasets,
    }


def _get_server_username(webapp_username: str) -> str:
    """Map webapp username (email-derived) to server home directory name."""
    return WEBAPP_TO_SERVER_USERNAME.get(webapp_username, webapp_username)


def _get_notification_scripts(server_user: str) -> list[dict]:
    """Fetch notification scripts list via notify-scripts helper."""
    try:
        result = subprocess.run(
            ["/usr/bin/sudo", "-u", server_user, NOTIFY_SCRIPTS_BIN, "list"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            logger.warning(
                "notify-scripts list failed for %s: %s", server_user, result.stderr[:300]
            )
            return []
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        logger.error("notify-scripts list timed out for %s", server_user)
        return []
    except (json.JSONDecodeError, Exception) as e:
        logger.error("notify-scripts list error for %s: %s", server_user, e)
        return []


def _get_cron_schedule(server_user: str) -> str | None:
    """Read user's crontab and extract the schedule expression.

    Returns human-readable schedule string or None if no crontab.
    """
    try:
        result = subprocess.run(
            ["/usr/bin/sudo", "-u", server_user, USER_CRONTAB_BIN],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            # returncode 1 = "no crontab for user" - expected case
            return None
        return _parse_cron_schedule(result.stdout)
    except subprocess.TimeoutExpired:
        logger.error("user-crontab timed out for %s", server_user)
        return None
    except Exception as e:
        logger.error("user-crontab error for %s: %s", server_user, e)
        return None


def _parse_cron_schedule(crontab_output: str) -> str | None:
    """Extract first cron expression from crontab output and humanize it."""
    for line in crontab_output.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Cron lines have 5 time fields + command
        parts = line.split()
        if len(parts) >= 6:
            cron_expr = " ".join(parts[:5])
            return _humanize_cron(cron_expr)
    return None


def _humanize_cron(expr: str) -> str:
    """Convert a cron expression to a human-readable string.

    Handles common patterns. Falls back to the raw expression for
    anything unusual.
    """
    parts = expr.split()
    if len(parts) != 5:
        return expr

    minute, hour, dom, month, dow = parts

    # Every N minutes: */N * * * *
    if hour == "*" and dom == "*" and month == "*" and dow == "*":
        if minute == "*":
            return "Every minute"
        m = re.match(r"^\*/(\d+)$", minute)
        if m:
            n = int(m.group(1))
            if n == 1:
                return "Every minute"
            return f"Every {n} minutes"
        # Specific minute: e.g. "30 * * * *"
        if minute.isdigit():
            return "Every hour"

    # Every N hours: 0 */N * * *
    if minute == "0" and dom == "*" and month == "*" and dow == "*":
        m = re.match(r"^\*/(\d+)$", hour)
        if m:
            n = int(m.group(1))
            if n == 1:
                return "Every hour"
            return f"Every {n} hours"

    # Daily: 0 H * * *
    if dom == "*" and month == "*" and dow == "*" and minute.isdigit() and hour.isdigit():
        return f"Daily at {int(hour):02d}:{int(minute):02d}"

    # Fallback - show raw expression
    return expr


def _get_last_sync(server_user: str) -> str | None:
    """Get last sync time via notify-scripts sync-status."""
    try:
        result = subprocess.run(
            ["/usr/bin/sudo", "-u", server_user, NOTIFY_SCRIPTS_BIN, "sync-status"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if data.get("synced"):
            return data.get("elapsed_display")
        return None
    except subprocess.TimeoutExpired:
        logger.error("notify-scripts sync-status timed out for %s", server_user)
        return None
    except (json.JSONDecodeError, Exception) as e:
        logger.error("notify-scripts sync-status error for %s: %s", server_user, e)
        return None


def _get_enabled_datasets(webapp_username: str) -> list[str]:
    """Get list of enabled optional datasets from sync settings."""
    try:
        from .sync_settings_service import get_sync_settings
        settings = get_sync_settings(webapp_username)
        datasets = settings.get("datasets", {})
        return [name for name, enabled in datasets.items() if enabled]
    except Exception as e:
        logger.error("Failed to read sync settings for %s: %s", webapp_username, e)
        return []
