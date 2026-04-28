"""Build a deterministic ZIP + per-request info for the aggregated marketplace.

The ZIP is the delivery artifact for the non-git channel. Its layout:

    .claude-plugin/marketplace.json   ← merged, prefixed-name manifest
    plugins/<prefixed_name>/...       ← copy of ${DATA_DIR}/marketplaces/<slug>/
                                         plugins/<plugin_name>/...
    .agnes/version.json               ← per-request diagnostics

Determinism requirements:
  - Members sorted by arcname
  - Fixed DOS timestamp (1980-01-01)
  - ZIP_DEFLATED
  - UNIX mode 0o644

Two users with the same allowed plugin set therefore produce byte-identical
ZIPs (modulo `.agnes/version.json`, which carries `generated_at`; this is why
the git channel strips that file — see git_backend).
"""

from __future__ import annotations

import io
import json
import os
import threading
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import duckdb
from cachetools import TTLCache

from src import marketplace_filter

MARKETPLACE_NAME = "agnes"

# In-process TTL cache for compute_etag() results. The expensive part of
# compute_etag is a SHA256 over every plugin file on disk; for a stable
# marketplace this hash doesn't change between requests. We key on the
# resolved plugin set (prefixed_name + version + plugin_dir path) so two
# users with the same allowed view share the same cache entry.
#
# TTL bounds drift between cache and on-disk content. Marketplace sync runs
# nightly; the default 120s TTL means the first session-start in a cold
# minute pays the SHA cost and the next ~120s of session-starts (across all
# users with the same view) hit the cache. Override with
# AGNES_MARKETPLACE_ETAG_TTL=<seconds> for tests / tighter staleness bounds;
# set 0 to disable.
_ETAG_CACHE_TTL = int(os.environ.get("AGNES_MARKETPLACE_ETAG_TTL", "120"))
_ETAG_CACHE: Optional[TTLCache] = (
    TTLCache(maxsize=512, ttl=_ETAG_CACHE_TTL) if _ETAG_CACHE_TTL > 0 else None
)
_ETAG_CACHE_LOCK = threading.Lock()
MARKETPLACE_OWNER = {"name": "Agnes AI Analyst"}
MARKETPLACE_DESCRIPTION = (
    "Aggregated per-user Claude Code marketplace — served by agnes-the-ai-analyst"
)
DETERMINISTIC_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _merged_manifest(plugins: List[dict], etag: str) -> Dict[str, Any]:
    """Synthesize .claude-plugin/marketplace.json over the filtered plugin set.

    Each entry copies the plugin's cached `raw` manifest, then overrides:
      - `name`   = prefixed_name
      - `source` = "./plugins/<prefixed_name>"  (flat relative path in the ZIP)
    All other fields (version, description, author, homepage, keywords, ...)
    are preserved so Claude Code UI looks the same as if the user pulled from
    the upstream marketplace directly.
    """
    entries: List[dict] = []
    for plugin in plugins:
        entry = dict(plugin["raw"])  # shallow copy — we only override two keys
        entry["name"] = plugin["prefixed_name"]
        entry["source"] = f"./plugins/{plugin['prefixed_name']}"
        # Always honor the cached version on the aggregated manifest — the
        # plugin_dir on disk might have drifted if sync fetched a new commit
        # after marketplace_plugins was written, but this is the authoritative
        # record.
        if plugin.get("version") and "version" not in entry:
            entry["version"] = plugin["version"]
        entries.append(entry)
    return {
        "name": MARKETPLACE_NAME,
        "owner": MARKETPLACE_OWNER,
        "metadata": {
            "description": MARKETPLACE_DESCRIPTION,
            "version": etag,
        },
        "plugins": entries,
    }


