"""CoordinationBackend abstract interface.

Defines the four primitive groups every implementation must provide —
see each method's docstring for the exact contract. Both implementations
(:mod:`app.coordination.memory`, :mod:`app.coordination.redis_backend`)
are exercised against the identical assertion set in
``tests/test_coordination_contract.py``, so a consumer written against
this ABC gets the same guarantees regardless of which backend an
instance is configured with.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional


class CoordinationUnavailable(RuntimeError):
    """Raised when a coordination backend cannot reach its store.

    Every method EXCEPT :meth:`CoordinationBackend.ping` raises this on a
    transport-level failure (e.g. Redis connection refused/timed out)
    rather than silently returning a falsy value that a caller could
    mistake for a legitimate negative result (lease not held, key not
    found, etc.). Callers decide what "unavailable" means for their own
    operation — fail open, fail closed, or retry.

    ``ping()`` is the deliberate exception: it exists precisely to answer
    "is the backend reachable?" and returns ``False`` for a connectivity
    failure instead of raising.
    """


class CoordinationBackend(ABC):
    """Cross-process coordination primitives: TTL KV, counters, leases, pub/sub.

    Obtain the active instance via :func:`app.coordination.factory.coordination`
    — do not instantiate a concrete backend directly outside of tests and
    the factory itself.
    """

    # -- KV with TTL (tickets, operational codes) ---------------------------

    @abstractmethod
    def kv_set(self, key: str, value: str, *, ttl_s: int) -> None:
        """Set ``key`` to ``value``, expiring after ``ttl_s`` seconds."""

    @abstractmethod
    def kv_get(self, key: str) -> Optional[str]:
        """Return the current value of ``key``, or ``None`` if absent or expired."""

    @abstractmethod
    def kv_delete(self, key: str) -> Optional[str]:
        """Atomically get-and-delete ``key`` — single-use ticket semantics.

        Returns the value that was present (now removed), or ``None`` if
        the key was already gone (expired, never set, or already consumed
        by a concurrent caller). When multiple callers race to delete the
        same key, exactly one receives the non-``None`` value.
        """

    # -- Counters (rate limits, quotas) --------------------------------------

    @abstractmethod
    def incr(self, key: str, *, amount: int = 1, ttl_s: int) -> int:
        """Increment ``key`` by ``amount`` and return the new value.

        ``ttl_s`` is applied ONLY when this call creates the key (i.e. the
        key didn't exist, or its previous TTL had already expired) — later
        increments within the same window do not reset the expiry. The
        caller encodes the rate-limit window into the key name (e.g. a
        per-minute bucket suffix); this method has no notion of windows
        beyond that.

        ``amount`` defaults to ``1`` (the common rate-limiting "count one
        event" case). A quota that accumulates a variable-sized delta per
        event (e.g. LLM tokens spent on a turn) passes the delta directly.
        ``amount=0`` is a valid, deliberate no-op increment — a "peek" at
        the current value (creating the key with value ``0`` and the given
        TTL if it didn't already exist) without a real event occurring.
        """

    # -- Leases (leader election, singleton sweeps) --------------------------

    @abstractmethod
    def lease_acquire(self, name: str, holder_id: str, *, ttl_s: int) -> bool:
        """Attempt to acquire lease ``name`` for ``holder_id``.

        Returns ``True`` if acquired (the lease was free, or a previous
        holder's lease had already expired), ``False`` if another holder
        currently holds an unexpired lease. Acquiring is exclusive
        regardless of holder identity — a holder that already holds the
        lease and calls ``lease_acquire`` again gets ``False`` too; use
        :meth:`lease_renew` to extend an already-held lease.
        """

    @abstractmethod
    def lease_renew(self, name: str, holder_id: str, *, ttl_s: int) -> bool:
        """Atomically extend lease ``name`` iff ``holder_id`` is still the
        current holder.

        Returns ``False`` (no-op) if the lease is held by a different
        holder, or has already expired/been released — a caller must not
        assume it still holds the lease after a ``False`` return.
        """

    @abstractmethod
    def lease_release(self, name: str, holder_id: str) -> None:
        """Release lease ``name`` iff ``holder_id`` is still the current
        holder; a no-op otherwise (e.g. the lease already expired and was
        stolen by another holder)."""

    # -- Pub/sub (cache invalidation) -----------------------------------------

    @abstractmethod
    def publish(self, channel: str, message: str) -> None:
        """Publish ``message`` on ``channel`` to every current subscriber."""

    @abstractmethod
    def subscribe(self, channel: str, handler: Callable[[str], None]) -> Callable[[], None]:
        """Register ``handler`` to be invoked with each message published on
        ``channel``. Returns an unsubscribe callable; calling it more than
        once is safe (a no-op after the first call)."""

    @abstractmethod
    def ping(self) -> bool:
        """Health check — ``True`` if the backend is reachable and
        functioning, ``False`` otherwise.

        Unlike every other method here, ``ping`` never raises
        :class:`CoordinationUnavailable` for a plain connectivity failure
        — that failure IS the ``False`` it returns.
        """
