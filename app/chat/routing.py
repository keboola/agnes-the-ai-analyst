"""Session routing leases (wave-2F task 1).

Multiple gateway replicas may run behind a shared `redis` coordination
backend (see ``app/coordination/factory.py``). Exactly one replica may be
the "live" host of a given chat session's sandbox/runner at a time — this
module gives any replica a way to find out which one, and to claim/renew/
release that ownership, via one lease per session (name ``chat:{chat_id}``)
on the process-wide :func:`app.coordination.factory.coordination` backend.

This module only provides the primitives. :class:`app.chat.manager.ChatManager`
is the actual consumer: it claims the lease when a session becomes live
in its ``self._live`` registry (``_spawn_live`` / ``_resume_from_row`` /
``_takeover_foreign_session``), renews it on the existing idle-reaper
heartbeat (``_reap_once``, ~60s cadence), and releases it on teardown
(``kill``). What ChatManager actually DOES with a claim failure or a lost
renew has evolved across two tasks: task 1 (this module) only ever logged
and kept serving locally. Wave-2F task 5 added real handling on both
sides — a WS connect for a chat_id owned by a different, still-valid
gateway now claims (steals) the lease and takes over via
``ChatManager._takeover_foreign_session`` (destroy the old sandbox,
respawn a fresh runner, replay recent turns — NOT a live handoff, see that
method's docstring for why and its accepted trade-off), and a renew that
comes back lost is followed by ONE MORE read (``owner_of``) before
``ChatManager`` decides what to do: only a POSITIVE, concrete different
gateway id is treated as a genuine steal and tears the local session down
(``ChatManager._teardown_lost_ownership``); ``owner_of`` returning ``None``
(unclaimed, expired with nobody else holding it yet, or the coordination
backend itself being unreachable) is NOT proof of loss, so that case keeps
serving locally and retries on the next reaper tick instead — see
``ChatManager._renew_routing_leases``'s Critical-3 fix for the full
reasoning (a naive "any False renew means torn down" reaction would turn an
ordinary transient backend blip into every replica dropping every session
it hosts). A lease loss under the default ``memory`` backend can still
never actually happen (single process, nothing else ever contends — see
``app.coordination.leases``'s FLUSHALL/memory-mode docstring for the same
invariant applied to the leader-lease helper), so none of this is reachable
there; under `redis` it is now live.

FLUSHALL / CoordinationUnavailable posture: none of the four functions
below ever raises :class:`~app.coordination.base.CoordinationUnavailable`
to their caller — a transport-level failure is caught, logged, and turned
into the same "didn't happen" result a legitimate negative would produce
(``False`` for claim/renew, ``None`` for owner_of, a no-op for release).
A caller that loses a claim/renew because the backend is unavailable is in
exactly the same position as one that lost it to another gateway: it must
not crash, and must not assume it still owns the session.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.coordination.base import CoordinationUnavailable
from app.coordination.factory import coordination
from app.coordination.leases import default_holder_id

logger = logging.getLogger(__name__)

#: Default lease TTL for a routing claim/renew, in seconds. Callers with a
#: different heartbeat cadence (e.g. ChatManager's idle-reaper tick) should
#: pass an explicit ``ttl_s`` sized to that cadence — see
#: ``app.chat.manager._ROUTING_LEASE_TTL_SEC``.
DEFAULT_TTL_S = 30


def _lease_name(chat_id: str) -> str:
    return f"chat:{chat_id}"


def this_gateway_id() -> str:
    """Stable per-process identity for this gateway replica.

    ``<hostname>:<pid>`` — reuses the exact convention
    ``app.coordination.leases.default_holder_id`` already established for
    every other lease holder in this process, so a routing-lease holder_id
    and (say) the paused-sandbox-sweep lease's holder_id are the same
    string for a given process.
    """
    return default_holder_id()


def claim_session(chat_id: str, gateway_id: str, *, ttl_s: int = DEFAULT_TTL_S) -> bool:
    """Attempt to claim ownership of ``chat_id`` for ``gateway_id``.

    Returns ``True`` if claimed (the lease was free or had expired),
    ``False`` if another gateway currently holds it (or the backend is
    unavailable — see module docstring). A caller that already holds the
    lease must use :func:`renew_session`, not call this again — same
    exclusive-acquire semantics as the underlying
    :meth:`~app.coordination.base.CoordinationBackend.lease_acquire`.
    """
    try:
        return coordination().lease_acquire(_lease_name(chat_id), gateway_id, ttl_s=ttl_s)
    except CoordinationUnavailable:
        logger.warning(
            "routing lease claim for %r: coordination backend unavailable; treating as not claimed",
            chat_id,
        )
        return False


def renew_session(chat_id: str, gateway_id: str, *, ttl_s: int = DEFAULT_TTL_S) -> bool:
    """Extend ``gateway_id``'s claim on ``chat_id`` iff it is still the
    current holder.

    Returns ``False`` if the lease was lost (expired and taken by another
    gateway, released, or the backend is unavailable) — a caller must not
    assume it still owns the session after a ``False`` return; see the
    module docstring's FLUSHALL posture.
    """
    try:
        return coordination().lease_renew(_lease_name(chat_id), gateway_id, ttl_s=ttl_s)
    except CoordinationUnavailable:
        logger.warning(
            "routing lease renew for %r: coordination backend unavailable; treating as lost",
            chat_id,
        )
        return False


def release_session(chat_id: str, gateway_id: str) -> None:
    """Release ``gateway_id``'s claim on ``chat_id``, if it still holds it.

    No-op if the lease is already held by someone else (expired and
    stolen) or the backend is unavailable — teardown must never raise on
    this best-effort cleanup.
    """
    try:
        coordination().lease_release(_lease_name(chat_id), gateway_id)
    except CoordinationUnavailable:
        logger.warning(
            "routing lease release for %r: coordination backend unavailable; leaving it to expire on its own TTL",
            chat_id,
        )


def owner_of(chat_id: str) -> Optional[str]:
    """Return the ``gateway_id`` that currently owns ``chat_id``'s routing
    lease, or ``None`` if unclaimed, expired, or the backend is
    unavailable."""
    try:
        return coordination().lease_owner(_lease_name(chat_id))
    except CoordinationUnavailable:
        logger.warning(
            "routing lease owner lookup for %r: coordination backend unavailable; treating as unknown",
            chat_id,
        )
        return None
