"""Resolver: authenticated user → allowed plugins across all marketplaces.

The marketplace endpoint aggregates plugins from every registered marketplace
and returns only those the caller is allowed to see. Access is resolved
uniformly through ``resource_grants`` (resource_type='marketplace_plugin'):
the caller sees the distinct plugins granted to any of their groups. There
is no implicit Everyone membership and no god-mode shortcut for the
marketplace feed — admins curate their own view by granting plugins to a
group they belong to (Admin or otherwise).

Two distinct identifiers travel through the resolver:

- ``prefixed_name`` (``<slug>-<plugin_name>``) drives the on-disk directory
  layout in the served ZIP / git tree (``plugins/<prefixed_name>/...``) so
  two marketplaces shipping a same-named plugin don't overwrite each other's
  files.
- ``manifest_name`` (read from the plugin's own
  ``.claude-plugin/plugin.json`` ``name`` field, with a fallback to the
  upstream marketplace.json ``name``) is what the synth marketplace.json's
  ``name`` field uses. Claude Code's ``/plugin`` UI resolves a loaded plugin
  back to its catalog entry by ``plugin.json`` ``name``, so the catalog
  entry must match — anything else and the Components panel renders
  "Plugin <X> not found in marketplace".

Same-named plugins from two upstream marketplaces therefore collide in the
served catalog by design; admin RBAC (which grants survive the filter)
decides which one wins, identical to Claude Code's behavior when a user
adds two upstream marketplaces with overlapping plugin names directly.

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
from app.utils import get_marketplaces_dir, get_store_dir
from src.repositories import (
    marketplace_plugins_repo,
    resource_grants_repo,
    user_curated_subscriptions_repo,
    user_groups_repo,
    user_store_installs_repo,
)


def required_plugin_keys(conn: duckdb.DuckDBPyConnection | None, user_id: str | None) -> set[tuple[str, str]]:
    """``(marketplace_id, plugin_name)`` keys held at the ``required`` tier
    by any of the user's groups.

    v49 gave ``resource_grants`` a ``requirement`` enum
    (``available`` | ``required``) where required is the always-in-stack
    tier: the StackResolver unions required ids into the effective stack
    without an explicit subscription row. Marketplace plugins keep their
    own resolver (design D1), so the same union has to be applied here —
    ``resolve_user_marketplace`` serves
    ``(rbac ∩ (subscriptions ∪ required)) ∪ store_installs``. Before this
    helper existed, flipping a marketplace_plugin grant to ``required``
    was a silent no-op for the served set (the separate global
    ``is_system`` flag was the only mandatory path).

    ``resource_id`` format is ``<marketplace_slug>/<plugin_name>``; rows
    without a slash are skipped defensively so a hand-written grant can
    never crash the serve path. Reads go through the repo factory (not
    raw SQL on ``conn``) for the same PG-backend reason documented in
    ``resolve_allowed_plugins``.
    """
    group_ids = _user_group_ids(user_id, conn) if user_id else set()
    if not group_ids:
        return set()
    rows = resource_grants_repo().list_for_groups(list(group_ids), "marketplace_plugin")
    keys: set[tuple[str, str]] = set()
    for r in rows:
        if (r.get("requirement") or "available") != "required":
            continue
        resource_id = r.get("resource_id") or ""
        if "/" not in resource_id:
            continue
        slug, _, name = resource_id.partition("/")
        keys.add((slug, name))
    return keys


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


def resolve_manifest_name(plugin_dir: Path, fallback: str) -> str:
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


def resolve_allowed_plugins(conn: duckdb.DuckDBPyConnection, user: dict) -> List[dict]:
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
    #
    # Reads must go through the repo factory rather than raw SQL on the
    # DuckDB-typed ``conn`` parameter — on Postgres-backed deployments
    # ``marketplace_plugins`` / ``resource_grants`` / ``marketplace_registry``
    # rows live in PG; a raw ``conn.execute`` would hit the empty DuckDB
    # tables and silently exclude every plugin from the served set. The
    # symptom was missing plugins for users whose groups hadn't changed
    # (i.e. it wasn't stale Google sync) — the JOIN simply returned 0
    # rows because it was running against an empty DuckDB. The repo's
    # PG mirror runs the same logical JOIN against the live engine.
    group_ids = _user_group_ids(user_id, conn) if user_id else set()
    if not group_ids:
        return []
    rows = marketplace_plugins_repo().list_granted_for_groups(group_ids)

    result: List[dict] = []
    for row in rows:
        marketplace_id = row["marketplace_id"]
        name = row["name"]
        slug = marketplace_id  # registry.id IS the slug (see src/marketplace.py)
        plugin_dir = root / slug / "plugins" / name
        result.append(
            {
                "marketplace_id": marketplace_id,
                "marketplace_slug": slug,
                "original_name": name,
                "prefixed_name": _prefixed_name(slug, name),
                "manifest_name": resolve_manifest_name(plugin_dir, fallback=name),
                "version": row.get("version"),
                "raw": _resolve_raw(row.get("raw")),
                "plugin_dir": plugin_dir,
            }
        )
    return result


STORE_MARKETPLACE_ID = "store"
"""Sentinel slug used for Store-derived plugin entries in the served
marketplace. ``is_valid_slug`` in ``src/marketplace.py`` rejects any admin
marketplace registering ``store`` as its slug, so collisions with admin
content are impossible."""

BUNDLE_PLUGIN_NAME = "flea"
"""Synth plugin that wraps every Store-installed skill and agent for a user
into a single Claude Code plugin. Skill / agent uploads share this single
plugin in the served marketplace; only ``type='plugin'`` Store entities
materialize as their own plugin entry. See ``resolve_user_marketplace``.

