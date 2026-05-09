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
from pydantic import BaseModel

from app.auth.access import (
    _user_group_ids,
    is_user_admin,
    require_resource_access,
)
from app.auth.dependencies import _get_db, get_current_user
from app.resource_types import ResourceType
from app.utils import get_marketplaces_dir
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
    # v32+ quarantine UX: surface the card's visibility + an
    # is_viewer_owner flag so the listing template can render an
    # "Under review" / "Quarantined" corner badge on the submitter's
    # own non-approved cards. Approved cards omit both fields.
    visibility_status: Optional[str] = None
    is_viewer_owner: bool = False


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
    owner_display: Optional[str] = None         # flea: users.name → email → owner_username
    homepage: Optional[str] = None
    cover_photo_url: Optional[str] = None       # /api/store/.../photo for flea, curator metadata for curated
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
    # v32+ quarantine: surface the entity's visibility so the JS install
    # path can refuse to render the install button when non-approved.
    # Curated entries omit (always live).
    visibility_status: Optional[str] = None
    # Latest submission verdict for the linked entity — populated only
    # for the owner / admin (the same audiences that see the quarantine
    # banner). The banner's auto-refresh JS polls this field so it can
    # reload the page when an LLM review lands; visibility alone is
    # insufficient because `blocked_llm` keeps the entity at
    # `visibility_status='pending'`.
    submission_status: Optional[str] = None


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


def _resolve_marketplace_name(
    conn: duckdb.DuckDBPyConnection, marketplace_id: str
) -> str:
    row = MarketplaceRegistryRepository(conn).get(marketplace_id)
    if not row:
        return marketplace_id
    return (row.get("name") or "").strip() or marketplace_id


def _curated_to_item(
    conn: duckdb.DuckDBPyConnection,
    plugin_row: dict,
    *,
    subs: set,
    marketplace_names: Dict[str, str],
) -> MarketplaceItem:
    marketplace_id = plugin_row["marketplace_id"]
    plugin_name = plugin_row["name"]
    mp_name = marketplace_names.get(marketplace_id) or marketplace_id
    return MarketplaceItem(
        id=f"curated-{marketplace_id}/{plugin_name}",
        source="curated",
        name=plugin_name,
        type="plugin",
        category=plugin_row.get("category") or None,
        description=plugin_row.get("description"),
        owner=plugin_row.get("author_name") or OWNER_TODO_PLACEHOLDER,
        version=plugin_row.get("version"),
        photo_url=None,
        added=_to_iso(plugin_row.get("created_at")),
        installed=(marketplace_id, plugin_name) in subs,
        marketplace_slug=marketplace_id,
        marketplace_name=mp_name,
        detail_url=_curated_detail_url(marketplace_id, plugin_name),
    )


