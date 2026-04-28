"""Process-local quota tracker for /api/v2/scan (spec §3.8).

In-memory only. Multi-replica deployments effectively multiply caps by N
(documented caveat — see spec §9.4). Future v2 should move to durable
storage if horizontal scale is needed.
"""

from __future__ import annotations
import contextlib
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator

logger = logging.getLogger(__name__)

KIND_CONCURRENT = "concurrent_scans"
KIND_DAILY_BYTES = "daily_bytes"


@dataclass
class QuotaExceededError(Exception):
    kind: str
    current: int
    limit: int
    retry_after_seconds: int = 0

    def __str__(self) -> str:
        return f"{self.kind}: {self.current}/{self.limit}"


def _utcnow() -> datetime:  # patched in tests
    return datetime.now(timezone.utc)


def _utc_today() -> str:
    """ISO date string in UTC, used as the daily-bucket key."""
    return _utcnow().strftime("%Y-%m-%d")


def _seconds_until_utc_midnight() -> int:
    now = _utcnow()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_midnight = midnight + timedelta(days=1)
    return int((next_midnight - now).total_seconds())


class QuotaTracker:
    """Thread-safe quota state. Caller wraps each request in `with q.acquire(user)`,
    and after the BQ result lands records bytes via `record_bytes(user, n)`.
    """

    def __init__(self, *, max_concurrent_per_user: int, max_daily_bytes_per_user: int):
        self._max_concurrent = max_concurrent_per_user
        self._max_daily_bytes = max_daily_bytes_per_user
        self._lock = threading.Lock()
        # state: { user_id: { "concurrent": int, "bucket_day": "YYYY-MM-DD", "bytes": int } }
        self._state: dict[str, dict] = {}

    def _ensure_bucket(self, user: str) -> dict:
        today = _utc_today()
        s = self._state.setdefault(user, {"concurrent": 0, "bucket_day": today, "bytes": 0})
        if s["bucket_day"] != today:
            s["bucket_day"] = today
            s["bytes"] = 0
        return s

    @contextlib.contextmanager
    def acquire(self, user: str) -> Iterator[None]:
        with self._lock:
            s = self._ensure_bucket(user)
            if s["concurrent"] >= self._max_concurrent:
                raise QuotaExceededError(
                    kind=KIND_CONCURRENT,
                    current=s["concurrent"],
                    limit=self._max_concurrent,
                )
            s["concurrent"] += 1
        try:
            yield
        finally:
            with self._lock:
                s = self._ensure_bucket(user)
                s["concurrent"] = max(0, s["concurrent"] - 1)

    def record_bytes(self, user: str, n: int) -> None:
        """Record bytes consumed by a request that already executed.

        Always commits the new total — even if it pushes the user past the
        daily cap — so subsequent ``check_daily_budget`` calls see the
        cumulative usage and reject pre-flight. This method NEVER raises
        anymore — the post-scan recording shouldn't strand a fetch the
        user already paid for. Pre-flight enforcement lives in
        ``check_daily_budget``.
        """
        if n <= 0:
            return
        with self._lock:
            s = self._ensure_bucket(user)
            s["bytes"] = s["bytes"] + n

    def check_daily_budget(self, user: str) -> None:
        """Pre-flight check: raise QuotaExceededError if the user is already
        AT or OVER the daily cap. Call this BEFORE running the BQ scan, so
        the user doesn't pay for a query whose result we'd then have to
        block on response."""
        with self._lock:
            current = self._ensure_bucket(user)["bytes"]
            if current >= self._max_daily_bytes:
                raise QuotaExceededError(
                    kind=KIND_DAILY_BYTES,
                    current=current,
                    limit=self._max_daily_bytes,
                    retry_after_seconds=_seconds_until_utc_midnight(),
                )

    def bytes_used_today(self, user: str) -> int:
        with self._lock:
            return self._ensure_bucket(user)["bytes"]
