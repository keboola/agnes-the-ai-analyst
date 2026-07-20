"""Leader-lease helper for singleton consumers (wave-2C task 3).

Some consumers must run on at most one replica at a time even when the
process topology is multi-replica behind a shared `redis` coordination
backend — a Slack Socket Mode WebSocket, a Telegram long-poll loop, and
the paused-sandbox TTL sweep all fall into this bucket (see
``app/main.py._start_slack_socket_transport``,
``services/telegram_bot/bot.py`` and ``app/chat/manager.py``'s
``_reap_once`` for the three call sites).

:func:`run_with_lease` is the shared acquire-or-wait loop: it takes a
lease `name`, a per-process `holder_id`, and two callables (`start`/
`stop`) the caller supplies to actually begin/end the singleton work. It
never runs the work itself — that decoupling is what lets a caller wrap
"start a WebSocket dispatcher" and "start an HTTP long-poll loop" with
the exact same lease machinery.

FLUSHALL story (the operational scenario every call site's comment
should point back to): if the coordination backend loses its state (a
Redis ``FLUSHALL``, a Redis restart with no persistence, or any renew
call raising :class:`~app.coordination.base.CoordinationUnavailable` for
longer than one ``ttl_s``), the current holder's next renew fails (or
keeps failing past the grace window) -> this loop calls ``stop()`` on
that replica -> re-enters the acquire loop -> some replica (possibly the
same one) re-acquires the free lease within one ``ttl_s`` and calls
``start()`` again. No replica is ever left believing it holds a lease it
does not, and no two replicas ever believe they hold the same lease at
once (mutual exclusion is the backend's job — see
``CoordinationBackend.lease_acquire``/``lease_renew``'s docstrings).

In ``memory`` mode (the zero-config, single-process default) the lease
is process-local: nothing else in the process ever contends for it, so
the very first ``lease_acquire`` always succeeds and every subsequent
``lease_renew`` from the same holder always succeeds too (barring a
``ttl_s`` shorter than the renew cadence, which callers should not
configure). The net effect is exactly today's pre-lease behavior —
``start()`` runs once at boot and keeps running for the process
lifetime — with zero new failure modes introduced for the common
single-container deployment.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from typing import Awaitable, Callable, Optional, Union

from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination

logger = logging.getLogger(__name__)

#: Floor so a misconfigured/very short ``ttl_s`` (e.g. in a test) can't
#: spin the renew loop into a busy-wait.
_MIN_RENEW_INTERVAL_S = 0.05


def default_holder_id() -> str:
    """``<hostname>:<pid>`` — stable per-process identity for lease
    holders. Mirrors ``app.worker.runtime.default_worker_id`` (not
    imported directly so ``app.coordination`` has no dependency on
    ``app.worker``); every lease call site in this process shares one
    holder id, which is fine — lease names are already per-consumer, so
    two leases held by the same process never collide."""
    return f"{socket.gethostname()}:{os.getpid()}"


async def _invoke(fn: Callable[[], Union[Awaitable[None], None]]) -> None:
    """Call `fn()`, awaiting the result iff it returned an awaitable.

    Lets `start`/`stop` callables be either `async def` or plain `def`
    (a plain callback that only flips a flag or clears local state has
    no reason to be a coroutine function).
    """
    result = fn()
    if result is not None:
        await result


async def run_with_lease(
    name: str,
    holder_id: str,
    *,
    ttl_s: int = 15,
    start: Callable[[], Union[Awaitable[None], None]],
    stop: Callable[[], Union[Awaitable[None], None]],
) -> None:
    """Acquire-or-wait loop around a named lease, running until cancelled.

    While not holding the lease: poll `coordination().lease_acquire`
    every ``ttl_s / 3`` seconds. On success, call `start()` and switch to
    the renew phase. If `start()` raises, this holder never actually
    began delivering the singleton work, so holding onto the lease would
    starve every other replica for no benefit: log the failure, best-effort
    `coordination().lease_release` the lease immediately (so a healthier
    replica can pick it up right away rather than waiting out this
    holder's `ttl_s`), back off for ``ttl_s`` seconds (so a `start()` that
    fails deterministically — e.g. a persistently unreachable upstream —
    doesn't spin this replica in a hot acquire/fail loop), then re-enter
    the acquire loop.

    All three `coordination()` lease calls (`lease_acquire`, `lease_renew`,
    `lease_release`) run via `asyncio.to_thread` — the Redis backend makes
    a blocking socket call per invocation, and this loop's own sleeps are
    the only other thing sharing the event loop with everything else the
    process is serving; without the offload, a slow/hung Redis round-trip
    on this heartbeat would stall unrelated request handling for the
    whole process.

    While holding the lease: sleep ``ttl_s / 3`` then call
    `coordination().lease_renew`. A `False` return means the lease was
    lost (expired and taken by another holder, or released out from
    under us) — call `stop()` and go back to polling for acquisition.
    A `CoordinationUnavailable` from `lease_renew` is tolerated for up
    to one `ttl_s` (a single Redis blip shorter than the lease's own
    expiry shouldn't stop a healthy consumer); once unavailability has
    persisted for a full `ttl_s`, treat it the same as a lost lease —
    call `stop()` and resume polling (a transport-level outage that
    outlives the lease's own TTL means some *other* replica may already
    believe it can take over, so this replica must stop believing it
    still holds anything).

    On cancellation (caller shutdown): call `stop()` if currently
    holding the lease, then best-effort `coordination().lease_release`
    (a no-op if the lease already expired/moved on), then re-raise
    `CancelledError` so the caller's own `await task` unwinds normally.

    Never returns on its own — the caller is expected to run this as a
    background `asyncio.Task` and cancel it at shutdown (see the three
    call sites' shutdown code for the exact pattern).
    """
    renew_interval = max(ttl_s / 3, _MIN_RENEW_INTERVAL_S)
    held = False
    unavailable_since: Optional[float] = None
    try:
        while True:
            if not held:
                try:
                    acquired = await asyncio.to_thread(coordination().lease_acquire, name, holder_id, ttl_s=ttl_s)
                except CoordinationUnavailable:
                    logger.warning(
                        "lease %r: coordination backend unavailable while acquiring; retrying in %.1fs",
                        name,
                        renew_interval,
                    )
                    await asyncio.sleep(renew_interval)
                    continue
                if not acquired:
                    await asyncio.sleep(renew_interval)
                    continue
                held = True
                unavailable_since = None
                logger.info("lease %r: acquired by %s", name, holder_id)
                try:
                    await _invoke(start)
                except Exception:
                    # start() never got the singleton work running, so
                    # holding the lease buys nothing except starving every
                    # other replica -> release it immediately (best-effort;
                    # a backend outage here just means the lease expires on
                    # its own ttl_s instead) and back off before retrying so
                    # a deterministically-failing start() doesn't spin this
                    # replica in a hot acquire/fail loop.
                    logger.exception(
                        "lease %r: start() failed after acquiring; releasing lease and backing off %ds before retrying",
                        name,
                        ttl_s,
                    )
                    held = False
                    try:
                        await asyncio.to_thread(coordination().lease_release, name, holder_id)
                    except CoordinationUnavailable:
                        pass
                    await asyncio.sleep(ttl_s)
                continue

            await asyncio.sleep(renew_interval)
            try:
                renewed = await asyncio.to_thread(coordination().lease_renew, name, holder_id, ttl_s=ttl_s)
            except CoordinationUnavailable:
                now = time.monotonic()
                if unavailable_since is None:
                    unavailable_since = now
                    logger.warning(
                        "lease %r: coordination backend unavailable while renewing; "
                        "tolerating for up to %ds before stopping",
                        name,
                        ttl_s,
                    )
                    continue
                if now - unavailable_since < ttl_s:
                    continue
                logger.warning(
                    "lease %r: coordination backend unavailable beyond ttl_s=%ds; "
                    "stopping consumer and re-entering acquire loop",
                    name,
                    ttl_s,
                )
                held = False
                unavailable_since = None
                await _invoke(stop)
                continue

            unavailable_since = None
            if not renewed:
                logger.warning(
                    "lease %r: lost (renew failed); stopping consumer and re-entering acquire loop",
                    name,
                )
                held = False
                await _invoke(stop)
    except asyncio.CancelledError:
        logger.info("lease %r: cancelled (holder=%s, held=%s); stopping and releasing", name, holder_id, held)
        if held:
            await _invoke(stop)
            try:
                await asyncio.to_thread(coordination().lease_release, name, holder_id)
            except CoordinationUnavailable:
                pass
        raise