v49 phase-4: renamed from ``agnes-store-bundle`` to ``flea``. Clean cut —
``usage_events`` rows whose JSONL was written before the rename stay
attributed as ``source='builtin'``; no legacy-prefix fallback in the
attribution layer (``services/session_processors/usage_lib.py``)."""

BUNDLE_PREFIXED_NAME = "flea"
"""On-disk directory name in the served ZIP / git tree for the bundle plugin.
Lives under ``plugins/flea/...``. v49 phase-4: renamed from ``store-bundle``
for parity with the manifest plugin name."""

BUNDLE_DESCRIPTION = "Skills and agents you've installed from the Agnes Store"

# Files we strip from the per-entity tree when we merge it into the bundle —
# each entity's plugin.json is replaced by a single bundle plugin.json that
# the bundle synth-emits.
_BUNDLE_EXCLUDE_DIR = ".claude-plugin"


def is_agnes_only_path(rel_parts: tuple[str, ...]) -> bool:
    """True when a relative path inside a plugin / bundle source is Agnes-only.

    Two patterns covered:

    * any segment named ``.agnes`` — convention dir for cover photos and
      docs that should NEVER reach the synth Claude Code marketplace,
    * file named ``marketplace-metadata.json`` at any depth (typically lives at
      ``.claude-plugin/marketplace-metadata.json`` at the repo root, but this
      catches it wherever it appears).

    Used by both the synth ZIP/git tree assembly path and the ETag
    computation so adding/removing Agnes-only content never invalidates
    user-side caches.
    """
    if not rel_parts:
        return False
    if any(part == ".agnes" for part in rel_parts):
        return True
    if rel_parts[-1] == "marketplace-metadata.json":
        return True
    return False


def _bundle_files(bundle_dirs: list[Path]) -> list[tuple[str, Path]]:
    """Return [(relpath_in_bundle, abs_path)] for every file across the bundle
    sources, dropping each per-entity ``.claude-plugin/`` content (the bundle
    has its own synth plugin.json) AND any Agnes-only file (``.agnes/**`` or
    ``marketplace-metadata.json``) so the served Claude Code marketplace stays
    clean of Agnes-side enrichment."""
    out: list[tuple[str, Path]] = []
    for src in bundle_dirs:
        if not src.is_dir():
            continue
        for f in sorted(p for p in src.rglob("*") if p.is_file()):
            rel_parts = f.relative_to(src).parts
            if rel_parts and rel_parts[0] == _BUNDLE_EXCLUDE_DIR:
                continue
            if is_agnes_only_path(rel_parts):
                continue
            out.append((Path(*rel_parts).as_posix(), f))
    return out


def _compute_bundle_version(bundle_dirs: list[Path]) -> str:
    """sha256[:16] of the bundle's bytes — bumps every install / uninstall
    /upload-update so Claude Code's plugin auto-update toggle picks up the
    change. Independent of the marketplace ETag."""
    h = hashlib.sha256()
    for rel, abs_path in _bundle_files(bundle_dirs):
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(abs_path.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()[:16]


def resolve_user_marketplace(conn: duckdb.DuckDBPyConnection, user: dict) -> List[dict]:
    """Final, served plugin set for a user.

    Composition::

        (admin_granted ∩ (subscriptions ∪ required)) ∪ store_installs

    Output entries match ``resolve_allowed_plugins`` shape so that the
    existing ``packager`` / ``git_backend`` machinery iterates them
    transparently. Entries carry an extra ``source`` key (``"marketplace"``
    or ``"store"``) used by ``build_info`` for diagnostics — packager /
    git_backend ignore it.

    Ordering is deterministic: admin entries first (by registration time +
    name), then Store entries (by entity id) — so two requests with the same
    inputs produce byte-identical ZIPs and git commits.
    """
    user_id = user.get("id")
    if not user_id:
        return []

    admin = resolve_allowed_plugins(conn, user)

    # Model B (v28+): RBAC grant is only eligibility — the user must explicitly
    # subscribe via /marketplace for a curated plugin to enter their served set.
    # Pre-v28 the filter was (rbac ∖ opt_outs); now it's (rbac ∩ in_stack),
    # where in_stack = subscriptions ∪ required-tier grant keys: a grant at
    # ``requirement='required'`` is always-in-stack for every group member,
    # matching the StackResolver union for data packages / memory domains.
    # Reads through the repo factory so subscriptions resolve correctly on
    # the active backend (PG / DuckDB) — same rationale as
    # ``resolve_allowed_plugins`` above.
    in_stack = user_curated_subscriptions_repo().subscribed_set(user_id) | required_plugin_keys(conn, user_id)
    admin = [p for p in admin if (p["marketplace_id"], p["original_name"]) in in_stack]
    for p in admin:
        p["source"] = "marketplace"

    store_root = get_store_dir()
    installs = user_store_installs_repo().list_for_user(user_id)
    store_plugin_entries: List[dict] = []
    bundle_rows: List[dict] = []
    for row in installs:
        if row.get("type") in ("skill", "agent"):
            bundle_rows.append(row)
            continue

        # type == 'plugin' (or any other future type that ships its own
        # plugin tree) gets its own entry — Claude Code shows them as
        # standalone plugins. all-or-nothing per spec.
        entity_id = row["id"]
        owner_username = row["owner_username"]
        original_name = row["name"]
        # v49 phase-3: stored synthetic_name from store_entities is the
        # canonical value baked into the on-disk plugin tree
        # (frontmatter name, plugin.json `name`). Reading it from DB
        # keeps the served manifest in lockstep with whatever the upload
        # / edit / archive paths last wrote. The column is NOT NULL +
        # explicitly selected by ``list_for_user``.
        manifest_name = row["synthetic_name"]
        plugin_dir = store_root / entity_id / "plugin"
        store_plugin_entries.append(
            {
                "marketplace_id": STORE_MARKETPLACE_ID,
                "marketplace_slug": STORE_MARKETPLACE_ID,
                "original_name": original_name,
                # Use entity_id (UUID-ish) for the on-disk dir prefix so two
                # owners uploading "code-review" never collide. The ZIP /
                # git tree groups them under plugins/store-<entity_id>/.
                "prefixed_name": f"store-{entity_id}",
                "manifest_name": manifest_name,
                "version": row.get("version"),
                "raw": {
                    "name": manifest_name,
                    "version": row.get("version"),
                    "description": row.get("description") or "",
                },
                "plugin_dir": plugin_dir,
                "source": "store",
                "entity_id": entity_id,
                "type": row.get("type"),
                "owner_username": owner_username,
            }
        )

    bundle_entry: List[dict] = []
    if bundle_rows:
        bundle_dirs = [store_root / r["id"] / "plugin" for r in bundle_rows]
        version = _compute_bundle_version(bundle_dirs)
        bundle_entry.append(
            {
                "marketplace_id": STORE_MARKETPLACE_ID,
                "marketplace_slug": STORE_MARKETPLACE_ID,
                "original_name": BUNDLE_PREFIXED_NAME,
                "prefixed_name": BUNDLE_PREFIXED_NAME,
                "manifest_name": BUNDLE_PLUGIN_NAME,
                "version": version,
                "raw": {
                    "name": BUNDLE_PLUGIN_NAME,
                    "version": version,
                    "description": BUNDLE_DESCRIPTION,
                },
                # No real on-disk root for the bundle — its content is
                # composed at serve time from `bundle_dirs`. packager /
                # git_backend / compute_etag check for `bundle_dirs` and
                # skip the usual `plugin_dir.is_dir()` path.
                "plugin_dir": None,
                "bundle_dirs": bundle_dirs,
                "source": "store-bundle",
                "bundle_entity_ids": [r["id"] for r in bundle_rows],
            }
        )

    return list(admin) + store_plugin_entries + bundle_entry


def resolve_user_groups(conn: duckdb.DuckDBPyConnection, user: dict) -> List[str]:
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
    # Repo factory routes to the active backend (PG / DuckDB) — same
    # rationale as ``resolve_allowed_plugins``: raw SQL on the
    # DuckDB-typed ``conn`` would return [] on Postgres deployments
    # because the ``user_groups`` rows live in PG.
    return user_groups_repo().list_names_by_ids(group_ids)


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

    For bundle entries (``bundle_dirs`` set, ``plugin_dir`` is None) we hash
    every file under each source dir except the per-entity ``.claude-plugin/``
    content; the bundle ships one synth plugin.json so the per-entity ones
    don't enter the served tree.
    """
    tokens: List[List[Any]] = []
    for plugin in plugins:
        files: List[List[str]] = []
        if plugin.get("bundle_dirs"):
            for rel, abs_path in _bundle_files(plugin["bundle_dirs"]):
                files.append([rel, _sha256_file(abs_path)])
        else:
            plugin_dir: Path = plugin["plugin_dir"]
            if plugin_dir is not None and plugin_dir.is_dir():
                for f in _iter_files(plugin_dir):
                    rel_parts = f.relative_to(plugin_dir).parts
                    if is_agnes_only_path(rel_parts):
                        continue
                    rel = f.relative_to(plugin_dir).as_posix()
                    files.append([rel, _sha256_file(f)])
        tokens.append([plugin["prefixed_name"], plugin.get("version") or "", files])
    payload = json.dumps({"plugins": tokens}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]
