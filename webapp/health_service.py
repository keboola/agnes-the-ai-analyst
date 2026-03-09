"""
Health check service for monitoring.

Returns detailed system status including:
- Systemd services (webapp, telegram-bot, timers)
- Disk space
- System load
- Optional: Jira webhook timestamp (if Jira connector enabled)
"""

import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path

from .config import Config

logger = logging.getLogger(__name__)

# Services to monitor
CRITICAL_SERVICES = [
    "webapp.service",
    "notify-bot.service",
]

# Base timers (always monitored)
_BASE_TIMERS = [
    "corporate-memory.timer",
]

# Jira timers (only if Jira connector is enabled)
_JIRA_TIMERS = [
    "jira-consistency.timer",
    "jira-sla-poll.timer",
]

TIMERS_TO_MONITOR = _BASE_TIMERS + (_JIRA_TIMERS if Config.JIRA_ENABLED else [])


def get_service_status(service_name: str) -> dict:
    """Get systemd service status."""
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", service_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return {
            "name": service_name,
            "status": result.stdout.strip(),
            "healthy": result.returncode == 0,
        }
    except Exception as e:
        logger.warning(f"Failed to check {service_name}: {e}")
        return {
            "name": service_name,
            "status": "unknown",
            "healthy": False,
            "error": str(e),
        }


def get_disk_usage() -> list:
    """Get disk usage for all important partitions."""
    partitions = ["/", "/data", "/home", "/tmp"]
    results = []

    for partition in partitions:
        try:
            stat = os.statvfs(partition)
            total = stat.f_blocks * stat.f_frsize
            free = stat.f_bavail * stat.f_frsize
            used = total - free
            percent = (used / total) * 100 if total > 0 else 0

            results.append({
                "partition": partition,
                "used_percent": round(percent, 1),
                "free_gb": round(free / (1024**3), 2),
                "total_gb": round(total / (1024**3), 2),
                "healthy": percent < 90,
            })
        except Exception as e:
            logger.warning(f"Failed to get disk usage for {partition}: {e}")
            results.append({
                "partition": partition,
                "healthy": False,
                "error": str(e)
            })

    return results


def get_load_average() -> dict:
    """Get system load average."""
    try:
        load1, load5, load15 = os.getloadavg()
        # e2-medium has 2 CPUs, so load > 4 is concerning
        return {
            "load_1min": round(load1, 2),
            "load_5min": round(load5, 2),
            "load_15min": round(load15, 2),
            "healthy": load1 < 4 and load5 < 4,
        }
    except Exception as e:
        logger.warning(f"Failed to get load average: {e}")
        return {"healthy": False, "error": str(e)}


def get_last_jira_webhook() -> dict:
    """Get timestamp of last Jira webhook received."""
    try:
        # Check recent raw Jira files
        jira_dir = Path("/data/src_data/raw/jira/issues")
        if not jira_dir.exists():
            return {"healthy": True, "message": "Jira directory not found"}

        # Get most recently modified file
        files = list(jira_dir.glob("*.json"))
        if not files:
            return {"healthy": True, "message": "No Jira files yet"}

        latest_file = max(files, key=lambda p: p.stat().st_mtime)
        mtime = latest_file.stat().st_mtime
        age_seconds = datetime.now().timestamp() - mtime
        age_hours = age_seconds / 3600

        return {
            "last_webhook_file": latest_file.name,
            "last_webhook_hours_ago": round(age_hours, 1),
            "healthy": age_hours < 48,  # Alert if no webhook in 48h
        }
    except Exception as e:
        logger.warning(f"Failed to check Jira webhooks: {e}")
        return {"healthy": True, "error": str(e)}  # Non-critical


def health_check() -> tuple[dict, int]:
    """
    Perform comprehensive health check.

    Returns:
        (response_dict, http_status_code)
    """
    services = [get_service_status(s) for s in CRITICAL_SERVICES]
    timers = [get_service_status(t) for t in TIMERS_TO_MONITOR]
    disk = get_disk_usage()
    load = get_load_average()

    # Overall health: all critical checks must pass
    all_healthy = (
        all(s["healthy"] for s in services)
        and all(d["healthy"] for d in disk)
        and load["healthy"]
    )

    response = {
        "status": "healthy" if all_healthy else "degraded",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "services": services,
        "timers": timers,
        "disk": disk,
        "load": load,
    }

    # Include Jira webhook status only if connector is enabled
    if Config.JIRA_ENABLED:
        response["jira_webhook"] = get_last_jira_webhook()

    # Return 200 if healthy, 503 if degraded
    status_code = 200 if all_healthy else 503

    return response, status_code
