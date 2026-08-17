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
#: A daily budget denominated in STATEMENTS, not bytes.
#:
#: The byte budget prices what a scan moved. That works for an engine that
#: reports what a statement cost — BigQuery's dry run prices a query before it
#: runs — but it cannot bound an engine that only reports what a statement
#: RETURNED. A Databricks ``COUNT(*)`` scans a Delta table and returns one row:
#: real warehouse compute, ~0 bytes recorded. Ten thousand of them are ten
#: thousand billable statements that the byte ledger reads as free.
#:
#: So this counter measures the one unit that is honestly available there —
#: how many billable statements a caller submitted today. It is deliberately
#: NOT folded into ``daily_bytes``: inventing a byte figure for a statement
#: whose bytes are unknown would corrupt the ledger for the engine that
#: reports real ones.
KIND_DAILY_ESTIMATES = "daily_estimates"


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

    def __init__(
        self,
        *,
        max_concurrent_per_user: int,
        max_daily_bytes_per_user: int,
        max_daily_estimates_per_user: int = 500,
    ):
        self._max_concurrent = max_concurrent_per_user
        self._max_daily_bytes = max_daily_bytes_per_user
        self._max_daily_estimates = max_daily_estimates_per_user
        self._lock = threading.Lock()
        # state: { user_id: { "concurrent": int, "bucket_day": "YYYY-MM-DD",
        #                     "bytes": int, "estimates": int } }
        self._state: dict[str, dict] = {}

    def _ensure_bucket(self, user: str) -> dict:
        today = _utc_today()
        s = self._state.setdefault(user, {"concurrent": 0, "bucket_day": today, "bytes": 0, "estimates": 0})
        if s["bucket_day"] != today:
            s["bucket_day"] = today
            s["bytes"] = 0
            s["estimates"] = 0
        s.setdefault("estimates", 0)
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

    # ------------------------------------------------------------------
    # Statement-count budget (see KIND_DAILY_ESTIMATES)
    # ------------------------------------------------------------------

    def check_daily_estimates(self, user: str) -> None:
        """Pre-flight: raise if the user is AT or OVER the daily statement cap.

        Mirrors ``check_daily_budget``'s contract — call it BEFORE submitting
        the billable statement, so the refusal costs nothing.
        """
        with self._lock:
            current = self._ensure_bucket(user)["estimates"]
            if current >= self._max_daily_estimates:
                raise QuotaExceededError(
                    kind=KIND_DAILY_ESTIMATES,
                    current=current,
                    limit=self._max_daily_estimates,
                    retry_after_seconds=_seconds_until_utc_midnight(),
                )

    def record_estimate(self, user: str, n: int = 1) -> None:
        """Count billable statements a caller has submitted today.

        Deliberately recorded at SUBMISSION, not on success: what the
        warehouse charges for is the statement reaching it, so a loop of
        statements that time out or error costs exactly as much as a loop that
        succeeds, and must be bounded the same way. Never raises — like
        ``record_bytes``, enforcement lives in the pre-flight check.
        """
        if n <= 0:
            return
        with self._lock:
            s = self._ensure_bucket(user)
            s["estimates"] = s["estimates"] + n

    def estimates_used_today(self, user: str) -> int:
        with self._lock:
            return self._ensure_bucket(user)["estimates"]


# Module-level singleton (process-local quota state per spec §3.8). FastAPI
# dispatches sync handlers via a thread pool, so two concurrent first-time
# requests can both observe `_quota_singleton is None` and each construct a
# separate tracker; the second assignment wins and the first reference leaks
# split-brain quota state. Guard with an init lock + double-check.
#
# Note: `_quota_singleton` and `_quota_init_lock` are intentionally
# module-private. Callers MUST go through `_build_quota_tracker()` so the
# singleton stays single. Re-exporting `_quota_singleton` from another
# module via `from app.api.v2_quota import _quota_singleton` would copy the
# initial-None binding at import time and never see subsequent updates —
# that's a footgun. The function re-export is safe (it always reads the
# live module-global).
_quota_init_lock = threading.Lock()
_quota_singleton: "QuotaTracker | None" = None


def _build_quota_tracker() -> QuotaTracker:
    """Returns or constructs the process-local quota tracker (thread-safe).

    Shared across `/api/v2/scan` (the original caller) and `/api/query`
    (issue #160 cost guardrail) so the per-user daily byte cap accumulates
    across both BQ-touching paths.
    """
    from app.instance_config import get_value

    global _quota_singleton
    if _quota_singleton is not None:
        return _quota_singleton
    with _quota_init_lock:
        if _quota_singleton is None:
            _quota_singleton = QuotaTracker(
                max_concurrent_per_user=int(get_value("api", "scan", "max_concurrent_per_user", default=5) or 5),
                max_daily_bytes_per_user=int(
                    get_value("api", "scan", "max_daily_bytes_per_user", default=53687091200) or 53687091200
                ),
                max_daily_estimates_per_user=int(
                    get_value("api", "scan", "max_daily_estimates_per_user", default=500) or 500
                ),
            )
    return _quota_singleton