def _flea_to_item(
    entity: dict, *, installed_set: set, viewer_id: Optional[str] = None,
) -> MarketplaceItem:
    photo_url = (
        f"/api/store/entities/{entity['id']}/photo"
        if entity.get("photo_path") else None
    )
    # The archive flow renames the row's `name` to free the slot; strip
    # the suffix when rendering listings so owners don't see the ugly
    # `__archived__<epoch>` in their own cards. The served catalog
    # (Claude Code's `/plugin` resolution) uses the renamed slug — we
    # don't strip there.
    from src.store_naming import strip_archive_suffix
    display_name = strip_archive_suffix(entity["name"])
    invocation = suffixed_name(display_name, entity.get("owner_username") or "")
    is_viewer_owner = bool(viewer_id and entity.get("owner_user_id") == viewer_id)
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
        visibility_status=entity.get("visibility_status") or "approved",
        is_viewer_owner=is_viewer_owner,
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
        marketplace_names: Dict[str, str] = {}
        if rows:
            distinct_ids = {r["marketplace_id"] for r in rows}
            for mp_id in distinct_ids:
                marketplace_names[mp_id] = _resolve_marketplace_name(conn, mp_id)
        items = [
            _curated_to_item(conn, r, subs=subs, marketplace_names=marketplace_names)
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
        # Visibility filter: non-admin sees approved + their own
        # non-approved (so submitters spot what's still under review
        # in their own grid). Admin sees everything.
        from app.auth.access import is_user_admin
        if is_user_admin(user["id"], conn):
            visibility_filter = None
            include_owner = None
        else:
            visibility_filter = ["approved"]
            include_owner = user["id"]
        rows, total = StoreEntitiesRepository(conn).list(
            skip=skip, limit=page_size,
            type=type, category=category, search=q or None,
            visibility_status=visibility_filter,
            include_owner_id=include_owner,
        )
        items = [
            _flea_to_item(r, installed_set=installed_set, viewer_id=user["id"])
            for r in rows
        ]
        return ItemListResponse(
            items=items, total=total, page=page, page_size=page_size,
        )

    # tab == "my" — see docstring; read directly from source-of-truth tables.
    items: List[MarketplaceItem] = []

    granted = resolve_allowed_plugins(conn, user)
    subs = UserCuratedSubscriptionsRepository(conn).subscribed_set(user["id"])
    marketplace_names: Dict[str, str] = {}
    for p in granted:
        if (p["marketplace_id"], p["original_name"]) not in subs:
            continue
        mp_id = p["marketplace_id"]
        if mp_id not in marketplace_names:
            marketplace_names[mp_id] = _resolve_marketplace_name(conn, mp_id)
        author = p["raw"].get("author")
        plugin_row = {
            "marketplace_id": mp_id,
            "name": p["original_name"],
            "description": p["raw"].get("description"),
            "version": p.get("version"),
            "category": p["raw"].get("category"),
            "author_name": author.get("name") if isinstance(author, dict) else None,
            "created_at": None,
        }
        items.append(_curated_to_item(
            conn, plugin_row, subs=subs, marketplace_names=marketplace_names,
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
        # Visibility filter (v32+/v35): non-admin counts approved + own
        # non-archived non-approved (mirrors the listing endpoint so the
        # category counts match what the user actually sees in the
        # grid). Admin counts everything.
        from app.auth.access import is_user_admin
        clauses: List[str] = []
        sql_params: List[Any] = []
        if type:
            clauses.append("type = ?"); sql_params.append(type)
        if not is_user_admin(user["id"], conn):
            clauses.append(
                "(visibility_status = 'approved' "
                "OR (owner_user_id = ? AND visibility_status != 'archived'))"
            )
            sql_params.append(user["id"])
        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            f"SELECT COALESCE(NULLIF(TRIM(category),''), 'Other') AS cat, COUNT(*) "
            f"FROM store_entities {where_sql} GROUP BY cat",
            sql_params,
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
    for s in skills:
        s.detail_url = (
            f"/marketplace/curated/{marketplace_id}/{plugin_name}/skill/{s.name}"
        )
    agents = _list_inner_agents(plugin_root)
    for a in agents:
        a.detail_url = (
            f"/marketplace/curated/{marketplace_id}/{plugin_name}/agent/{a.name}"
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

    # Curated cover photo lives in dodatečná curator metadata (not in upstream
    # plugin.json today). The raw row may carry it once curators populate the
    # field; until then the value falls through to None and the frontend
    # paints a gradient placeholder.
    cover_url: Optional[str] = None
    if isinstance(raw, dict):
        candidate = raw.get("cover_photo_url") or raw.get("photo_url")
        if isinstance(candidate, str) and candidate.strip():
            cover_url = candidate.strip()

    return PluginDetailResponse(
        source="curated",
        marketplace_id=marketplace_id,
        marketplace_name=_resolve_marketplace_name(conn, marketplace_id),
        plugin_name=plugin_name,
        manifest_name=(raw.get("name") if isinstance(raw, dict) else None) or plugin_name,
        description=plugin_row.get("description"),
        version=plugin_row.get("version"),
        category=plugin_row.get("category"),
        author_name=plugin_row.get("author_name") or OWNER_TODO_PLACEHOLDER,
        homepage=plugin_row.get("homepage"),
        cover_photo_url=cover_url,
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
    from app.api.store import _enforce_visibility
    from app.utils import get_store_dir
    entity = StoreEntitiesRepository(conn).get(entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="entity_not_found")
    # Same gate as /api/store/entities/{id}: non-owner non-admin gets
    # 404 (not 403) for any non-approved entity. Without this, a user
    # who guesses an entity_id can still pull the bundle metadata
    # through the marketplace JSON feed even though it's quarantined
    # and excluded from the public listing.
    _enforce_visibility(entity, user, conn)

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

    # Strip archive-rename suffix for human display; manifest_name keeps
    # the renamed-on-archive slug since that's what Claude Code resolves.
    from src.store_naming import strip_archive_suffix
    _flea_display_name = strip_archive_suffix(entity["name"])
    invocation = suffixed_name(_flea_display_name, entity.get("owner_username") or "")

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

    # Surface the latest submission verdict to the owner / admin so the
    # quarantine banner's auto-refresh JS has a signal to poll on.
    # Visibility alone is not enough because `blocked_llm` keeps the
    # entity at `visibility_status='pending'`.
    submission_status: Optional[str] = None
    is_owner = entity.get("owner_user_id") == user.get("id")
    is_admin_user = is_user_admin(user["id"], conn)
    if is_owner or is_admin_user:
        from src.repositories.store_submissions import StoreSubmissionsRepository
        latest_sub = StoreSubmissionsRepository(conn).latest_for_entity(entity_id)
        if latest_sub:
            submission_status = latest_sub.get("status")

    return PluginDetailResponse(
        source="flea",
        entity_id=entity_id,
        plugin_name=_flea_display_name,
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
        visibility_status=entity.get("visibility_status") or "approved",
        submission_status=submission_status,
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
    return {
        "marketplace_name": _resolve_marketplace_name(conn, marketplace_id),
        "category": plugin_row.get("category"),
        "parent_author_name": plugin_row.get("author_name") or OWNER_TODO_PLACEHOLDER,
        "parent_updated_at": _to_iso(plugin_row.get("updated_at")),
        "manifest_name": resolve_manifest_name(plugin_root, fallback=plugin_name),
    }


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
    )