def build_info(conn: duckdb.DuckDBPyConnection, user: dict) -> Dict[str, Any]:
    """Return a JSON-serializable summary for diagnostic / admin endpoints.

    Mirrors the PoC's /marketplace/info contract.
    """
    plugins = marketplace_filter.resolve_allowed_plugins(conn, user)
    etag = marketplace_filter.compute_etag(plugins)
    return {
        "user_id": user.get("id"),
        "email": user.get("email"),
        "groups": marketplace_filter.resolve_user_groups(conn, user),
        "marketplace_name": MARKETPLACE_NAME,
        "etag": etag,
        "plugin_count": len(plugins),
        "plugins": [
            {
                "name": p["prefixed_name"],
                "original_name": p["original_name"],
                "marketplace_slug": p["marketplace_slug"],
                "version": p.get("version"),
                "description": p["raw"].get("description"),
            }
            for p in plugins
        ],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _collect_members(plugins: List[dict], etag: str) -> List[Tuple[str, bytes]]:
    """Collect (arcname, bytes) pairs for everything that goes into the ZIP.

    Intentionally returns unsorted — caller sorts for deterministic order.
    """
    members: List[Tuple[str, bytes]] = []
    manifest = _merged_manifest(plugins, etag)
    members.append(
        (
            ".claude-plugin/marketplace.json",
            json.dumps(manifest, indent=2, sort_keys=False).encode("utf-8"),
        )
    )

    for plugin in plugins:
        plugin_dir = plugin["plugin_dir"]
        if not plugin_dir.is_dir():
            continue
        for f in sorted(p for p in plugin_dir.rglob("*") if p.is_file()):
            rel = f.relative_to(plugin_dir).as_posix()
            arc = f"plugins/{plugin['prefixed_name']}/{rel}"
            members.append((arc, f.read_bytes()))

    return members


def _write_zip_entry(zf: zipfile.ZipFile, arcname: str, data: bytes) -> None:
    info = zipfile.ZipInfo(filename=arcname, date_time=DETERMINISTIC_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


def _etag_cache_key(plugins: List[dict]) -> tuple:
    return tuple(
        sorted(
            (p["prefixed_name"], p.get("version") or "", str(p["plugin_dir"]))
            for p in plugins
        )
    )


def compute_etag_for_user(
    conn: duckdb.DuckDBPyConnection, user: dict
) -> Tuple[str, List[dict]]:
    """Resolve the user's allowed plugins and compute their content-addressed
    ETag, without doing any file collection or ZIP assembly.

    Returns (etag, plugins) so callers that proceed to build_zip can reuse
    the resolved plugin set and skip the second DB query.
    """
    plugins = marketplace_filter.resolve_allowed_plugins(conn, user)
    if _ETAG_CACHE is None:
        return marketplace_filter.compute_etag(plugins), plugins
    cache_key = _etag_cache_key(plugins)
    with _ETAG_CACHE_LOCK:
        cached = _ETAG_CACHE.get(cache_key)
    if cached is not None:
        return cached, plugins
    etag = marketplace_filter.compute_etag(plugins)
    with _ETAG_CACHE_LOCK:
        _ETAG_CACHE[cache_key] = etag
    return etag, plugins


def invalidate_etag_cache() -> None:
    """Drop all cached etags. Called by marketplace sync after refresh so the
    next request re-hashes against the new on-disk content instead of waiting
    for TTL expiry."""
    if _ETAG_CACHE is None:
        return
    with _ETAG_CACHE_LOCK:
        _ETAG_CACHE.clear()


def build_zip(
    conn: duckdb.DuckDBPyConnection,
    user: dict,
    *,
    plugins: Optional[List[dict]] = None,
    etag: Optional[str] = None,
) -> Tuple[bytes, str]:
    """Build the deterministic ZIP for this user. Returns (bytes, etag).

    The `.agnes/version.json` entry carries `generated_at` for diagnostics and
    therefore makes the ZIP non-byte-identical on every request. That's fine
    for the ZIP channel (the ETag gate is computed from content hashes *before*
    that file is added). The git channel uses file_set_for_user() instead,
    which deliberately omits this diagnostic file.

    Callers that already resolved plugins + etag (e.g. the router after an
    If-None-Match miss) pass them as kwargs so we don't redo the work.
    """
    if plugins is None or etag is None:
        etag, plugins = compute_etag_for_user(conn, user)

    members = _collect_members(plugins, etag)

    version_payload = {
        "user_id": user.get("id"),
        "email": user.get("email"),
        "groups": marketplace_filter.resolve_user_groups(conn, user),
        "marketplace_name": MARKETPLACE_NAME,
        "etag": etag,
        "plugin_count": len(plugins),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    members.append(
        (
            ".agnes/version.json",
            json.dumps(version_payload, indent=2, sort_keys=True).encode("utf-8"),
        )
    )

    members.sort(key=lambda m: m[0])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arc, data in members:
            _write_zip_entry(zf, arc, data)
    return buf.getvalue(), etag
