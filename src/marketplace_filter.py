"""Resolver: authenticated user → allowed plugins across all marketplaces.

The marketplace endpoint aggregates plugins from every registered marketplace
and returns only those the caller is allowed to see. Access is resolved
uniformly through ``resource_grants`` (resource_type='marketplace_plugin'):
the caller sees the distinct plugins granted to any of their groups. There
is no implicit Everyone membership and no god-mode shortcut for the
marketplace feed — admins curate their own view by granting plugins to a
group they belong to (Admin or otherwise).

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

from app.auth.access import _user_group_ids
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


def _resolve_manifest_name(plugin_dir: Path, fallback: str) -> str:
    """Return the plugin's authoritative `name` from its `.claude-plugin/plugin.json`.

    Claude Code resolves a loaded plugin back to its marketplace catalog
    entry by the name declared in the plugin's own `plugin.json`. The synth
    `marketplace.json` we serve must use that same name, otherwise the
    `/plugin` UI Components panel can't link the loaded plugin to its
    catalog entry and renders "Plugin <X> not found in marketplace".

    Falls back to ``fallback`` (the upstream marketplace.json's plugin name)
    when plugin.json is missing, unreadable, has no string `name`, or has
    an empty/whitespace-only `name` — same defensive style as
    ``src.marketplace.read_plugins``: never crash, always return a usable
    value.
    """
    pj = plugin_dir / ".claude-plugin" / "plugin.json"
    if not pj.is_file():
        return fallback
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback
    if not isinstance(data, dict):
        return fallback
    name = data.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return fallback


def resolve_allowed_plugins(
    conn: duckdb.DuckDBPyConnection, user: dict
) -> List[dict]:
    """Return the distinct, prefixed plugin list this user is allowed to install.

    Each entry:
        {
            "marketplace_id":   str,   # also the slug (they are the same)
            "marketplace_slug": str,
            "original_name":    str,   # name from upstream marketplace.json
            "prefixed_name":    str,   # "<slug>-<original_name>" — drives
                                       # the on-disk dir layout in the ZIP /
                                       # git tree (cross-marketplace files
                                       # don't collide).
            "manifest_name":    str,   # name from the plugin's own
                                       # .claude-plugin/plugin.json (or
                                       # original_name fallback) — drives
                                       # the `name` field in the synth
                                       # marketplace.json we serve, so the
                                       # Claude Code UI's catalog lookup
                                       # matches the loaded plugin's
                                       # namespace.
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

    # Distinct (marketplace_id, plugin_name) across all of the user's
    # groups. If two groups grant the same plugin, it still appears
    # once. Admin is treated as a regular group — admins get only the
    # plugins their groups have been granted.
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
        plugin_dir = root / slug / "plugins" / name
        result.append(
            {
                "marketplace_id": marketplace_id,
                "marketplace_slug": slug,
                "original_name": name,
                "prefixed_name": _prefixed_name(slug, name),
                "manifest_name": _resolve_manifest_name(plugin_dir, fallback=name),
                "version": version,
                "raw": _resolve_raw(raw),
                "plugin_dir": plugin_dir,
            }
        )
    return result


def resolve_user_groups(
    conn: duckdb.DuckDBPyConnection, user: dict
) -> List[str]:
    """Return the names of groups this user belongs to, sorted alphabetically.

    Diagnostic only — the actual RBAC filtering of the marketplace feed is
    owned by ``resolve_allowed_plugins``. This helper backs the ``groups``
    field in ``/marketplace/info`` and in ``.agnes/version.json`` inside the
    ZIP, so an operator can read those payloads and answer "which groups
    granted me visibility into this plugin set?" without opening the admin UI.

    Membership semantics mirror ``app.auth.access._user_group_ids``:
    only real ``user_group_members`` rows are surfaced; there is no
    implicit Everyone membership.
    """
    user_id = user.get("id")
    if not user_id:
        return []
    group_ids = _user_group_ids(user_id, conn)
    if not group_ids:
        return []
    placeholders = ",".join(["?"] * len(group_ids))
    rows = conn.execute(
        f"SELECT name FROM user_groups "
        f"WHERE id IN ({placeholders}) ORDER BY name",
        list(group_ids),
    ).fetchall()
    return [r[0] for r in rows]


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
