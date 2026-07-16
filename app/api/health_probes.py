"""LB probes: /healthz (liveness) and /readyz (readiness).

Readiness = background write-canary result with M-of-N hysteresis
(3 consecutive failures -> not ready, 2 consecutive successes -> ready)
plus any registered role-specific checks. The canary runs on a timer,
NOT per probe request — N replicas probing a slow DB must not amplify
load or flap together. /api/health is unchanged and stays the
compatibility alias. Spec §3.7.
"""

import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter(tags=["probes"])

_FAILS_TO_TRIP = 3
_OKS_TO_RECOVER = 2

# Sentinel (user_id, dataset) pair used to park the write-canary row in the
# existing `user_sync_settings` table via `sync_settings_repo()` — the
# smallest KV-shaped repo already routed through the backend factory
# (src/repositories/__init__.py). Chosen over other candidates because it
# needs no extra config (unlike `system_secrets_repo`, which raises
# VaultKeyNotConfiguredError when AGNES_VAULT_KEY is unset — a false
# readiness failure unrelated to DB health) and doesn't collide with or
# overwrite real operator-facing content (unlike the `instance_templates`
# rows backing claude_md/welcome/news_template). No real user can ever
# authenticate as this sentinel id, so the row never appears in a real
# user's settings.
_CANARY_USER_ID = "__system__"
_CANARY_DATASET = "__readiness_canary__"


class ReadinessState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = True
        self._consec_fail = 0
        self._consec_ok = 0
        self._last_canary_at: str | None = None

    def record_canary(self, ok: bool) -> None:
        with self._lock:
            self._last_canary_at = datetime.now(timezone.utc).isoformat()
            if ok:
                self._consec_ok += 1
                self._consec_fail = 0
                if not self._ready and self._consec_ok >= _OKS_TO_RECOVER:
                    self._ready = True
                    logger.info("readiness: recovered")
            else:
                self._consec_fail += 1
                self._consec_ok = 0
                if self._ready and self._consec_fail >= _FAILS_TO_TRIP:
                    self._ready = False
                    logger.error("readiness: tripped after %d canary failures", self._consec_fail)

    def is_ready(self) -> bool:
        with self._lock:
            return self._ready

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "canary_ready": self._ready,
                "consecutive_failures": self._consec_fail,
                "last_canary_at": self._last_canary_at,
            }


readiness = ReadinessState()
_extra_checks: dict[str, Callable[[], bool]] = {}


def register_readiness_check(name: str, fn: Callable[[], bool]) -> None:
    _extra_checks[name] = fn


def _write_canary() -> bool:
    try:
        # Reuse the existing user_sync_settings KV surface through the repo
        # factory so the write exercises whichever backend (DuckDB or
        # Postgres) is currently active — see module docstring above for
        # why this repo was picked over the other KV-shaped candidates.
        from src.repositories import sync_settings_repo

        sync_settings_repo().set_dataset_enabled(_CANARY_USER_ID, _CANARY_DATASET, True)
        return True
    except Exception:
        logger.exception("readiness write-canary failed")
        return False


async def canary_loop(interval_s: float = 30.0) -> None:
    while True:
        ok = await asyncio.to_thread(_write_canary)
        readiness.record_canary(ok)
        await asyncio.sleep(interval_s)


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "alive"}


@router.get("/readyz")
def readyz():
    failed = [name for name, fn in _extra_checks.items() if not _safe(fn)]
    ok = readiness.is_ready() and not failed
    body = {"status": "ready" if ok else "not_ready", "failed_checks": failed, **readiness.snapshot()}
    return JSONResponse(status_code=200 if ok else 503, content=body)


def _safe(fn: Callable[[], bool]) -> bool:
    try:
        return bool(fn())
    except Exception:
        logger.exception("readiness extra check crashed")
        return False
