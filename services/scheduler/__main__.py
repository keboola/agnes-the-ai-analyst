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

_token_warning_emitted = False


def _get_auth_token() -> str:
    """Return the bearer token for API calls.

    Production: ``SCHEDULER_API_TOKEN`` env var carries a long-lived PAT
    minted via ``/tokens`` for a service-account user with the roles the
    jobs need (typically ``core.admin`` for sync triggers). Set it.

    Dev / LOCAL_DEV_MODE: leave it unset. The scheduler returns the empty
    string and calls the API without an ``Authorization`` header — the
    API's dev-bypass auto-authenticates the request as the dev user.

    The previous implementation tried to auto-fetch a token by POSTing to
    ``/auth/token`` with just the seed admin's email. That endpoint
    requires email + password (or rejects external-auth accounts that
    have no local password), so the call always 401-ed and the scheduler
    log was noisy with one access-log line per cron tick. Removed in
    favor of explicit configuration: either set the PAT or rely on
    LOCAL_DEV_MODE.
    """
    global _token_warning_emitted
    if SCHEDULER_API_TOKEN:
        return SCHEDULER_API_TOKEN
    if not _token_warning_emitted:
        logger.warning(
            "SCHEDULER_API_TOKEN is not set — calling the API without "
            "Authorization. Required in production; in LOCAL_DEV_MODE "
            "the dev-bypass auto-authenticates and this is fine."
        )
        _token_warning_emitted = True
    return ""

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
    token = _get_auth_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
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
