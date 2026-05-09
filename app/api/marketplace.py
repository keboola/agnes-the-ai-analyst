"""Unified Marketplace API — backs the ``/marketplace`` browse page.

Combines two upstream sources into a single browse + detail surface:

  * **curated**  — admin-registered git marketplaces, RBAC-scoped via
    ``resource_grants`` (resource_type='marketplace_plugin').
  * **flea**     — community Store uploads (``store_entities``).

Per-tab listing endpoints (``/items``, ``/categories``) plus curated-side
detail and explicit-install (Model B) endpoints. Flea-side install/uninstall
keeps using the existing ``/api/store/entities/{id}/install`` POST/DELETE
endpoints — no need to duplicate.

Curated detail / install endpoints are gated by
``require_resource_access(MARKETPLACE_PLUGIN, "{marketplace_id}/{plugin_name}")``
so a user without the RBAC grant gets a 403 even on direct URL hit.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth.access import (
    _user_group_ids,
    require_resource_access,
)
from app.auth.dependencies import _get_db, get_current_user
from app.resource_types import ResourceType
from app.utils import get_marketplace_cache_dir, get_marketplaces_dir
from src.marketplace_filter import (
    resolve_allowed_plugins,
    resolve_manifest_name,
)
from src.repositories.audit import AuditRepository
from src.repositories.marketplace_plugins import MarketplacePluginsRepository
from src.repositories.marketplace_registry import MarketplaceRegistryRepository
from src.repositories.store_entities import StoreEntitiesRepository
from src.repositories.user_curated_subscriptions import (
    UserCuratedSubscriptionsRepository,
)
from src.repositories.user_store_installs import UserStoreInstallsRepository
from src.store_categories import STORE_CATEGORIES
from src.store_naming import suffixed_name

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])

OWNER_TODO_PLACEHOLDER = "owner_todo"
"""Placeholder displayed in the UI when a curated plugin has no owner / curator
metadata. To be replaced once ``marketplace_plugins.curator_owner`` lands."""

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class MarketplaceItem(BaseModel):
    id: str
    source: Literal["curated", "flea"]
    name: str
    type: Literal["skill", "agent", "plugin"]
    category: Optional[str] = None
    description: Optional[str] = None
    owner: Optional[str] = None
    version: Optional[str] = None
    photo_url: Optional[str] = None
    added: Optional[str] = None
    installed: bool = False
    marketplace_slug: Optional[str] = None
    marketplace_name: Optional[str] = None
    detail_url: str


class ItemListResponse(BaseModel):
    items: List[MarketplaceItem]
    total: int
    page: int
    page_size: int


class CategoryEntry(BaseModel):
    name: str
    count: int
    icon_key: str


class CategoriesResponse(BaseModel):
    items: List[CategoryEntry]


class InnerItemSummary(BaseModel):
    name: str
    description: Optional[str] = None
    detail_url: Optional[str] = None  # nested detail for skill/agent only
    # v32: agnes-metadata-driven cover photo for the skill/agent card on the
    # parent plugin detail page. Already in served-URL form (internal /asset/
    # endpoint, mirrored /mirrored/ endpoint, or pass-through external URL).
    # None → frontend renders initials placeholder ("SK" / "AG").
    cover_photo_url: Optional[str] = None


class CommandEntry(BaseModel):
    name: str
    description: Optional[str] = None


class HookEntry(BaseModel):
    name: str
    event: Optional[str] = None  # "—" rendered client-side when None
    description: Optional[str] = None


class McpEntry(BaseModel):
    name: str
    type: Optional[str] = None  # "stdio" / "sse" / etc.
    description: Optional[str] = None


class FileEntry(BaseModel):
    """One file in a plugin / skill / agent bundle. Path is relative to the
    bundle root; size is bytes (rendered with ``humanbytes`` on the client)."""
    path: str
    size: int


class DocEntry(BaseModel):
    """One doc file shipped alongside a Store entity. URL points at the
    serving endpoint (``/api/store/entities/<id>/docs/<filename>``)."""
    name: str
    url: str


class PluginDetailResponse(BaseModel):
    """Unified detail response for a plugin (curated *or* flea).

    Frontend-side switching is purely cosmetic — `source` controls the
    Curated/Flea pill, photo URL fallback path, and owner label.
    """
    source: Literal["curated", "flea"]
    # IDs / breadcrumbs
    marketplace_id: Optional[str] = None        # curated only
    marketplace_name: Optional[str] = None      # curated only
    entity_id: Optional[str] = None             # flea only
    plugin_name: str
    manifest_name: str
    # Display
    description: Optional[str] = None
    version: Optional[str] = None
    category: Optional[str] = None
    author_name: Optional[str] = None           # curated curator / flea owner
    curator_email: Optional[str] = None         # curated only — surfaced for "contact curator"
    owner_display: Optional[str] = None         # flea: users.name → email → owner_username
    homepage: Optional[str] = None
    cover_photo_url: Optional[str] = None       # /api/store/.../photo for flea, agnes-metadata for curated
    video_url: Optional[str] = None             # v32: external (YouTube/Vimeo/Loom) embed URL
    bundle_size: Optional[int] = None           # bytes; None when unknown
    install_count: int = 0                      # flea only; curated leaves at 0
    released_at: Optional[str] = None           # ISO timestamp
    updated_at: Optional[str] = None            # ISO timestamp
    installed: bool = False
    # Internal structure
    skills: List[InnerItemSummary] = []
    agents: List[InnerItemSummary] = []
    commands: List[CommandEntry] = []
    hooks: List[HookEntry] = []
    mcps: List[McpEntry] = []
    # Bundle contents — used by the redesigned skill/agent/plugin detail pages
    files: List[FileEntry] = []
    docs: List[DocEntry] = []


# Legacy alias kept so any unmigrated import continues to resolve. The new
# unified model supersedes the curated-only response shape.
CuratedDetailResponse = PluginDetailResponse


class InnerDetailResponse(BaseModel):
    marketplace_id: str
    plugin_name: str
    kind: Literal["skill", "agent"]
    name: str
    description: Optional[str] = None
    body: str
    relpath: str  # path inside plugin_dir for diagnostics
    # Parent plugin metadata + bundle contents — populated so the redesigned
    # skill/agent detail page can render hero badges, sidebar rows, and the
    # Files section without a second roundtrip to the parent plugin endpoint.
    marketplace_name: str = ""
    category: Optional[str] = None
    parent_author_name: Optional[str] = None
    parent_updated_at: Optional[str] = None
    # Parent plugin's `name` from `.claude-plugin/plugin.json` — same value
    # the synth marketplace.json uses, so /<manifest_name>:<inner_name> is
    # exactly what Claude Code accepts after install.
    manifest_name: str = ""
    bundle_size: Optional[int] = None
    files: List[FileEntry] = []
    # v32: per-skill / per-agent enrichment from agnes-metadata.json sub-tree.
    # Read at request time from the cloned working tree (not cached in DB) so
    # curators can update one inner asset without paying the cost of a full
    # plugin-cache rewrite. Three-typed cover URL layout matches plugin
    # detail: internal → /asset/, mirror status not yet checked here so
    # external URLs pass through unmirrored (we don't run sync mid-request).
    cover_photo_url: Optional[str] = None
    video_url: Optional[str] = None
    docs: List[DocEntry] = []


class InstallActionResponse(BaseModel):
    installed: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    target: str,
    params: Optional[dict] = None,
) -> None:
    try:
        AuditRepository(conn).log(
            user_id=actor_id, action=action, resource=target, params=params
        )
    except Exception:
        pass


def _invalidate_etag() -> None:
    try:
        from app.marketplace_server import packager
        packager.invalidate_etag_cache()
    except Exception:
        logger.exception("failed to invalidate marketplace etag cache")


def _parse_frontmatter(text: str) -> Dict[str, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: Dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _frontmatter_body(text: str) -> str:
    m = _FRONTMATTER_RE.match(text)
    return text[m.end():].lstrip("\n") if m else text


def _to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def _icon_key_for(category: Optional[str]) -> str:
    """Stable key used by the frontend to look up the inline SVG icon
    (registered in ``src/category_icons.py``). Matches our taxonomy 1:1."""
    return category or "Other"


def _curated_detail_url(marketplace_id: str, plugin_name: str) -> str:
    return f"/marketplace/curated/{marketplace_id}/{plugin_name}"


def _flea_detail_url(entity_id: str) -> str:
    return f"/marketplace/flea/{entity_id}"


def _resolve_marketplace_meta(
    conn: duckdb.DuckDBPyConnection, marketplace_id: str
) -> Dict[str, Optional[str]]:
    """Look up display name + curator metadata in one DB hit.

    Returns a dict with keys ``name``, ``curator_name``, ``curator_email``.
    Missing rows fall back to the marketplace_id as ``name`` and ``None``
    for the curator fields. Caller decides what to do with the absence
    (typically: surface the ``OWNER_TODO_PLACEHOLDER``).
    """
    row = MarketplaceRegistryRepository(conn).get(marketplace_id)
    if not row:
        return {
            "name": marketplace_id,
            "curator_name": None,
            "curator_email": None,
        }
    name = (row.get("name") or "").strip() or marketplace_id
    return {
        "name": name,
        "curator_name": (row.get("curator_name") or "").strip() or None,
        "curator_email": (row.get("curator_email") or "").strip() or None,
    }


def _curated_to_item(
    conn: duckdb.DuckDBPyConnection,
    plugin_row: dict,
    *,
    subs: set,
    marketplace_meta: Dict[str, Dict[str, Optional[str]]],
) -> MarketplaceItem:
    marketplace_id = plugin_row["marketplace_id"]
    plugin_name = plugin_row["name"]
    meta = marketplace_meta.get(marketplace_id) or {
        "name": marketplace_id,
        "curator_name": None,
        "curator_email": None,
    }
    # v32: card "owner" is the curator from the marketplace registry — not the
    # upstream `marketplace.json::author.name` we historically used. Curator
    # is the human accountable for the plugin's presence in this Agnes
    # instance; upstream author belongs in the plugin source repo.
    owner = meta["curator_name"] or OWNER_TODO_PLACEHOLDER
    return MarketplaceItem(
        id=f"curated-{marketplace_id}/{plugin_name}",
        source="curated",
        name=plugin_name,
        type="plugin",
        category=plugin_row.get("category") or None,
        description=plugin_row.get("description"),
        owner=owner,
        version=plugin_row.get("version"),
        # Cover photo URL is already in served form (internal `/asset/`,
        # mirrored `/mirrored/`, or pass-through external URL) — see
        # src.marketplace._refresh_plugin_cache for the resolution path.
        photo_url=plugin_row.get("cover_photo_url"),
        added=_to_iso(plugin_row.get("created_at")),
        installed=(marketplace_id, plugin_name) in subs,
        marketplace_slug=marketplace_id,
        marketplace_name=meta["name"],
        detail_url=_curated_detail_url(marketplace_id, plugin_name),
    )


def _flea_to_item(
    entity: dict, *, installed_set: set
) -> MarketplaceItem:
    photo_url = (
        f"/api/store/entities/{entity['id']}/photo"
        if entity.get("photo_path") else None
    )
    invocation = suffixed_name(entity["name"], entity.get("owner_username") or "")
    return MarketplaceItem(
        id=f"flea-{entity['id']}",
        source="flea",
        name=invocation,
        type=entity["type"],
        category=entity.get("category") or None,
        description=entity.get("description"),
        owner=entity.get("owner_username"),
        version=entity.get("version"),
        photo_url=photo_url,
        added=_to_iso(entity.get("created_at")),
        installed=entity["id"] in installed_set,
        marketplace_slug=None,
        marketplace_name=None,
        detail_url=_flea_detail_url(entity["id"]),
    )


# ---------------------------------------------------------------------------
# GET /api/marketplace/items
# ---------------------------------------------------------------------------


@router.get("/items", response_model=ItemListResponse)
async def list_items(
    tab: Literal["curated", "flea", "my"] = Query("curated"),
    q: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    type: Optional[Literal["skill", "agent", "plugin"]] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Paginated, RBAC-scoped item list for the ``/marketplace`` browse page.

    Per-tab dispatch:

    * ``curated`` → ``MarketplacePluginsRepository.list_with_filters`` scoped to
      the caller's ``user_group_members``. Results are tagged with the
      caller's subscription state.
    * ``flea`` → ``StoreEntitiesRepository.list`` (community Store).
    * ``my`` → direct read from ``user_curated_subscriptions`` (filtered
      against the caller's RBAC-allowed plugins) ∪ ``user_store_installs``
      (all flea types — skill / agent / plugin — surface as individual
      cards). We deliberately don't reuse ``resolve_user_marketplace``
      here: that resolver bundles flea skills/agents into a single
      synthetic ``store-bundle`` entry useful for the served Claude Code
      marketplace ZIP/git endpoints but wrong for /marketplace browsing,
      where each item should appear as its own card. Mirrors the
      ``/api/my-stack`` reading pattern.
    """
    skip = (page - 1) * page_size

    if tab == "curated":
        group_ids = _user_group_ids(user["id"], conn) or set()
        subs = UserCuratedSubscriptionsRepository(conn).subscribed_set(user["id"])
        rows, total = MarketplacePluginsRepository(conn).list_with_filters(
            group_ids=group_ids,
            search=q or None,
            category=category or None,
            skip=skip,
            limit=page_size,
        )
        marketplace_meta: Dict[str, Dict[str, Optional[str]]] = {}
        if rows:
            distinct_ids = {r["marketplace_id"] for r in rows}
            for mp_id in distinct_ids:
                marketplace_meta[mp_id] = _resolve_marketplace_meta(conn, mp_id)
        items = [
            _curated_to_item(conn, r, subs=subs, marketplace_meta=marketplace_meta)
            for r in rows
        ]
        return ItemListResponse(
            items=items, total=total, page=page, page_size=page_size,
        )

    if tab == "flea":
        installed_set = {
            row["id"]
            for row in UserStoreInstallsRepository(conn).list_for_user(user["id"])
        }
        rows, total = StoreEntitiesRepository(conn).list(
            skip=skip, limit=page_size,
            type=type, category=category, search=q or None,
        )
        items = [_flea_to_item(r, installed_set=installed_set) for r in rows]
        return ItemListResponse(
            items=items, total=total, page=page, page_size=page_size,
        )

    # tab == "my" — see docstring; read directly from source-of-truth tables.
    items: List[MarketplaceItem] = []

    granted = resolve_allowed_plugins(conn, user)
    subs = UserCuratedSubscriptionsRepository(conn).subscribed_set(user["id"])
    marketplace_meta: Dict[str, Dict[str, Optional[str]]] = {}
    for p in granted:
        if (p["marketplace_id"], p["original_name"]) not in subs:
            continue
        mp_id = p["marketplace_id"]
        if mp_id not in marketplace_meta:
            marketplace_meta[mp_id] = _resolve_marketplace_meta(conn, mp_id)
        # `resolve_allowed_plugins` reads the upstream marketplace.json, so it
        # doesn't see our agnes-metadata enrichment columns. The /my-stack
        # surface still works because the curated card shape only needs
        # category + description + photo from this synthetic row — none of
        # which depend on agnes-metadata in steady state. Cover photos here
        # fall through to the gradient placeholder until the user re-visits
        # the curated browse tab (which goes through MarketplacePluginsRepository
        # and gets the enriched cover_photo_url).
        author = p["raw"].get("author")
        plugin_row = {
            "marketplace_id": mp_id,
            "name": p["original_name"],
            "description": p["raw"].get("description"),
            "version": p.get("version"),
            "category": p["raw"].get("category"),
            "author_name": author.get("name") if isinstance(author, dict) else None,
            "cover_photo_url": None,
            "created_at": None,
        }
        items.append(_curated_to_item(
            conn, plugin_row, subs=subs, marketplace_meta=marketplace_meta,
        ))

    flea_installs = UserStoreInstallsRepository(conn).list_for_user(user["id"])
    flea_installed_set = {row["id"] for row in flea_installs}
    for entity in flea_installs:
        items.append(_flea_to_item(entity, installed_set=flea_installed_set))

    # Apply optional filters client-server-style for `my` tab (small N):
    if q:
        needle = q.lower()
        items = [
            it for it in items
            if needle in it.name.lower()
            or needle in (it.description or "").lower()
            or needle in (it.owner or "").lower()
            or needle in (it.category or "").lower()
        ]
    if category:
        items = [it for it in items if (it.category or "Other") == category]
    if type:
        items = [it for it in items if it.type == type]
    total = len(items)
    items = items[skip : skip + page_size]
    return ItemListResponse(
        items=items, total=total, page=page, page_size=page_size,
    )


