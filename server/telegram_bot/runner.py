"""
Script runner for Telegram bot - executes user notification scripts on demand.

Used by callback query handler when user clicks "Run" button in /status.
Runs the script as the owning user via the notify-scripts helper.
"""

import json
import logging
import subprocess

from . import config

logger = logging.getLogger(__name__)

NOTIFY_SCRIPTS_BIN = "/usr/local/bin/notify-scripts"


def run_user_script(username: str, script_name: str) -> dict | None:
    """Run a notification script as the specified user and return parsed JSON output.

    Returns None on error, or the parsed JSON dict on success.
    """
    if not script_name.endswith(".py"):
        logger.warning(f"Not a Python script: {script_name}")
        return None

    try:
        result = subprocess.run(
            ["/usr/bin/sudo", "-u", username, NOTIFY_SCRIPTS_BIN, "run", script_name],
            capture_output=True,
            text=True,
            timeout=config.SCRIPT_TIMEOUT_SECONDS + 10,  # extra margin over inner timeout
        )

        if result.returncode != 0:
            # notify-scripts prints JSON error to stdout on failure
            try:
                error_info = json.loads(result.stdout)
                logger.warning(
                    f"Script {script_name} (user={username}) failed: "
                    f"{error_info.get('error', 'unknown')}"
                )
            except (json.JSONDecodeError, Exception):
                logger.warning(
                    f"Script {script_name} (user={username}) exited with code "
                    f"{result.returncode}: {result.stderr[:500]}"
                )
            return None

        stdout = result.stdout.strip()
        if not stdout:
            logger.warning(f"Script {script_name} produced no stdout")
            return None

        parsed = json.loads(stdout)
        logger.info(
            f"Script {script_name} output: "
            f"image_path={parsed.get('image_path', 'MISSING')}"
        )
        return parsed

    except subprocess.TimeoutExpired:
        logger.error(
            f"Script {script_name} timed out after {config.SCRIPT_TIMEOUT_SECONDS}s"
        )
        return None
    except json.JSONDecodeError as e:
        logger.error(f"Script {script_name} returned invalid JSON: {e}")
        return None
    except Exception:
        logger.exception(f"Error running script {script_name} for user {username}")
        return None
