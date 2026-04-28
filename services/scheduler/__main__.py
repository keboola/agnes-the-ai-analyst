"""Scheduler service — replaces systemd timers.

Lightweight sidecar that fires scheduled jobs. Two job kinds:
  - "http": POST/GET an endpoint on the main app (e.g. data-refresh).
  - "fn":   call a Python function in-process (e.g. marketplaces sync).

Schedules are strings parsed by src.scheduler.is_table_due — accepts
"every 15m", "every 1h", "daily 03:00", "daily 07:00,13:00".

Usage: python -m services.scheduler
"""

import logging
import os
import signal
import time
from datetime import datetime, timezone

import httpx

from src.scheduler import is_table_due

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


def _marketplaces_job():
    """Entry point for the nightly marketplaces sync.

    Imported lazily so the scheduler container still starts even if the
    module has an import-time issue in development — a failure here only
    kills one job, not the whole loop.
    """
    from src.marketplace import sync_marketplaces
    return sync_marketplaces()


# Schedule definitions: (name, schedule_string, kind, target)
#   kind = "http"  -> target = (endpoint, method)
#   kind = "fn"    -> target = callable_returning_any
JOBS = [
    ("data-refresh",    "every 15m",   "http", ("/api/sync/trigger", "POST")),
    ("health-check",    "every 5m",    "http", ("/api/health",       "GET")),
    ("marketplaces",    "daily 03:00", "fn",   _marketplaces_job),
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


def _call_fn(label: str, fn) -> bool:
    """Run an in-process callable. Returns True on success."""
    try:
        result = fn()
        logger.info("Job %s OK: %s", label, result)
        return True
    except Exception as e:
        logger.error("Job %s failed: %s", label, e)
        return False


def run():
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    logger.info(f"Scheduler started. API_URL={API_URL}, {len(JOBS)} jobs configured.")

    # Track last successful run per job as ISO string — matches what
    # src.scheduler.is_table_due expects.
    last_run: dict[str, str | None] = {name: None for name, *_ in JOBS}

    while _running:
        now_iso = datetime.now(timezone.utc).isoformat()
        for name, schedule, kind, target in JOBS:
            if not is_table_due(schedule, last_run[name]):
                continue
            logger.info("Running job: %s (%s)", name, schedule)
            if kind == "http":
                endpoint, method = target
                ok = _call_api(endpoint, method)
            elif kind == "fn":
                ok = _call_fn(name, target)
            else:
                logger.error("Unknown job kind %r for %s", kind, name)
                ok = False
            if ok:
                last_run[name] = now_iso
        # 30s tick is plenty: interval jobs have minute-level resolution,
        # daily jobs have a ~24 h retry window.
        time.sleep(30)

    logger.info("Scheduler stopped.")


if __name__ == "__main__":
    run()
