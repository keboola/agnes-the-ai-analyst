"""Scheduler service — replaces systemd timers.

Lightweight sidecar that triggers jobs by calling the main app's API.
Keeps all business logic in the main app.

Usage: python -m services.scheduler
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

import httpx

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [scheduler] %(message)s",
)
logger = logging.getLogger(__name__)

API_URL = os.environ.get("API_URL", "http://localhost:8000")
SCHEDULER_API_TOKEN = os.environ.get("SCHEDULER_API_TOKEN", "")

# Schedule definitions: (name, interval_seconds, api_endpoint, http_method)
JOBS = [
    ("data-refresh", 15 * 60, "/api/sync/trigger", "POST"),
    ("health-check", 5 * 60, "/api/health", "GET"),
]

_running = True


def _signal_handler(sig, frame):
    global _running
    logger.info(f"Received signal {sig}, shutting down...")
    _running = False


def _call_api(endpoint: str, method: str = "POST") -> bool:
    """Call the main app API. Returns True on success."""
    url = f"{API_URL}{endpoint}"
    headers = {}
    if SCHEDULER_API_TOKEN:
        headers["Authorization"] = f"Bearer {SCHEDULER_API_TOKEN}"
    try:
        if method == "POST":
            resp = httpx.post(url, headers=headers, timeout=120)
        else:
            resp = httpx.get(url, headers=headers, timeout=30)
        if resp.status_code < 400:
            logger.info(f"Job {endpoint}: {resp.status_code}")
            return True
        else:
            logger.warning(f"Job {endpoint}: HTTP {resp.status_code} - {resp.text[:200]}")
            return False
    except Exception as e:
        logger.error(f"Job {endpoint} failed: {e}")
        return False


def run():
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    logger.info(f"Scheduler started. API_URL={API_URL}, {len(JOBS)} jobs configured.")

    # Track last run time per job
    last_run = {name: 0.0 for name, _, _, _ in JOBS}

    while _running:
        now = time.time()
        for name, interval, endpoint, method in JOBS:
            if now - last_run[name] >= interval:
                logger.info(f"Running job: {name}")
                _call_api(endpoint, method)
                last_run[name] = now
        time.sleep(10)  # check every 10 seconds

    logger.info("Scheduler stopped.")


if __name__ == "__main__":
    run()
