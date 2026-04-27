"""Resolver: authenticated user → allowed plugins across all marketplaces.

The marketplace endpoint aggregates plugins from every registered marketplace
and returns only those the caller is allowed to see. Access is resolved
through ``resource_grants`` (resource_type='marketplace_plugin'):

    user in Admin group       → every plugin across all marketplaces
    otherwise                 → distinct plugins granted to any of the
                                user's groups (Everyone is implicit)

Plugins from different marketplaces that happen to share a name are NOT the
same plugin — the caller needs both. We therefore prefix every plugin name
with its marketplace slug (`<slug>-<plugin_name>`) when projecting out, so
the merged marketplace.json never has colliding entries.

resource_id format for ``marketplace_plugin`` grants is
``<marketplace_slug>/<plugin_name>`` — the slash is the canonical separator;
neither slugs nor plugin names contain slashes (both regex-constrained).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, List

import duckdb

from app.auth.access import _user_group_ids, is_user_admin
from app.resource_types import ResourceType
from app.utils import get_marketplaces_dir


def _resolve_raw(raw: Any) -> dict:
    """marketplace_plugins.raw is JSON — DuckDB may surface it as str or dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _prefixed_name(slug: str, plugin_name: str) -> str:
    """<slug>-<plugin_name>. Both fields are already regex-constrained to
    characters safe for a Claude Code plugin identifier."""
    return f"{slug}-{plugin_name}"


def resolve_allowed_plugins(
    conn: duckdb.DuckDBPyConnection, user: dict
) -> List[dict]:
    """Return the distinct, prefixed plugin list this user is allowed to install.

    Each entry:
        {
            "marketplace_id":   str,   # also the slug (they are the same)
            "marketplace_slug": str,
            "original_name":    str,
            "prefixed_name":    str,   # "<slug>-<original_name>"
            "version":          str | None,
            "raw":              dict,  # parsed marketplace.json plugin entry
            "plugin_dir":       Path,  # ${DATA_DIR}/marketplaces/<slug>/plugins/<name>
        }

    Ordering is deterministic: by marketplace registration time, then plugin
    name — so ETag / git commit hash stay stable as long as the underlying
    content is unchanged.
    """
    user_id = user.get("id")
    root = get_marketplaces_dir()

    if user_id and is_user_admin(user_id, conn):
        sql = (
            "SELECT mp.marketplace_id, mp.name, mp.version, mp.raw "
            "FROM marketplace_plugins mp "
            "JOIN marketplace_registry mr ON mr.id = mp.marketplace_id "
            "ORDER BY mr.registered_at, mp.name"
        )
        rows = conn.execute(sql).fetchall()
    else:
        # Distinct (marketplace_id, plugin_name) across all of the user's
        # groups (Everyone is implicit via _user_group_ids). If two groups
        # grant the same plugin, it still appears once.
        group_ids = _user_group_ids(user_id, conn) if user_id else set()
        if not group_ids:
            return []
        placeholders = ",".join(["?"] * len(group_ids))
        sql = (
            "SELECT DISTINCT mp.marketplace_id, mp.name, mp.version, mp.raw "
            "FROM resource_grants rg "
            "JOIN marketplace_plugins mp "
            "  ON mp.marketplace_id || '/' || mp.name = rg.resource_id "
            "JOIN marketplace_registry mr ON mr.id = mp.marketplace_id "
            f"WHERE rg.group_id IN ({placeholders}) "
            "  AND rg.resource_type = ? "
            "ORDER BY mr.registered_at, mp.name"
        )
        rows = conn.execute(
            sql, [*group_ids, ResourceType.MARKETPLACE_PLUGIN.value],
        ).fetchall()

    result: List[dict] = []
    for marketplace_id, name, version, raw in rows:
        slug = marketplace_id  # registry.id IS the slug (see src/marketplace.py)
        result.append(
            {
                "marketplace_id": marketplace_id,
                "marketplace_slug": slug,
                "original_name": name,
                "prefixed_name": _prefixed_name(slug, name),
                "version": version,
                "raw": _resolve_raw(raw),
                "plugin_dir": root / slug / "plugins" / name,
            }
        )
    return result


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_files(root: Path) -> List[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def compute_etag(plugins: Iterable[dict]) -> str:
    """Content-addressed ETag for the user's plugin view.

    Two users with the same allowed set share the same ETag — so they also
    share the same bare-repo cache entry. When the source files change, the
    hash changes, so a stale 304 can never leak.
    """
    tokens: List[List[Any]] = []
    for plugin in plugins:
        plugin_dir: Path = plugin["plugin_dir"]
        files: List[List[str]] = []
        if plugin_dir.is_dir():
            for f in _iter_files(plugin_dir):
                rel = f.relative_to(plugin_dir).as_posix()
                files.append([rel, _sha256_file(f)])
        tokens.append(
            [plugin["prefixed_name"], plugin.get("version") or "", files]
        )
    payload = json.dumps(
        {"plugins": tokens}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]
