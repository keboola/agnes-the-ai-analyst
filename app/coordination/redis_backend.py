"""Redis-backed CoordinationBackend — the multi-process implementation.

Used when ``coordination.backend`` resolves to ``"redis"`` (see
:mod:`app.coordination.factory`). Built on redis-py; every method wraps
transport failures (``redis.exceptions.RedisError`` and subclasses —
connection refused, timeout, or a command-level error) into
:class:`app.coordination.base.CoordinationUnavailable`, EXCEPT
:meth:`ping`, which returns ``False`` instead — see the base class
docstring for the rationale.

Primitive-by-primitive notes:

- ``kv_delete`` is a plain ``GETDEL`` (Redis >= 6.2; fakeredis supports
  it) — atomic get-and-delete server-side, no read-then-DEL round trip
  that could race two callers.
- ``incr`` pipelines ``SET key 0 EX ttl_s NX`` followed by ``INCRBY key
  amount`` inside one MULTI/EXEC transaction (redis-py's default
  ``pipeline(transaction=True)``) — the ``NX`` makes the SET a no-op when
  the key already exists, so the TTL is only ever established on the
  first increment of a window, and wrapping both commands in one
  transaction closes the race between "does the key exist" and "set it".
  ``amount=0`` is a valid no-op increment (a "peek" at the current
  value) — ``INCRBY key 0`` is a normal Redis command, not a special case.
- ``lease_acquire`` is ``SET name holder_id NX PX ttl_ms`` — the standard
  single-instance Redis lock acquire (create-if-absent, no compare
  needed since NX already guarantees exclusivity).
- ``lease_renew`` / ``lease_release`` need "extend/delete iff I'm still
  the holder" — a plain GET-then-PEXPIRE (or GET-then-DEL) from the
  client would race a concurrent expiry or steal between the two round
  trips. Both use the standard redis-py **WATCH/MULTI/EXEC** optimistic-
  transaction pattern (see :meth:`_compare_and_run`): WATCH the key, GET
  it, and only if it still equals ``holder_id`` do we MULTI the mutating
  command and EXEC — if anything touched the key in between, EXEC raises
  ``WatchError`` and we retry (which will then correctly observe the new
  owner, or no owner, and return accordingly). Deliberately not a Lua
  script (the other standard option for this pattern): keeps the
  fakeredis-backed half of the contract-test matrix dependency-free (Lua
  scripting in fakeredis needs the optional, natively-compiled ``lupa``
  package).
- Pub/sub uses one shared ``redis.client.PubSub`` object plus one daemon
  listener thread, started lazily on the first ``subscribe()`` call; the
  thread polls ``get_message(timeout=...)`` in a loop and dispatches to
  the registered handlers for whichever channel the message names.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Callable, Optional

import redis as redis_lib

from app.coordination.base import CoordinationBackend, CoordinationUnavailable

logger = logging.getLogger(__name__)

# Any of these (ConnectionError, TimeoutError, and the broader RedisError
# they both subclass — includes command-level failures like a malformed
# command) surface to callers as CoordinationUnavailable rather than
# propagating a redis-py-specific exception type up through the ABC.
_REDIS_ERRORS = (redis_lib.exceptions.RedisError, OSError)

# Bound on WATCH/MULTI/EXEC retries in :meth:`RedisCoordinationBackend._compare_and_run`
# — a WatchError means another client mutated the key between our WATCH and
# EXEC, which the next iteration will observe and resolve. Contention on a
# single lease key is expected to be rare (at most a couple of concurrent
# renew/release calls), so this is a generous ceiling against a pathological
# hot loop, not a tuning knob.
_MAX_CAS_RETRIES = 50


class RedisCoordinationBackend(CoordinationBackend):
    """See :class:`app.coordination.base.CoordinationBackend` for the contract.

    ``client`` is any redis-py-compatible client (``redis.Redis`` or a
    ``fakeredis`` fake) constructed with ``decode_responses=True`` — every
    method here returns/accepts plain ``str``, never ``bytes``.
    """

    def __init__(self, client: "redis_lib.Redis") -> None:
        self._client = client

        self._pubsub: Optional["redis_lib.client.PubSub"] = None
        self._pubsub_lock = threading.Lock()
        self._listener_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._subscribers_lock = threading.Lock()
        self._subscribers: dict[str, list[Callable[[str], None]]] = {}

    # -- KV -------------------------------------------------------------------

    def kv_set(self, key: str, value: str, *, ttl_s: int) -> None:
        try:
            self._client.set(key, value, ex=ttl_s)
        except _REDIS_ERRORS as exc:
            raise CoordinationUnavailable(f"redis kv_set failed: {exc}") from exc

    def kv_get(self, key: str) -> Optional[str]:
        try:
            return self._client.get(key)
        except _REDIS_ERRORS as exc:
            raise CoordinationUnavailable(f"redis kv_get failed: {exc}") from exc

    def kv_delete(self, key: str) -> Optional[str]:
        try:
            return self._client.getdel(key)
        except _REDIS_ERRORS as exc:
            raise CoordinationUnavailable(f"redis kv_delete failed: {exc}") from exc

    # -- Counters ---------------------------------------------------------------

    def incr(self, key: str, *, amount: int = 1, ttl_s: int) -> int:
        try:
            pipe = self._client.pipeline(transaction=True)
            pipe.set(key, 0, ex=ttl_s, nx=True)
            pipe.incrby(key, amount)
            _, new_value = pipe.execute()
            return int(new_value)
        except _REDIS_ERRORS as exc:
            raise CoordinationUnavailable(f"redis incr failed: {exc}") from exc

    # -- Leases -------------------------------------------------------------------

    def lease_acquire(self, name: str, holder_id: str, *, ttl_s: int) -> bool:
        try:
            result = self._client.set(name, holder_id, nx=True, px=ttl_s * 1000)
        except _REDIS_ERRORS as exc:
            raise CoordinationUnavailable(f"redis lease_acquire failed: {exc}") from exc
        return bool(result)

    def lease_renew(self, name: str, holder_id: str, *, ttl_s: int) -> bool:
        def _action(pipe: "redis_lib.client.Pipeline") -> None:
            pipe.pexpire(name, ttl_s * 1000)

        return self._compare_and_run(name, holder_id, _action)

    def lease_release(self, name: str, holder_id: str) -> None:
        def _action(pipe: "redis_lib.client.Pipeline") -> None:
            pipe.delete(name)

        self._compare_and_run(name, holder_id, _action)

    def lease_owner(self, name: str) -> Optional[str]:
        # Lease keys share the same top-level Redis keyspace as kv_set/
        # kv_get/lease_acquire (a plain `SET name holder_id ...`), so a
        # plain GET returns the current holder — no separate namespace to
        # maintain.
        try:
            return self._client.get(name)
        except _REDIS_ERRORS as exc:
            raise CoordinationUnavailable(f"redis lease_owner failed: {exc}") from exc

    def _compare_and_run(self, key: str, holder_id: str, action: Callable[["redis_lib.client.Pipeline"], None]) -> bool:
        """WATCH ``key``; if its current value is ``holder_id``, run
        ``action`` (one or more pipelined commands) inside MULTI/EXEC.
        Returns ``True`` iff ``action`` actually ran (holder matched and
        EXEC committed), ``False`` if the holder didn't match. Retries on
        ``WatchError`` (another client raced us between WATCH and EXEC) —
        see the module docstring for why this replaces a Lua script.
        """
        try:
            with self._client.pipeline() as pipe:
                for _ in range(_MAX_CAS_RETRIES):
                    try:
                        pipe.watch(key)
                        current = pipe.get(key)
                        if current != holder_id:
                            pipe.unwatch()
                            return False
                        pipe.multi()
                        action(pipe)
                        pipe.execute()
                        return True
                    except redis_lib.exceptions.WatchError:
                        continue
                raise CoordinationUnavailable(
                    f"redis compare-and-run on {key!r} did not converge after {_MAX_CAS_RETRIES} retries"
                )
        except _REDIS_ERRORS as exc:
            raise CoordinationUnavailable(f"redis compare-and-run on {key!r} failed: {exc}") from exc

    # -- Pub/sub ------------------------------------------------------------------

    def _ensure_listener(self) -> None:
        with self._pubsub_lock:
            if self._pubsub is not None:
                return
            try:
                self._pubsub = self._client.pubsub(ignore_subscribe_messages=True)
            except _REDIS_ERRORS as exc:
                raise CoordinationUnavailable(f"redis subscribe failed: {exc}") from exc
            thread = threading.Thread(
                target=self._listen_loop,
                name="coordination-redis-pubsub",
                daemon=True,
            )
            self._listener_thread = thread
            thread.start()

    def _listen_loop(self) -> None:
        while not self._stop_event.is_set():
            pubsub = self._pubsub
            if pubsub is None:
                return
            try:
                message = pubsub.get_message(timeout=0.5)
            except Exception:
                # Transport hiccup on the listener thread — nothing to
                # propagate to a caller (no one is blocked on this thread),
                # so log and keep polling rather than killing the thread.
                logger.debug("coordination pub/sub listener error", exc_info=True)
                continue
            if not message or message.get("type") != "message":
                continue
            channel = message.get("channel")
            data = message.get("data")
            with self._subscribers_lock:
                handlers = list(self._subscribers.get(channel, ()))
            for handler in handlers:
                try:
                    handler(data)
                except Exception:
                    logger.exception("coordination subscribe handler raised for channel %r", channel)

    def publish(self, channel: str, message: str) -> None:
        try:
            self._client.publish(channel, message)
        except _REDIS_ERRORS as exc:
            raise CoordinationUnavailable(f"redis publish failed: {exc}") from exc

    def subscribe(self, channel: str, handler: Callable[[str], None]) -> Callable[[], None]:
        self._ensure_listener()
        with self._subscribers_lock:
            is_new_channel = channel not in self._subscribers
            self._subscribers.setdefault(channel, []).append(handler)
        if is_new_channel:
            try:
                self._pubsub.subscribe(channel)
            except _REDIS_ERRORS as exc:
                # Roll back the local registration we just made: if the
                # Redis-level SUBSCRIBE never succeeded, no local state may
                # survive. Otherwise this channel would look already-live to
                # every future subscribe() call (is_new_channel=False) and
                # the SUBSCRIBE would never be retried — a permanently
                # broken channel with a handler that never fires, and the
                # caller received no unsubscribe callable to clean it up.
                with self._subscribers_lock:
                    handlers = self._subscribers.get(channel)
                    if handlers and handler in handlers:
                        handlers.remove(handler)
                    if not self._subscribers.get(channel):
                        self._subscribers.pop(channel, None)
                raise CoordinationUnavailable(f"redis subscribe failed: {exc}") from exc

        def _unsubscribe() -> None:
            with self._subscribers_lock:
                handlers = self._subscribers.get(channel)
                if handlers and handler in handlers:
                    handlers.remove(handler)
                channel_empty = not self._subscribers.get(channel)
                if channel_empty:
                    self._subscribers.pop(channel, None)
            if channel_empty and self._pubsub is not None:
                try:
                    self._pubsub.unsubscribe(channel)
                except Exception:
                    logger.debug("redis unsubscribe failed", exc_info=True)

        return _unsubscribe

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:
            # Deliberately broad: ping's entire job is "is this reachable?" —
            # ANY failure (including non-RedisError transport issues) means no.
            return False

    def close(self) -> None:
        """Stop the pub/sub listener thread, if one was started.

        Not part of the :class:`CoordinationBackend` ABC — an
        implementation detail called by
        :func:`app.coordination.factory.reset_coordination_for_tests` (and
        available for any long-lived owner doing an orderly shutdown) so
        tests don't leak daemon threads across many backend instances.
        """
        self._stop_event.set()
        with self._pubsub_lock:
            pubsub = self._pubsub
            self._pubsub = None
        if pubsub is not None:
            try:
                pubsub.close()
            except Exception:
                logger.debug("redis pubsub close failed", exc_info=True)
        thread = self._listener_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    # -- Streams ------------------------------------------------------------------
    #
    # Backed by a native Redis stream (XADD/XRANGE), one entry per JSON-
    # serialized ``entry`` dict under a single ``"data"`` field — streams
    # store field-value pairs, not arbitrary objects, so this is the
    # simplest encoding that round-trips any dict. Trimming is EXACT
    # (``MAXLEN`` without ``~``), not Redis's approximate/`~` trim: an
    # approximate trim only removes whole radix-tree nodes and can retain
    # more than ``maxlen`` entries, which would make the eviction-vs-
    # after_seq-gap contract test in tests/test_chat_replay.py
    # non-deterministic between the memory backend (an exact `deque`
    # maxlen) and this one. The exact trim is an O(N) command, but N is
    # bounded by ``maxlen`` itself (~1000 for the chat-out replay stream),
    # which is cheap enough not to matter.

    def stream_append(self, key: str, entry: dict, *, maxlen: int) -> str:
        try:
            entry_id = self._client.xadd(
                key,
                {"data": json.dumps(entry)},
                maxlen=maxlen,
                approximate=False,
            )
        except _REDIS_ERRORS as exc:
            raise CoordinationUnavailable(f"redis stream_append failed: {exc}") from exc
        return entry_id

    def stream_read(self, key: str, after_seq: Optional[int] = None) -> list[dict]:
        # after_seq filters on the "seq" field INSIDE the JSON payload, not
        # the Redis-generated stream id, so we can't ask Redis to start the
        # XRANGE past a given id — read everything currently retained (
        # bounded by maxlen at append time) and filter client-side.
        try:
            raw_entries = self._client.xrange(key, min="-", max="+")
        except _REDIS_ERRORS as exc:
            raise CoordinationUnavailable(f"redis stream_read failed: {exc}") from exc
        entries: list[dict] = []
        for _entry_id, fields in raw_entries:
            data = fields.get("data") if fields else None
            if data is None:
                continue
            try:
                entry = json.loads(data)
            except (TypeError, ValueError):
                # Malformed payload (shouldn't happen — we're the only
                # writer) — skip rather than raise, same "degrade the
                # replay, don't crash" posture as a missing "seq" field.
                continue
            entries.append(entry)
        # Sorted by the frame's own "seq" field, not Redis XADD arrival
        # order (2026-07-18 hardening) — append_frame now runs OUTSIDE
        # ChatManager._broadcast_lock, so two concurrent XADDs for the same
        # session's stream can land in a different order than their
        # stamps. Stable sort preserves relative order for ties/missing
        # seq.
        entries.sort(key=lambda e: e.get("seq", 0))
        if after_seq is None:
            return entries
        return [e for e in entries if e.get("seq", 0) > after_seq]
