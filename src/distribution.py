"""Bucket-mirror marker index (three-plane wave 2-H, WS F, task WF-3 — see
``docs/superpowers/plans/2026-07-20-three-plane-wave2h-distribution.md``).

The marker index is a small JSON object at ``{prefix}/_mirrored.json``
recording exactly which tables' parquets are CURRENTLY mirrored to the
configured :class:`src.object_store.ObjectStore` — ``{table_id: md5}`` for
every table whose object is present *and* current (its stamped ``md5``
metadata matches ``sync_state.hash``) as of the last distribution-mirror
job run.

WF-2 (manifest signed URLs) reads this index before adding a ``signed_url``
to a table's manifest entry: a partial or failed mirror run must never
advertise a URL for an object that isn't there (or is stale), because the
client trusts the manifest and would otherwise fail the download with no
fallback signal. Reading the index is therefore always fail-open — any
error (missing object, malformed JSON, store outage) degrades to "no signed
URLs this request", never a 500, since the app-served
``/api/data/{id}/download`` path remains available regardless.

Lives in ``src/`` rather than either ``app/worker/kinds.py`` (the writer) or
``app/api/sync.py`` (the reader) so neither of those ``app/`` modules has to
import the other.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Dict

from src.object_store import ObjectStore

logger = logging.getLogger(__name__)

MIRROR_INDEX_KEY = "_mirrored.json"

# How long `cached_mirror_index` may serve a stale index before re-reading
# the store. WF-2's manifest build touches the store at most once per this
# window (never per-table) — the perf budget (manifest p95 < 300ms under
# 200 concurrent) rules out a store round-trip in the hot path on every
# request. A 45s window means a just-finished mirror run is visible to new
# `signed_url`s within one cache lifetime, which is well inside the 15-min
# presign TTL it feeds.
_MIRROR_INDEX_CACHE_TTL_S = 45.0

_mirror_index_lock = threading.Lock()
_mirror_index_cache: Dict[str, str] = {}
_mirror_index_cache_at: float = 0.0


def write_mirror_index(store: ObjectStore, mapping: Dict[str, str]) -> None:
    """Upload the marker index — ``{"tables": {table_id: md5, ...}, "updated":
    <iso8601>}`` — to :data:`MIRROR_INDEX_KEY`.

    Best-effort: any failure is logged, not raised, so a marker-index write
    hiccup never fails the whole distribution-mirror job — the per-file
    uploads it summarizes have already landed regardless of whether this
    final bookkeeping write succeeds. A dropped write here is self-healing:
    the next mirror run recomputes the full mapping from scratch and
    overwrites it.
    """
    payload = {
        "tables": dict(mapping),
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    data = json.dumps(payload).encode("utf-8")
    md5 = hashlib.md5(data).hexdigest()
    try:
        store.put_bytes(MIRROR_INDEX_KEY, data, md5=md5)
    except Exception:
        logger.exception("distribution mirror: failed to write marker index %s", MIRROR_INDEX_KEY)


def read_mirror_index(store: ObjectStore) -> Dict[str, str]:
    """Return ``{table_id: md5}`` from the marker index, or ``{}`` on ANY
    failure (missing object, malformed JSON, store error) — fail-open, see
    the module docstring."""
    try:
        data = store.get_bytes(MIRROR_INDEX_KEY)
        if data is None:
            return {}
        payload = json.loads(data)
        tables = payload.get("tables") if isinstance(payload, dict) else None
        if not isinstance(tables, dict):
            return {}
        return {str(k): str(v) for k, v in tables.items()}
    except Exception:
        logger.exception("distribution mirror: failed to read marker index %s", MIRROR_INDEX_KEY)
        return {}


def cached_mirror_index(store: ObjectStore, ttl_s: float = _MIRROR_INDEX_CACHE_TTL_S) -> Dict[str, str]:
    """Process-wide, short-TTL-cached wrapper around :func:`read_mirror_index`.

    WF-2 (manifest build) calls this once per manifest build rather than
    calling :func:`read_mirror_index` directly, so a burst of concurrent
    ``/api/sync/manifest`` requests shares one store round-trip per
    ``ttl_s`` window instead of one per request — the perf budget (manifest
    p95 < 300ms under 200 concurrent) rules out a per-request store hit.

    Still fail-open: :func:`read_mirror_index` already returns ``{}`` on any
    store error, and that empty result is cached too (a down store degrades
    to "no signed URLs this cycle" for the whole TTL window, never a
    manifest-build failure).

    :func:`reset_mirror_index_cache` is the test-facing invalidation hook —
    tests that seed a fresh marker index per case must call it (directly or
    via an autouse fixture) so a prior case's cached index doesn't leak.
    """
    global _mirror_index_cache, _mirror_index_cache_at
    now = time.monotonic()
    with _mirror_index_lock:
        if now - _mirror_index_cache_at < ttl_s:
            return _mirror_index_cache
    index = read_mirror_index(store)
    with _mirror_index_lock:
        _mirror_index_cache = index
        _mirror_index_cache_at = time.monotonic()
        return _mirror_index_cache


def reset_mirror_index_cache() -> None:
    """Drop the cached mirror index so the next :func:`cached_mirror_index`
    call re-reads the store. Test-facing invalidation hook — mirrors
    ``src.object_store.reset_object_store_cache`` / ``reset_database_cache``
    elsewhere in the codebase."""
    global _mirror_index_cache, _mirror_index_cache_at
    with _mirror_index_lock:
        _mirror_index_cache = {}
        _mirror_index_cache_at = 0.0