# ---------------------------------------------------------------------------
# GET /api/marketplace/categories
# ---------------------------------------------------------------------------


@router.get("/categories", response_model=CategoriesResponse)
async def list_categories(
    tab: Literal["curated", "flea", "my"] = Query("curated"),
    type: Optional[Literal["skill", "agent", "plugin"]] = Query(None),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Per-tab category list with non-zero counts.

    Source of categories: union of ``STORE_CATEGORIES`` and any non-empty
    ``marketplace_plugins.category`` values in the caller's RBAC scope.
    Categories with zero matching items are omitted (the frontend hides
    them this way).
    """
    counts: Dict[str, int] = {}

    if tab in ("curated", "my"):
        group_ids = _user_group_ids(user["id"], conn) or set()
        if tab == "curated":
            counts.update(
                MarketplacePluginsRepository(conn).category_counts(
                    group_ids=group_ids
                )
            )
        else:  # my — direct read mirroring the items endpoint's `my` branch.
            granted = resolve_allowed_plugins(conn, user)
            subs = UserCuratedSubscriptionsRepository(conn).subscribed_set(user["id"])
            for p in granted:
                if (p["marketplace_id"], p["original_name"]) not in subs:
                    continue
                cat = (p["raw"].get("category") or "").strip() or "Other"
                counts[cat] = counts.get(cat, 0) + 1
            for row in UserStoreInstallsRepository(conn).list_for_user(user["id"]):
                if type and row.get("type") != type:
                    continue
                cat = (row.get("category") or "").strip() or "Other"
                counts[cat] = counts.get(cat, 0) + 1

    if tab == "flea":
        rows = conn.execute(
            "SELECT COALESCE(NULLIF(TRIM(category),''), 'Other') AS cat, COUNT(*) "
            "FROM store_entities "
            + ("WHERE type = ? " if type else "")
            + "GROUP BY cat",
            ([type] if type else []),
        ).fetchall()
        for r in rows:
            counts[str(r[0])] = counts.get(str(r[0]), 0) + int(r[1])

    items = [
        CategoryEntry(name=name, count=count, icon_key=_icon_key_for(name))
        for name, count in sorted(counts.items())
        if count > 0
    ]
    return CategoriesResponse(items=items)


# ---------------------------------------------------------------------------
# Curated detail + install endpoints
# ---------------------------------------------------------------------------


def _list_inner_skills(plugin_root: Path) -> List[InnerItemSummary]:
    out: List[InnerItemSummary] = []
    skills_dir = plugin_root / "skills"
    if not skills_dir.is_dir():
        return out
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        out.append(InnerItemSummary(
            name=fm.get("name") or skill_dir.name,
            description=fm.get("description"),
            detail_url=None,  # populated by caller (needs marketplace_id + plugin_name)
        ))
    return out


def _list_inner_agents(plugin_root: Path) -> List[InnerItemSummary]:
    out: List[InnerItemSummary] = []
    agents_dir = plugin_root / "agents"
    if not agents_dir.is_dir():
        return out
    for agent_path in sorted(agents_dir.iterdir()):
        if not agent_path.is_file() or agent_path.suffix != ".md":
            continue
        try:
            text = agent_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        out.append(InnerItemSummary(
            name=fm.get("name") or agent_path.stem,
            description=fm.get("description"),
            detail_url=None,
        ))
    return out


def _list_commands(plugin_root: Path) -> List[CommandEntry]:
    """Return ``commands/*.md`` as ``[(name, description)]`` from frontmatter."""
    d = plugin_root / "commands"
    if not d.is_dir():
        return []
    out: List[CommandEntry] = []
    for p in sorted(d.iterdir()):
        if not p.is_file() or p.suffix != ".md":
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        out.append(CommandEntry(
            name="/" + (fm.get("name") or p.stem),
            description=fm.get("description"),
        ))
    return out


def _read_plugin_json(plugin_root: Path) -> dict:
    """Best-effort load of the plugin's own .claude-plugin/plugin.json."""
    pj = plugin_root / ".claude-plugin" / "plugin.json"
    if not pj.is_file():
        return {}
    try:
        import json
        data = json.loads(pj.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _list_hooks(plugin_root: Path) -> List[HookEntry]:
    """Combine hook declarations from plugin.json with files in ``hooks/``.

    Two declaration paths exist:
      1. ``plugin.json`` ``hooks`` field — ``{<EventName>: [{matcher, hooks: [...]}]}``
      2. Bare scripts in ``<plugin_root>/hooks/`` — event unknown, rendered
         as ``"—"`` so the table still surfaces them.
    """
    out: List[HookEntry] = []
    seen_names: set[str] = set()

    # Path 1: structured declaration in plugin.json.
    data = _read_plugin_json(plugin_root)
    hooks_field = data.get("hooks") if isinstance(data, dict) else None
    if isinstance(hooks_field, dict):
        for event, entries in hooks_field.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                inner = entry.get("hooks") or []
                if not isinstance(inner, list):
                    continue
                for h in inner:
                    if not isinstance(h, dict):
                        continue
                    name = h.get("command") or h.get("name") or h.get("type") or "(hook)"
                    name = str(name).strip() or "(hook)"
                    description = entry.get("matcher") or h.get("description")
                    out.append(HookEntry(
                        name=name,
                        event=str(event),
                        description=str(description) if description else None,
                    ))
                    seen_names.add(name)

    # Path 2: bare scripts in hooks/ directory not already covered.
    hooks_dir = plugin_root / "hooks"
    if hooks_dir.is_dir():
        for p in sorted(hooks_dir.iterdir()):
            if not p.is_file():
                continue
            if p.name in seen_names or str(p) in seen_names:
                continue
            out.append(HookEntry(name=p.name, event=None, description=None))
    return out


def _list_mcps(plugin_root: Path) -> List[McpEntry]:
    """Parse MCP server declarations from ``.mcp.json`` (or legacy
    ``mcp_servers.json``). Returns ``[(name, type, description)]``.

    Standard shape: ``{"mcpServers": {<name>: {<config>}}}`` where ``<config>``
    contains either ``command`` (stdio) or ``url`` (sse). Type is inferred
    from those fields. Description falls through to ``command`` so the
    operator at least sees what's launched.
    """
    candidates = [
        plugin_root / ".mcp.json",
        plugin_root / "mcp_servers.json",
    ]
    out: List[McpEntry] = []
    for path in candidates:
        if not path.is_file():
            continue
        try:
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        if not isinstance(servers, dict):
            continue
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                out.append(McpEntry(name=str(name)))
                continue
            kind: Optional[str] = None
            description: Optional[str] = None
            if "command" in cfg:
                kind = "stdio"
                description = str(cfg.get("command"))
                args = cfg.get("args")
                if isinstance(args, list) and args:
                    description = description + " " + " ".join(str(a) for a in args)
            elif "url" in cfg:
                kind = "sse"
                description = str(cfg.get("url"))
            out.append(McpEntry(
                name=str(name),
                type=kind,
                description=description,
            ))
        break  # first found candidate wins
    return out


def _bundle_size(plugin_root: Path) -> Optional[int]:
    """Sum of file sizes under ``plugin_root``. None if path missing."""
    if not plugin_root.is_dir():
        return None
    total = 0
    for p in plugin_root.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def _walk_files(root: Path) -> List[FileEntry]:
    """Recursive file listing under ``root``.

    Returns sorted ``[FileEntry(path, size)]`` with paths relative to ``root``
    (forward-slash form for stable display). Directories are skipped — only
    leaf files appear, matching the way ``store_detail.html`` renders the
    Files section. Empty list if ``root`` is missing or not a directory.
    """
    if not root.is_dir():
        return []
    out: List[FileEntry] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root).as_posix()
            out.append(FileEntry(path=rel, size=p.stat().st_size))
        except OSError:
            continue
    return out


def _resolve_owner_display(
    conn: duckdb.DuckDBPyConnection,
    owner_user_id: str,
    fallback: str,
) -> str:
    """Friendly owner display name — ``users.name → users.email → fallback``.

    Mirrors the inline lookup ``app/web/router.py::store_detail`` already does
    so the marketplace API surfaces the same string the Store page shows.
    """
    row = conn.execute(
        "SELECT name, email FROM users WHERE id = ?", [owner_user_id]
    ).fetchone()
    if not row:
        return fallback
    return row[0] or row[1] or fallback


def _get_plugin_row(
    conn: duckdb.DuckDBPyConnection,
    marketplace_id: str,
    plugin_name: str,
) -> Optional[dict]:
    """Fetch a single ``marketplace_plugins`` row as a dict, or ``None``."""
    rows = conn.execute(
        "SELECT * FROM marketplace_plugins "
        "WHERE marketplace_id = ? AND name = ?",
        [marketplace_id, plugin_name],
    ).fetchall()
    if not rows:
        return None
    columns = [d[0] for d in conn.description]
    return MarketplacePluginsRepository._row_to_dict(columns, rows[0])


@router.get(
    "/curated/{marketplace_id}/{plugin_name}",
    response_model=PluginDetailResponse,
)
async def curated_detail(
    marketplace_id: str,
    plugin_name: str,
    _user: dict = Depends(require_resource_access(
        ResourceType.MARKETPLACE_PLUGIN, "{marketplace_id}/{plugin_name}",
    )),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return the curated plugin detail + inner skill/agent/command/hook/mcp list.

    The 403 guard fires before this body runs (via ``require_resource_access``).
    A second ``get_current_user`` dependency is included so we still have the
    caller's user dict for the ``installed`` flag.
    """
    plugin_row = _get_plugin_row(conn, marketplace_id, plugin_name)
    if plugin_row is None:
        raise HTTPException(status_code=404, detail="plugin_not_found")

    plugin_root = (
        Path(get_marketplaces_dir()) / marketplace_id / "plugins" / plugin_name
    )
    skills = _list_inner_skills(plugin_root)
    agents = _list_inner_agents(plugin_root)

    # v32: enrich each skill/agent card with its agnes-metadata cover photo
    # so the inner cards on the plugin detail page render the real image
    # instead of just initials. Read agnes-metadata + mirror manifest once
    # (cached by the helpers) and reuse for all inner items in the plugin.
    from src.marketplace_metadata import read_agnes_metadata as _read_md
    inner_metadata = _read_md(
        Path(get_marketplaces_dir()) / marketplace_id,
    )
    inner_manifest = _load_mirror_manifest(marketplace_id)

    for s in skills:
        s.detail_url = (
            f"/marketplace/curated/{marketplace_id}/{plugin_name}/skill/{s.name}"
        )
        s.cover_photo_url = _curated_inner_cover(
            marketplace_id, plugin_name, "skill", s.name,
            manifest=inner_manifest, metadata=inner_metadata,
        )
    for a in agents:
        a.detail_url = (
            f"/marketplace/curated/{marketplace_id}/{plugin_name}/agent/{a.name}"
        )
        a.cover_photo_url = _curated_inner_cover(
            marketplace_id, plugin_name, "agent", a.name,
            manifest=inner_manifest, metadata=inner_metadata,
        )
    commands = _list_commands(plugin_root)
    hooks = _list_hooks(plugin_root)
    mcps = _list_mcps(plugin_root)

    subs = UserCuratedSubscriptionsRepository(conn).subscribed_set(user["id"])
    raw = plugin_row.get("raw") or {}
    if isinstance(raw, str):
        try:
            import json
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}

    # v32: cover, video, and doc_links come from `agnes-metadata.json` via the
    # sync pipeline (`src/marketplace.py::_refresh_plugin_cache`) — already in
    # served-URL form when written to the DB. We pass them through the
    # response model unchanged. Curator name + email come from the marketplace
    # registry and surface as the plugin's accountable owner; the upstream
    # `marketplace.json::author.name` is intentionally not used here so we
    # have a single source of truth for "who runs this".
    meta = _resolve_marketplace_meta(conn, marketplace_id)
    doc_link_entries: List[DocEntry] = []
    raw_links = plugin_row.get("doc_links")
    if isinstance(raw_links, list):
        for link in raw_links:
            if isinstance(link, dict) and link.get("name") and link.get("url"):
                doc_link_entries.append(
                    DocEntry(name=str(link["name"]), url=str(link["url"]))
                )

    return PluginDetailResponse(
        source="curated",
        marketplace_id=marketplace_id,
        marketplace_name=meta["name"],
        plugin_name=plugin_name,
        manifest_name=(raw.get("name") if isinstance(raw, dict) else None) or plugin_name,
        description=plugin_row.get("description"),
        version=plugin_row.get("version"),
        category=plugin_row.get("category"),
        author_name=meta["curator_name"] or OWNER_TODO_PLACEHOLDER,
        curator_email=meta["curator_email"],
        homepage=plugin_row.get("homepage"),
        cover_photo_url=plugin_row.get("cover_photo_url"),
        video_url=plugin_row.get("video_url"),
        bundle_size=_bundle_size(plugin_root),
        released_at=_to_iso(plugin_row.get("created_at")),
        updated_at=_to_iso(plugin_row.get("updated_at")),
        installed=(marketplace_id, plugin_name) in subs,
        skills=skills,
        agents=agents,
        commands=commands,
        hooks=hooks,
        mcps=mcps,
        files=_walk_files(plugin_root),
        docs=doc_link_entries,
    )


@router.get(
    "/flea/{entity_id}/detail",
    response_model=PluginDetailResponse,
)
async def flea_detail(
    entity_id: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Same shape as curated detail, sourced from a Store entity.

    Flea entities live at ``${DATA_DIR}/store/<entity_id>/plugin/`` —
    canonical Claude Code plugin tree, so the same parsers apply.
    """
    from app.utils import get_store_dir
    entity = StoreEntitiesRepository(conn).get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="entity_not_found")

    plugin_root = Path(get_store_dir()) / entity_id / "plugin"

    if entity["type"] == "plugin":
        skills = _list_inner_skills(plugin_root)
        for s in skills:
            s.detail_url = f"/marketplace/flea/{entity_id}/skill/{s.name}"
        agents = _list_inner_agents(plugin_root)
        for a in agents:
            a.detail_url = f"/marketplace/flea/{entity_id}/agent/{a.name}"
        commands = _list_commands(plugin_root)
        hooks = _list_hooks(plugin_root)
        mcps = _list_mcps(plugin_root)
    else:
        # skill / agent — inner structure isn't applicable; the dedicated
        # item-detail page will render those entities. We still return the
        # response so the frontend can decide which template to use.
        skills = []
        agents = []
        commands = []
        hooks = []
        mcps = []

    is_installed = UserStoreInstallsRepository(conn).is_installed(
        user["id"], entity_id,
    )

    cover_url: Optional[str] = None
    if entity.get("photo_path"):
        cover_url = f"/api/store/entities/{entity_id}/photo"

    invocation = suffixed_name(entity["name"], entity.get("owner_username") or "")

    # doc_paths is a JSON array of relative paths the uploader picked at upload
    # time; `app/api/store.py` serves them by basename via /api/store/.../docs/{filename}.
    docs: List[DocEntry] = []
    for raw_path in (entity.get("doc_paths") or []):
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        fname = Path(raw_path).name
        if not fname:
            continue
        docs.append(DocEntry(
            name=fname,
            url=f"/api/store/entities/{entity_id}/docs/{fname}",
        ))

    owner_display = _resolve_owner_display(
        conn,
        entity["owner_user_id"],
        entity.get("owner_username") or "",
    )

    return PluginDetailResponse(
        source="flea",
        entity_id=entity_id,
        plugin_name=entity["name"],
        manifest_name=invocation,
        description=entity.get("description"),
        version=entity.get("version"),
        category=entity.get("category"),
        author_name=entity.get("owner_username"),
        owner_display=owner_display,
        homepage=None,
        cover_photo_url=cover_url,
        bundle_size=int(entity.get("file_size") or 0) or _bundle_size(plugin_root),
        install_count=int(entity.get("install_count") or 0),
        released_at=_to_iso(entity.get("created_at")),
        updated_at=_to_iso(entity.get("updated_at")),
        installed=is_installed,
        skills=skills,
        agents=agents,
        commands=commands,
        hooks=hooks,
        mcps=mcps,
        files=_walk_files(plugin_root),
        docs=docs,
    )


@router.post(
    "/curated/{marketplace_id}/{plugin_name}/install",
    response_model=InstallActionResponse,
)
async def curated_install(
    marketplace_id: str,
    plugin_name: str,
    _user: dict = Depends(require_resource_access(
        ResourceType.MARKETPLACE_PLUGIN, "{marketplace_id}/{plugin_name}",
    )),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Subscribe the caller to a curated plugin (Model B opt-in).

    Idempotent — repeated calls are no-ops. The plugin must exist in
    ``marketplace_plugins``; otherwise 404. The RBAC guard already ensured
    the caller is allowed to install.
    """
    exists = conn.execute(
        "SELECT 1 FROM marketplace_plugins WHERE marketplace_id = ? AND name = ?",
        [marketplace_id, plugin_name],
    ).fetchone()
    if not exists:
        raise HTTPException(status_code=404, detail="plugin_not_found")
    inserted = UserCuratedSubscriptionsRepository(conn).subscribe(
        user["id"], marketplace_id, plugin_name,
    )
    if inserted:
        _audit(
            conn, user["id"], "marketplace.curated.install",
            f"plugin:{marketplace_id}/{plugin_name}",
        )
        _invalidate_etag()
    return InstallActionResponse(installed=True)


@router.delete(
    "/curated/{marketplace_id}/{plugin_name}/install",
    response_model=InstallActionResponse,
)
async def curated_uninstall(
    marketplace_id: str,
    plugin_name: str,
    _user: dict = Depends(require_resource_access(
        ResourceType.MARKETPLACE_PLUGIN, "{marketplace_id}/{plugin_name}",
    )),
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    deleted = UserCuratedSubscriptionsRepository(conn).unsubscribe(
        user["id"], marketplace_id, plugin_name,
    )
    if deleted:
        _audit(
            conn, user["id"], "marketplace.curated.uninstall",
            f"plugin:{marketplace_id}/{plugin_name}",
        )
        _invalidate_etag()
    return InstallActionResponse(installed=False)


# ---------------------------------------------------------------------------
# Curated inner skill/agent detail
# ---------------------------------------------------------------------------


def _safe_join(plugin_root: Path, *parts: str) -> Optional[Path]:
    """Join ``parts`` onto ``plugin_root`` and return the resolved path only if
    it actually lives under ``plugin_root``. Defends against ``..`` segments,
    Windows ``\\`` separators that slip past Starlette's ``[^/]+`` path-param
    regex, and symlinks planted inside a curated marketplace's git mirror that
    point outside the plugin's own tree.

    Returns ``None`` when the candidate doesn't exist, can't be resolved, or
    escapes ``plugin_root``. Callers translate that into a 404.
    """
    candidate = plugin_root.joinpath(*parts)
    try:
        resolved = candidate.resolve(strict=True)
        root = plugin_root.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved


def _read_inner(
    plugin_root: Path,
    sub: str,
    name: str,
    *,
    is_dir_layout: bool,
) -> Optional[tuple[str, str]]:
    """Return (text, relpath) for a skill (sub='skills', is_dir=True) or
    agent (sub='agents', is_dir=False) inside the plugin tree, or None if
    the file is missing or escapes ``plugin_root`` (see ``_safe_join``).
    """
    if is_dir_layout:
        candidate = _safe_join(plugin_root, sub, name, "SKILL.md")
    else:
        candidate = _safe_join(plugin_root, sub, f"{name}.md")
    if candidate is None:
        return None
    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        rel = candidate.relative_to(plugin_root.resolve()).as_posix()
    except ValueError:
        rel = candidate.name
    return text, rel


def _curated_inner_parent_fields(
    conn: duckdb.DuckDBPyConnection, marketplace_id: str, plugin_name: str,
) -> Dict[str, Any]:
    """Pull the parent-plugin metadata that the redesigned skill/agent detail
    page renders in the hero badges, meta-row, and sidebar.

    Returns a dict with ``marketplace_name``, ``category``, ``parent_author_name``,
    ``parent_updated_at``. Missing fields fall through to safe defaults so the
    inner endpoint still succeeds when the parent row is absent (which would be
    a sync-skew bug, but shouldn't 500 the page).
    """
    plugin_row = _get_plugin_row(conn, marketplace_id, plugin_name) or {}
    plugin_root = (
        Path(get_marketplaces_dir()) / marketplace_id / "plugins" / plugin_name
    )
    meta = _resolve_marketplace_meta(conn, marketplace_id)
    return {
        "marketplace_name": meta["name"],
        "category": plugin_row.get("category"),
        "parent_author_name": meta["curator_name"] or OWNER_TODO_PLACEHOLDER,
        "parent_updated_at": _to_iso(plugin_row.get("updated_at")),
        "manifest_name": resolve_manifest_name(plugin_root, fallback=plugin_name),
    }


def _load_mirror_manifest(marketplace_id: str) -> Dict[str, Any]:
    """Read the asset-mirror manifest for one marketplace into ``{url: entry}``.

    Returns an empty dict when the cache directory or manifest doesn't exist
    (fresh install, marketplace never synced) — callers treat that as "no
    URL is mirrored" which collapses to the same path as a fetch failure.
    """
    from src.marketplace_asset_mirror import _load_manifest

    cache_dir = get_marketplace_cache_dir() / marketplace_id
    return _load_manifest(cache_dir)


def _mirrored_url(marketplace_id: str, plugin_name: str, key: str) -> str:
    """Served URL for a mirrored external asset under cache.

    Mirror endpoint route — kept here as a local helper so ``app/api/`` does
    not need a sync-side import. Same shape as ``src/marketplace.py``'s
    ``_mirrored_asset_url``; the two must stay aligned with the FastAPI
    route definition in this module (``/curated/{mp}/{plugin}/mirrored/{key}``).
    """
    return f"/api/marketplace/curated/{marketplace_id}/{plugin_name}/mirrored/{key}"


def _resolve_external_via_mirror(
    marketplace_id: str,
    plugin_name: str,
    url: str,
    manifest: Dict[str, Any],
) -> Optional[str]:
    """Translate one external URL into the Agnes-served `/mirrored/` URL when
    the asset mirror successfully cached it; ``None`` otherwise.

    Used by the inner-detail (skill/agent) enrichment to apply the same
    "drop entries Agnes can't deliver" rule that the plugin-level sync
    flow already enforces. When this returns None the caller drops the
    enrichment entry entirely (no broken external link surfaces in the UI).
    """
    entry = manifest.get(url)
    if entry is None or entry.status != "ok" or not entry.local:
        return None
    # Manifest stores `local` as `<plugin>/<rest>`; the /mirrored/ endpoint
    # expects just `<rest>` (the plugin segment is in the URL path). Same
    # transform as src/marketplace.py uses on the plugin-level path.
    rest = entry.local.split("/", 1)[1] if "/" in entry.local else entry.local
    return _mirrored_url(marketplace_id, plugin_name, rest)


def _curated_inner_enrichment(
    marketplace_id: str,
    plugin_name: str,
    kind: str,
    inner_name: str,
) -> Dict[str, Any]:
    """Load agnes-metadata.json sub-tree for a single skill/agent.

    Lives here (not in the sync pipeline) so curators can update agnes-metadata
    on a working tree and see the change at the next page refresh, without
    waiting for a full plugin-cache rewrite. Read on every inner-detail
    request — agnes-metadata.json is small enough that disk hit cost is
    negligible compared to the SKILL.md / agent.md frontmatter parse the
    endpoint already does.

    External URL handling matches the plugin-level sync flow:
    * cover_photo external → kept only when the asset mirror has a
      successful cached copy (URL resolves to /mirrored/...). Else dropped
      → the card / hero falls through to the gradient placeholder.
    * doc_links external → same; entries that aren't mirrored OK get
      dropped from the served list. Internal docs survive only when the
      file actually exists in the working tree.

    Returns a dict shaped for direct merge into the InnerDetailResponse:
    ``cover_photo_url`` (resolved served URL or None),
    ``video_url`` (str or None), ``docs`` (list of DocEntry).
    """
    from src.marketplace_metadata import (
        read_agnes_metadata,
        resolve_inner_metadata,
    )

    repo_root = Path(get_marketplaces_dir()) / marketplace_id
    metadata = read_agnes_metadata(repo_root)
    section_kind = "skills" if kind == "skill" else "agents"
    resolved = resolve_inner_metadata(metadata, plugin_name, section_kind, inner_name)
    if not resolved:
        return {"cover_photo_url": None, "video_url": None, "docs": []}

    manifest = _load_mirror_manifest(marketplace_id)

    cover_url: Optional[str] = None
    cover_ref = resolved.get("cover_photo_ref")
    if isinstance(cover_ref, tuple):
        ref_kind, target = cover_ref
        if ref_kind == "internal":
            local_path = repo_root / target
            if local_path.is_file():
                cover_url = (
                    f"/api/marketplace/curated/{marketplace_id}/{plugin_name}"
                    f"/asset/{target}"
                )
        elif ref_kind == "external":
            cover_url = _resolve_external_via_mirror(
                marketplace_id, plugin_name, target, manifest,
            )

    docs: List[DocEntry] = []
    for link in resolved.get("doc_links") or []:
        if not hasattr(link, "kind"):
            continue
        if link.kind == "internal":
            local_path = repo_root / link.path
            if not local_path.is_file():
                continue
            docs.append(DocEntry(
                name=link.name,
                url=(
                    f"/api/marketplace/curated/{marketplace_id}/{plugin_name}"
                    f"/doc/{link.path}"
                ),
            ))
        else:
            served = _resolve_external_via_mirror(
                marketplace_id, plugin_name, link.url, manifest,
            )
            if served is None:
                continue
            docs.append(DocEntry(name=link.name, url=served))

    return {
        "cover_photo_url": cover_url,
        "video_url": resolved.get("video_url"),
        "docs": docs,
    }


def _curated_inner_cover(
    marketplace_id: str,
    plugin_name: str,
    kind: str,
    inner_name: str,
    manifest: Optional[Dict[str, Any]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Cheap helper: just the cover URL for one inner item.

    Used by ``curated_detail`` to populate ``InnerItemSummary.cover_photo_url``
    on the parent-plugin's skills/agents card list. Called once per inner
    item; metadata + manifest are loaded once per plugin and passed in to
    avoid N disk reads.
    """
    from src.marketplace_metadata import resolve_inner_metadata

    if metadata is None:
        from src.marketplace_metadata import read_agnes_metadata
        repo_root = Path(get_marketplaces_dir()) / marketplace_id
        metadata = read_agnes_metadata(repo_root)
    if manifest is None:
        manifest = _load_mirror_manifest(marketplace_id)

    section_kind = "skills" if kind == "skill" else "agents"
    resolved = resolve_inner_metadata(metadata, plugin_name, section_kind, inner_name)
    cover_ref = resolved.get("cover_photo_ref") if resolved else None
    if not isinstance(cover_ref, tuple):
        return None
    ref_kind, target = cover_ref
    if ref_kind == "internal":
        local_path = (Path(get_marketplaces_dir()) / marketplace_id / target)
        if local_path.is_file():
            return (
                f"/api/marketplace/curated/{marketplace_id}/{plugin_name}"
                f"/asset/{target}"
            )
        return None
    return _resolve_external_via_mirror(
        marketplace_id, plugin_name, target, manifest,
    )


@router.get(
    "/curated/{marketplace_id}/{plugin_name}/skill/{skill_name}",
    response_model=InnerDetailResponse,
)
async def curated_skill_detail(
    marketplace_id: str,
    plugin_name: str,
    skill_name: str,
    _user: dict = Depends(require_resource_access(
        ResourceType.MARKETPLACE_PLUGIN, "{marketplace_id}/{plugin_name}",
    )),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    plugin_root = (
        Path(get_marketplaces_dir()) / marketplace_id / "plugins" / plugin_name
    )
    res = _read_inner(plugin_root, "skills", skill_name, is_dir_layout=True)
    skill_dir = _safe_join(plugin_root, "skills", skill_name)
    if res is None or skill_dir is None:
        raise HTTPException(status_code=404, detail="skill_not_found")
    text, relpath = res
    fm = _parse_frontmatter(text)
    parent = _curated_inner_parent_fields(conn, marketplace_id, plugin_name)
    enrichment = _curated_inner_enrichment(
        marketplace_id, plugin_name, "skill", skill_name,
    )
    return InnerDetailResponse(
        marketplace_id=marketplace_id,
        plugin_name=plugin_name,
        kind="skill",
        name=fm.get("name") or skill_name,
        description=fm.get("description"),
        body=_frontmatter_body(text),
        relpath=relpath,
        bundle_size=_bundle_size(skill_dir),
        files=_walk_files(skill_dir),
        **parent,
        **enrichment,
    )


@router.get(
    "/curated/{marketplace_id}/{plugin_name}/agent/{agent_name}",
    response_model=InnerDetailResponse,
)
async def curated_agent_detail(
    marketplace_id: str,
    plugin_name: str,
    agent_name: str,
    _user: dict = Depends(require_resource_access(
        ResourceType.MARKETPLACE_PLUGIN, "{marketplace_id}/{plugin_name}",
    )),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    plugin_root = (
        Path(get_marketplaces_dir()) / marketplace_id / "plugins" / plugin_name
    )
    res = _read_inner(plugin_root, "agents", agent_name, is_dir_layout=False)
    agent_path = _safe_join(plugin_root, "agents", f"{agent_name}.md")
    if res is None or agent_path is None:
        raise HTTPException(status_code=404, detail="agent_not_found")
    text, relpath = res
    fm = _parse_frontmatter(text)
    # Agents are flat single-file .md — bundle = file size, files = one entry.
    try:
        agent_size = agent_path.stat().st_size
    except OSError:
        agent_size = 0
    parent = _curated_inner_parent_fields(conn, marketplace_id, plugin_name)
    enrichment = _curated_inner_enrichment(
        marketplace_id, plugin_name, "agent", agent_name,
    )
    return InnerDetailResponse(
        marketplace_id=marketplace_id,
        plugin_name=plugin_name,
        kind="agent",
        name=fm.get("name") or agent_name,
        description=fm.get("description"),
        body=_frontmatter_body(text),
        relpath=relpath,
        bundle_size=agent_size,
        files=[FileEntry(path=f"{agent_name}.md", size=agent_size)],
        **parent,
        **enrichment,
    )


# ---------------------------------------------------------------------------
# Asset / doc / mirrored serving endpoints (v32)
# ---------------------------------------------------------------------------
#
# Three sibling endpoints that serve the binary content referenced from
# `agnes-metadata.json`. All three:
#
#   * are gated by `require_resource_access(MARKETPLACE_PLUGIN, "{mp}/{plugin}")`
#     so a user without RBAC can't side-load assets even with a direct URL,
#   * resolve a candidate path with `Path.resolve(strict=True)` and verify the
#     result lives under the expected root via `is_relative_to()` — defense
#     against `..` / absolute paths / symlinks pointing out of the tree,
#   * use FastAPI's `FileResponse` so Content-Type detection comes from the
#     stdlib mimetypes module (good enough for the allowlisted set; binary
#     fallback for anything we don't recognize).


def _path_under(root: Path, *parts: str) -> Optional[Path]:
    """Resolve ``root / *parts`` and confirm the result stays under ``root``.

    Returns ``None`` if the file is missing, can't be resolved, or escapes
    ``root`` (typical sources of escape: ``..`` segments, Windows backslashes
    that survived path-param parsing, planted symlinks). Caller maps None to
    a 404 — distinct from "found but rejected by allowlist" which the doc
    endpoint surfaces as 415.
    """
    candidate = root.joinpath(*parts)
    try:
        resolved = candidate.resolve(strict=True)
        anchor = root.resolve(strict=True)
        resolved.relative_to(anchor)
    except (OSError, ValueError):
        return None
    return resolved


def _doc_disposition(filename: str) -> dict:
    """Force-download headers for the /doc and /mirrored doc paths.

    Browsers honor the frontend's `download` attribute only for same-origin
    URLs; the explicit Content-Disposition header makes the download
    behavior reliable regardless of how the link was opened (right-click →
    open, programmatic fetch, curl, …). Filename is the basename so the
    browser save dialog shows something readable.
    """
    safe = Path(filename).name or "download"
    return {"Content-Disposition": f'attachment; filename="{safe}"'}


# Mapping from validated image extension to the Content-Type we serve.
# Pinned (not stdlib mimetypes) so an unexpected file with a known extension
# can't push us into ``text/html`` territory — this endpoint NEVER serves
# anything but image bytes labelled as image bytes.
_ASSET_CONTENT_TYPE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

# Defense-in-depth headers applied to every /asset/ response. ``nosniff``
# stops browsers from second-guessing our Content-Type; the strict CSP is
# a belt-and-suspenders block — even if a future regression let HTML
# through, the browser still won't execute scripts/iframes/etc.
_ASSET_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
}


@router.get("/curated/{marketplace_id}/{plugin_name}/asset/{path:path}")
async def curated_asset(
    marketplace_id: str,
    plugin_name: str,
    path: str,
    _user: dict = Depends(require_resource_access(
        ResourceType.MARKETPLACE_PLUGIN, "{marketplace_id}/{plugin_name}",
    )),
):
    """Serve an internal image asset from the cloned marketplace working tree.

    Paths are repo-root-relative — ``{path}`` may be e.g.
    ``.agnes/cover.png`` or ``plugins/foo/icon.png``.

    **Image-only by contract.** The endpoint is the source of cover photos
    referenced from ``agnes-metadata.json`` and from inner skill / agent
    cards. A curator who could land an arbitrary file in the cloned repo
    (HTML, JS, SVG with inline ``<script>``) would otherwise have a
    same-origin XSS via this endpoint, since the response shares the
    cookie scope with ``/admin`` and ``/api/me/*``. Three layered checks:

    1. Extension must be in :data:`src.marketplace_assets.IMAGE_EXTENSIONS`
       (``.png``/``.jpg``/``.jpeg``/``.webp``); anything else → 415.
    2. Body must pass :func:`src.marketplace_assets.validate_image_file`
       magic-bytes check; mismatch → 415 (defeats the rename-extension
       attack: ``evil.png`` carrying ``<script>`` bytes).
    3. ``Content-Type`` is pinned from the extension table above (not
       stdlib mimetypes), so the response is never served as ``text/html``
       even if mimetypes were misconfigured.

    SVG is intentionally not in the allowlist — ``<script>`` inside SVG
    executes in the browser. ``X-Content-Type-Options: nosniff`` plus a
    strict CSP harden the response further.

    Inline rendering (no ``Content-Disposition``) — covers display in
    ``<img>``, not as a download.
    """
    from src.marketplace_assets import IMAGE_EXTENSIONS, validate_image_file

    repo_root = Path(get_marketplaces_dir()) / marketplace_id
    if not repo_root.exists():
        raise HTTPException(status_code=404, detail="marketplace_not_synced")
    safe = _path_under(repo_root, path)
    if safe is None or not safe.is_file():
        raise HTTPException(status_code=404, detail="asset_not_found")

    ext = safe.suffix.lower()
    if ext not in IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported_asset_extension: {ext or '(none)'}",
        )
    try:
        body = safe.read_bytes()
    except OSError:
        raise HTTPException(status_code=404, detail="asset_not_readable")
    validation = validate_image_file(safe.name, body)
    if not validation.ok:
        raise HTTPException(
            status_code=415,
            detail=f"asset_validation_failed: {validation.reason}",
        )
    return FileResponse(
        safe,
        media_type=_ASSET_CONTENT_TYPE[ext],
        headers=_ASSET_SECURITY_HEADERS,
    )


@router.get("/curated/{marketplace_id}/{plugin_name}/doc/{path:path}")
async def curated_doc(
    marketplace_id: str,
    plugin_name: str,
    path: str,
    _user: dict = Depends(require_resource_access(
        ResourceType.MARKETPLACE_PLUGIN, "{marketplace_id}/{plugin_name}",
    )),
):
    """Serve an internal doc path from the cloned marketplace working tree.

    Same path-traversal guard as ``/asset/``. Adds an allowlist check — the
    doc endpoint refuses to serve a file whose extension isn't in the
    documented PDF / Markdown / plain text set (HTTP 415). Defense-in-depth
    even though the agnes-metadata parser already rejects out-of-allowlist
    extensions during the doc_link parse — a curator who edits the working
    tree directly (or whose JSON survived parsing because of a generic
    extension match elsewhere) shouldn't be able to land a .docx through
    a re-served doc URL.

    Force-download via Content-Disposition: attachment — clicking a doc
    link in the UI saves the file to disk rather than opening it in a tab.
    """
    from src.marketplace_assets import DOC_EXTENSIONS

    repo_root = Path(get_marketplaces_dir()) / marketplace_id
    if not repo_root.exists():
        raise HTTPException(status_code=404, detail="marketplace_not_synced")
    safe = _path_under(repo_root, path)
    if safe is None or not safe.is_file():
        raise HTTPException(status_code=404, detail="doc_not_found")
    if safe.suffix.lower() not in DOC_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported_doc_extension: {safe.suffix.lower() or '(none)'}",
        )
    return FileResponse(safe, headers=_doc_disposition(safe.name))


@router.get("/curated/{marketplace_id}/{plugin_name}/mirrored/{key:path}")
async def curated_mirrored(
    marketplace_id: str,
    plugin_name: str,
    key: str,
    _user: dict = Depends(require_resource_access(
        ResourceType.MARKETPLACE_PLUGIN, "{marketplace_id}/{plugin_name}",
    )),
):
    """Serve a mirrored external asset from the marketplace cache.

    ``key`` is the ``local`` relpath stored in the cache manifest minus the
    leading ``<plugin>/`` prefix — the mirror writes files at
    ``${DATA_DIR}/marketplace-cache/<slug>/<plugin>/<rest>``, and this
    endpoint expects ``{plugin}/{rest}`` so the same path-resolution check
    catches escapes whether the caller provided ``cover.png`` or
    ``docs/abc-setup.pdf``.

    Doc-shaped paths (key starting with ``docs/``) get the same force-download
    treatment as the internal /doc/ endpoint. Cover photos under the cache
    root render inline so the <img> tag works.
    """
    cache_root = get_marketplace_cache_dir() / marketplace_id / plugin_name
    if not cache_root.exists():
        raise HTTPException(status_code=404, detail="mirror_cache_missing")
    safe = _path_under(cache_root, key)
    if safe is None or not safe.is_file():
        raise HTTPException(status_code=404, detail="mirrored_asset_not_found")
    if key.startswith("docs/"):
        return FileResponse(safe, headers=_doc_disposition(safe.name))
    return FileResponse(safe)
