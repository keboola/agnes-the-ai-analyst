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
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth.access import (
    _user_group_ids,
    is_user_admin,
    require_resource_access,
)
from app.auth.dependencies import _get_db, get_current_user
from app.resource_types import ResourceType
from app.utils import get_marketplace_cache_dir, get_marketplaces_dir
from src.marketplace_filter import (
    resolve_allowed_plugins,
    resolve_manifest_name,
)
from src.marketplace_listing import _FRONTMATTER_RE, _parse_frontmatter
from src.marketplace_urls import mirrored_url
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
    # v39: drives the "Required" pill on the curated browse cards. Only
    # set on curated items (flea/store entities are never system).
    is_system: bool = False
    # Rich-content fields from marketplace-metadata.json (plugin-level only
    # for now; skill/agent rich content lands in a later phase). Frontend
    # falls back to `name` / `description` when these are missing, so the
    # card renders identically to pre-enrichment behaviour for any plugin
    # whose curator hasn't filled the new fields yet.
    display_name: Optional[str] = None
    tagline: Optional[str] = None
    # telemetry (Phase B.1): populated from usage_plugin_daily rollup
    invocations_30d: int = 0
    unique_users_30d: int = 0
    trend_pct: Optional[float] = None


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
    # v32: marketplace-metadata-driven cover photo for the skill/agent card on the
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


class UseCase(BaseModel):
    """One "When to use it" example from marketplace-metadata.json.
    Curator-authored: friendly title + 1-2-sentence description + the
    literal prompt the user would paste into Claude Code."""
    title: str
    description: str
    prompt: str


class SampleInteraction(BaseModel):
    """Example dialog shown in the "Sample interaction" section.

    ``assistant`` is markdown — the API pre-renders to safe HTML in
    ``assistant_html`` so the template can inject without a client-side
    markdown lib. ``assistant`` stays as the source for copy-paste / future
    re-rendering needs.
    """
    user: str
    assistant: str
    assistant_html: str


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
    cover_photo_url: Optional[str] = None       # /api/store/.../photo for flea, marketplace-metadata for curated
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
    # v39: drives the disabled install button on the curated plugin
    # detail page. The same flag travels via /api/marketplace/items so
    # the browse cards can show a "Required" pill.
    is_system: bool = False
    # Rich-content fields from marketplace-metadata.json (plugin-level, curated
    # only — flea entities don't have a metadata layer). All optional; UI
    # sections render only when populated.
    #
    # `display_name` overrides the technical `manifest_name` on the hero h1
    # and window titlebar. `tagline` is the friendly subtitle (replacing
    # `description` as the hero summary on listing cards + hero).
    #
    # `description_long_html` is server-side rendered + sanitized markdown
    # (see app/markdown_render.render_safe). It powers the "What it does"
    # panel; the raw `description` from marketplace.json is the fallback.
    display_name: Optional[str] = None
    tagline: Optional[str] = None
    description_long_html: Optional[str] = None
    use_cases: List[UseCase] = []
    sample_interaction: Optional[SampleInteraction] = None
    # telemetry (Phase B.1)
    telemetry: Optional[Dict[str, Any]] = None


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
    # v32: per-skill / per-agent enrichment from marketplace-metadata.json sub-tree.
    # Read at request time from the cloned working tree (not cached in DB) so
    # curators can update one inner asset without paying the cost of a full
    # plugin-cache rewrite. Three-typed cover URL layout matches plugin
    # detail: internal → /asset/, mirror status not yet checked here so
    # external URLs pass through unmirrored (we don't run sync mid-request).
    cover_photo_url: Optional[str] = None
    video_url: Optional[str] = None
    docs: List[DocEntry] = []
    # Rich user-facing fields (parity with plugin-level rich content from
    # the 2026-05-12 redesign). All optional — UI hides each section when
    # the corresponding field is absent.
    #
    # `category` on the response is curator-override-OR-parent-fallback:
    # the API hands back the parent plugin's category when the skill/agent
    # didn't opt into its own. The override happens in the inner-detail
    # handler before the response leaves the API.
    #
    # `invocation`: curator-provided literal command string ("…&lt;arg&gt;"
    # forms welcome). When absent, the template falls back to its computed
    # "<manifest_name>:<inner_name>" string.
    display_name: Optional[str] = None
    tagline: Optional[str] = None
    description_long_html: Optional[str] = None
    use_cases: List[UseCase] = []
    sample_interaction: Optional[SampleInteraction] = None
    when_to_use_html: Optional[str] = None
    invocation: Optional[str] = None


class InstallActionResponse(BaseModel):
    installed: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_invocation_stats(
    conn: duckdb.DuckDBPyConnection,
    source: str,
) -> Dict[str, Dict]:
    """Return {ref_id: {invocations_30d, unique_users_30d, trend_pct}}.

    One query per source per page render — avoids N+1.
    """
    rows = conn.execute("""
        WITH last_30 AS (
            SELECT ref_id,
                   SUM(invocations) AS inv30,
                   SUM(distinct_users) AS u30
            FROM usage_plugin_daily
            WHERE source = ? AND day >= CURRENT_DATE - INTERVAL 30 DAY
            GROUP BY ref_id
        ),
        prior_week AS (
            SELECT ref_id, SUM(invocations) AS inv_prior
            FROM usage_plugin_daily
            WHERE source = ?
              AND day >= CURRENT_DATE - INTERVAL 14 DAY
              AND day <  CURRENT_DATE - INTERVAL 7  DAY
            GROUP BY ref_id
        ),
        recent_week AS (
            SELECT ref_id, SUM(invocations) AS inv_recent
            FROM usage_plugin_daily
            WHERE source = ?
              AND day >= CURRENT_DATE - INTERVAL 7 DAY
            GROUP BY ref_id
        )
        SELECT l.ref_id, l.inv30, l.u30, p.inv_prior, r.inv_recent
        FROM last_30 l
        LEFT JOIN prior_week p USING (ref_id)
        LEFT JOIN recent_week r USING (ref_id)
    """, [source, source, source]).fetchall()
    out: Dict[str, Dict] = {}
    for ref_id, inv30, u30, prior, recent in rows:
        trend = None
        if prior is not None and prior >= 3:
            _recent = recent or 0
            trend = (_recent - prior) / prior * 100.0
        out[ref_id] = {
            "invocations_30d": int(inv30 or 0),
            "unique_users_30d": int(u30 or 0),
            "trend_pct": trend,
        }
    return out


def _load_plugin_daily_series(
    conn: duckdb.DuckDBPyConnection,
    source: str,
    ref_id: str,
) -> List[Dict]:
    """Return a 30-entry list [{day, invocations}] with missing days filled to 0."""
    rows = conn.execute("""
        SELECT day, SUM(invocations) AS inv
        FROM usage_plugin_daily
        WHERE source = ? AND ref_id = ?
          AND day >= CURRENT_DATE - INTERVAL 30 DAY
        GROUP BY day
        ORDER BY day
    """, [source, ref_id]).fetchall()
    by_day = {str(r[0]): int(r[1] or 0) for r in rows}

    import datetime as _dt
    today = _dt.date.today()
    series = []
    for offset in range(29, -1, -1):
        day = today - _dt.timedelta(days=offset)
        day_str = day.isoformat()
        series.append({"day": day_str, "invocations": by_day.get(day_str, 0)})
    return series


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
    stats: Optional[Dict[str, Dict]] = None,
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
    # Lazy enrichment read for the listing card — cached per (marketplace_id,
    # mtime) so a page of 24 plugins from the same marketplace triggers ONE
    # disk read, not 24. Empty dict when curator hasn't filled the fields →
    # listing card falls back to raw name + marketplace.json description.
    enrichment = _curated_plugin_enrichment(marketplace_id, plugin_name)
    ref_id = f"{marketplace_id}/{plugin_name}"
    stat = (stats or {}).get(ref_id, {})
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
        is_system=bool(plugin_row.get("is_system")),
        display_name=enrichment.get("display_name"),
        tagline=enrichment.get("tagline"),
        invocations_30d=stat.get("invocations_30d", 0),
        unique_users_30d=stat.get("unique_users_30d", 0),
        trend_pct=stat.get("trend_pct"),
    )


def _flea_to_item(
    entity: dict,
    *,
    installed_set: set,
    viewer_id: Optional[str] = None,
    stats: Optional[Dict[str, Dict]] = None,
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
    stat = (stats or {}).get(entity["id"], {})
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
        invocations_30d=stat.get("invocations_30d", 0),
        unique_users_30d=stat.get("unique_users_30d", 0),
        trend_pct=stat.get("trend_pct"),
    )


# ---------------------------------------------------------------------------
# GET /api/marketplace/items
# ---------------------------------------------------------------------------


def _build_telemetry(
    conn: duckdb.DuckDBPyConnection,
    source: str,
    ref_id: str,
) -> Optional[Dict[str, Any]]:
    """Build the telemetry dict for detail endpoints.

    Returns None when invocations_30d == 0 (no data yet).
    Otherwise returns {invocations_30d, unique_users_30d, daily_series}.
    """
    stats = _load_invocation_stats(conn, source)
    stat = stats.get(ref_id)
    inv30 = stat["invocations_30d"] if stat else 0
    if inv30 == 0:
        return None
    return {
        "invocations_30d": inv30,
        "unique_users_30d": stat["unique_users_30d"] if stat else 0,
        "daily_series": _load_plugin_daily_series(conn, source, ref_id),
    }


def _apply_sort(
    items: List[MarketplaceItem],
    sort: str,
) -> List[MarketplaceItem]:
    """Sort a list of MarketplaceItem objects in-place and return it.

    - ``recent``    — preserve existing order (no-op).
    - ``most_used`` — DESC by invocations_30d, then DESC install_count, then name ASC.
    - ``trending``  — DESC by trend_pct; items with trend_pct=None are excluded.
    """
    if sort == "most_used":
        items.sort(
            key=lambda it: (-it.invocations_30d, it.name.lower())
        )
    elif sort == "trending":
        items = [it for it in items if it.trend_pct is not None]
        items.sort(key=lambda it: -(it.trend_pct or 0.0))
    return items


@router.get("/items", response_model=ItemListResponse)
async def list_items(
    tab: Literal["curated", "flea", "my"] = Query("curated"),
    q: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    type: Optional[Literal["skill", "agent", "plugin"]] = Query(None),
    sort: Literal["recent", "most_used", "trending"] = Query("recent"),
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

    ``sort`` controls ordering after stats are joined:
      * ``recent``    — existing DB order (default, backward-compatible).
      * ``most_used`` — DESC by invocations_30d, ties by install_count then name.
      * ``trending``  — DESC by trend_pct; items with no trend data are excluded.
    """
    skip = (page - 1) * page_size
    needs_sort = sort != "recent"

    if tab == "curated":
        group_ids = _user_group_ids(user["id"], conn) or set()
        subs = UserCuratedSubscriptionsRepository(conn).subscribed_set(user["id"])
        # When sorting, we need all rows to sort before paginating.
        if needs_sort:
            all_rows, total = MarketplacePluginsRepository(conn).list_with_filters(
                group_ids=group_ids,
                search=q or None,
                category=category or None,
                skip=0,
                limit=10000,
            )
        else:
            all_rows, total = MarketplacePluginsRepository(conn).list_with_filters(
                group_ids=group_ids,
                search=q or None,
                category=category or None,
                skip=skip,
                limit=page_size,
            )
        marketplace_meta: Dict[str, Dict[str, Optional[str]]] = {}
        if all_rows:
            distinct_ids = {r["marketplace_id"] for r in all_rows}
            for mp_id in distinct_ids:
                marketplace_meta[mp_id] = _resolve_marketplace_meta(conn, mp_id)
        curated_stats = _load_invocation_stats(conn, "curated")
        items = [
            _curated_to_item(
                conn, r, subs=subs,
                marketplace_meta=marketplace_meta,
                stats=curated_stats,
            )
            for r in all_rows
        ]
        if needs_sort:
            items = _apply_sort(items, sort)
            total = len(items)
            items = items[skip: skip + page_size]
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
        if needs_sort:
            all_flea_rows, total = StoreEntitiesRepository(conn).list(
                skip=0, limit=10000,
                type=type, category=category, search=q or None,
                visibility_status=visibility_filter,
                include_owner_id=include_owner,
            )
        else:
            all_flea_rows, total = StoreEntitiesRepository(conn).list(
                skip=skip, limit=page_size,
                type=type, category=category, search=q or None,
                visibility_status=visibility_filter,
                include_owner_id=include_owner,
            )
        flea_stats = _load_invocation_stats(conn, "flea")
        items = [
            _flea_to_item(
                r, installed_set=installed_set,
                viewer_id=user["id"],
                stats=flea_stats,
            )
            for r in all_flea_rows
        ]
        if needs_sort:
            items = _apply_sort(items, sort)
            total = len(items)
            items = items[skip: skip + page_size]
        return ItemListResponse(
            items=items, total=total, page=page, page_size=page_size,
        )

    # tab == "my" — see docstring; read directly from source-of-truth tables.
    items: List[MarketplaceItem] = []

    granted = resolve_allowed_plugins(conn, user)
    subs = UserCuratedSubscriptionsRepository(conn).subscribed_set(user["id"])
    marketplace_meta: Dict[str, Dict[str, Optional[str]]] = {}

    # Pull the enriched rows the Curated tab uses (cover_photo_url, video_url,
    # category override, doc_links from marketplace-metadata.json) so the My Stack
    # cards look identical to the curated cards the user just clicked
    # "+ Add to my stack" on. ``resolve_allowed_plugins`` reads only the
    # upstream marketplace.json, which doesn't carry those columns; without
    # this lookup the same plugin renders with its cover photo on
    # ``?tab=curated`` and with a gradient placeholder on ``?tab=my``.
    plugin_repo = MarketplacePluginsRepository(conn)
    subscribed_mp_ids = {mp_id for (mp_id, _) in subs}
    enriched_lookup: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for mp_id in subscribed_mp_ids:
        for row in plugin_repo.list_for_marketplace(mp_id):
            enriched_lookup[(mp_id, row["name"])] = row

    curated_stats = _load_invocation_stats(conn, "curated")
    flea_stats = _load_invocation_stats(conn, "flea")

    for p in granted:
        key = (p["marketplace_id"], p["original_name"])
        if key not in subs:
            continue
        mp_id = p["marketplace_id"]
        if mp_id not in marketplace_meta:
            marketplace_meta[mp_id] = _resolve_marketplace_meta(conn, mp_id)

        plugin_row = enriched_lookup.get(key)
        if plugin_row is None:
            # Fallback: plugin in RBAC + subscribed but not yet ingested into
            # marketplace_plugins (rare race — granted before the first sync
            # cycle runs). Build the bare shape from the on-disk manifest so
            # the card still renders, just without marketplace-metadata enrichment;
            # cover falls through to the gradient placeholder until the next
            # sync.
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
            stats=curated_stats,
        ))

    flea_installs = UserStoreInstallsRepository(conn).list_for_user(user["id"])
    flea_installed_set = {row["id"] for row in flea_installs}
    for entity in flea_installs:
        items.append(_flea_to_item(
            entity, installed_set=flea_installed_set, stats=flea_stats,
        ))

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
    items = _apply_sort(items, sort)
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
            # Curated plugins are always type='plugin'. When the type filter
            # is set to skill/agent, those rows won't show in the items grid
            # (filtered out by the items endpoint at line 579), so they must
            # not contribute to the category counts either — otherwise the
            # pill counts overstate what the user will actually see.
            if type is None or type == "plugin":
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
    """Build ``InnerItemSummary`` objects from the shared listing helper.

    The shared helper (``src.marketplace_listing.list_inner_skills``) returns
    plain names; the API layer re-reads each SKILL.md to populate the
    ``description`` field used on the detail page.
    """
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
    """Build ``InnerItemSummary`` objects from the shared listing helper."""
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
    """Return ``commands/*.md`` as ``CommandEntry`` objects from frontmatter.

    Names from the shared ``src.marketplace_listing.list_commands`` helper
    already carry the leading ``/``; re-read here to pick up ``description``.
    """
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
        raw = (fm.get("name") or p.stem or "").strip()
        if not raw:
            continue
        name = raw if raw.startswith("/") else f"/{raw}"
        out.append(CommandEntry(
            name=name,
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

    # v32: enrich each skill/agent card with its marketplace-metadata cover photo
    # so the inner cards on the plugin detail page render the real image
    # instead of just initials. Read marketplace-metadata + mirror manifest once
    # (mtime cache makes the metadata read free on cache hit) and reuse for
    # all inner items in the plugin.
    inner_metadata = _read_metadata_cached(marketplace_id)
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

    # v32: cover, video, and doc_links come from `marketplace-metadata.json` via the
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

    # Plugin-level rich content (display_name, tagline, description_long_html,
    # use_cases, sample_interaction) — on-demand read from the working tree's
    # marketplace-metadata.json so curator edits land at next page refresh
    # without a sync cycle. Empty dict when curator hasn't filled any of the
    # new fields → PluginDetailResponse renders the historical fallback shape.
    enrichment = _curated_plugin_enrichment(marketplace_id, plugin_name)

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
        is_system=bool(plugin_row.get("is_system")),
        **enrichment,
        telemetry=_build_telemetry(
            conn, "curated", f"{marketplace_id}/{plugin_name}",
        ),
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
        telemetry=_build_telemetry(conn, "flea", entity_id),
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
    # v39: system plugins are mandatory for every user — refuse uninstall.
    sys_row = conn.execute(
        "SELECT is_system FROM marketplace_plugins "
        "WHERE marketplace_id = ? AND name = ?",
        [marketplace_id, plugin_name],
    ).fetchone()
    if sys_row and bool(sys_row[0]):
        raise HTTPException(
            status_code=409,
            detail="cannot_uninstall_system_plugin",
        )

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


def _load_mirror_manifest(marketplace_id: str) -> Dict[Tuple[str, str], Any]:
    """Read the asset-mirror manifest for one marketplace, keyed by
    ``(plugin_name, url)``.

    Returns an empty dict when the cache directory or manifest doesn't exist
    (fresh install, marketplace never synced) — callers treat that as "no
    URL is mirrored" which collapses to the same path as a fetch failure.
    """
    from src.marketplace_asset_mirror import _load_manifest

    cache_dir = get_marketplace_cache_dir() / marketplace_id
    return _load_manifest(cache_dir)


def _resolve_external_via_mirror(
    marketplace_id: str,
    plugin_name: str,
    url: str,
    manifest: Dict[Tuple[str, str], Any],
) -> Optional[str]:
    """Translate one external URL into the Agnes-served `/mirrored/` URL when
    the asset mirror successfully cached it for THIS plugin; ``None`` otherwise.

    Used by the inner-detail (skill/agent) enrichment to apply the same
    "drop entries Agnes can't deliver" rule that the plugin-level sync
    flow already enforces. When this returns None the caller drops the
    enrichment entry entirely (no broken external link surfaces in the UI).
    """
    entry = manifest.get((plugin_name, url))
    if entry is None or entry.status != "ok" or not entry.local:
        return None
    # Manifest stores `local` as `<plugin>/<rest>`; the /mirrored/ endpoint
    # expects just `<rest>` (the plugin segment is in the URL path). Same
    # transform as src/marketplace.py uses on the plugin-level path.
    rest = entry.local.split("/", 1)[1] if "/" in entry.local else entry.local
    return mirrored_url(marketplace_id, plugin_name, rest)


def _safe_use_case(raw: Any) -> Optional[UseCase]:
    """Build a ``UseCase`` from curator JSON, skipping malformed entries.

    Curator-authored input — a missing or empty ``title`` / ``description`` /
    ``prompt`` returns ``None`` so the caller drops the card instead of
    500-ing the whole detail page on Pydantic's required-field validation.
    """
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    description = raw.get("description")
    prompt = raw.get("prompt")
    if not (title and description and prompt):
        return None
    return UseCase(title=title, description=description, prompt=prompt)


def _safe_sample_interaction(raw: Any) -> Optional[SampleInteraction]:
    """Build a ``SampleInteraction`` from curator JSON, skipping malformed input.

    Same rationale as :func:`_safe_use_case` — partial curator JSON should
    silently drop the section, not crash the endpoint.
    """
    from app.markdown_render import render_safe

    if not isinstance(raw, dict):
        return None
    user = raw.get("user")
    assistant = raw.get("assistant")
    if not (user and assistant):
        return None
    return SampleInteraction(
        user=user,
        assistant=assistant,
        assistant_html=render_safe(assistant),
    )


def _curated_inner_enrichment(
    marketplace_id: str,
    plugin_name: str,
    kind: str,
    inner_name: str,
) -> Dict[str, Any]:
    """Load marketplace-metadata.json sub-tree for a single skill/agent.

    Lives here (not in the sync pipeline) so curators can update marketplace-metadata
    on a working tree and see the change at the next page refresh, without
    waiting for a full plugin-cache rewrite. Read on every inner-detail
    request — marketplace-metadata.json is small enough that disk hit cost is
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
    from src.marketplace_metadata import resolve_inner_metadata

    repo_root = Path(get_marketplaces_dir()) / marketplace_id
    # Route through the mtime cache so skill / agent detail hits don't
    # re-parse the JSON on every request. Plugin listing already shares
    # this cache, so opening 5 inner-detail pages on a marketplace whose
    # metadata file hasn't changed = 0 disk reads beyond the first.
    metadata = _read_metadata_cached(marketplace_id)
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

    from app.markdown_render import render_safe

    out: Dict[str, Any] = {
        "cover_photo_url": cover_url,
        "video_url": resolved.get("video_url"),
        "docs": docs,
    }
    # Rich user-facing fields — same shape as the plugin-level enrichment.
    # Presence check (``in``) rather than truthy check so the resolver's
    # contract is respected: if the resolver decided to include a key,
    # the API propagates it. Future falsy-but-valid fields (e.g. a bool
    # ``featured: false`` or numeric ``priority: 0``) inherit correctly
    # instead of silently falling back to the parent — the trap @cvrysanek
    # flagged in the multi-persona review. String fields stay truthy-
    # guarded by the resolver itself, so today's behaviour is unchanged.
    if "display_name" in resolved:
        out["display_name"] = resolved["display_name"]
    if "tagline" in resolved:
        out["tagline"] = resolved["tagline"]
    # Per-item category override — caller merges with the parent plugin's
    # category, preferring the override when set.
    if "category" in resolved:
        out["category"] = resolved["category"]
    if "description" in resolved:
        out["description_long_html"] = render_safe(resolved["description"])
    if "when_to_use" in resolved:
        out["when_to_use_html"] = render_safe(resolved["when_to_use"])
    if "invocation" in resolved:
        out["invocation"] = resolved["invocation"]

    use_cases_raw = resolved.get("use_cases") or []
    if use_cases_raw:
        cards = [_safe_use_case(uc) for uc in use_cases_raw]
        cards = [c for c in cards if c is not None]
        if cards:
            out["use_cases"] = cards
    sample = _safe_sample_interaction(resolved.get("sample_interaction"))
    if sample is not None:
        out["sample_interaction"] = sample
    return out


# Module-level cache for marketplace-metadata.json reads. Keyed by
# ``(marketplace_id, mtime_ns)`` so a curator's `git push` (which updates
# the file's mtime after the next sync's `git pull`) implicitly invalidates
# the cached parse. The 24-plugin listing endpoint reads from the same
# marketplace's metadata 24 times per page; without this cache that's 24
# disk reads + JSON parses per request. With cache: 1.
#
# OrderedDict + popitem(last=False) on overflow gives us a bounded LRU
# without a third-party dependency. Earlier versions used a per-marketplace
# eviction predicate that only swept stale entries for the CURRENT marketplace
# — at N>100 distinct marketplaces the inner predicate matched zero entries,
# so the cap silently failed and memory grew linearly. The bounded LRU here
# guarantees the dict size never exceeds _PLUGIN_METADATA_CACHE_MAX.
_PLUGIN_METADATA_CACHE_MAX = 256
_PLUGIN_METADATA_CACHE: "OrderedDict[Tuple[str, int], Dict[str, Any]]" = OrderedDict()


def _read_metadata_cached(marketplace_id: str) -> Dict[str, Any]:
    """Return the parsed marketplace-metadata.json for a given marketplace.

    Cached by mtime so curator edits land at next request without explicit
    invalidation. Missing / malformed files degrade to ``{}`` (delegated to
    :func:`src.marketplace_metadata.read_marketplace_metadata` which already
    swallows OSError / JSONDecodeError). Cache is bounded LRU; the oldest
    entry is dropped when the cap is reached.
    """
    from src.marketplace_metadata import (
        MARKETPLACE_METADATA_REL,
        read_marketplace_metadata,
    )
    repo_root = Path(get_marketplaces_dir()) / marketplace_id
    metadata_path = repo_root / MARKETPLACE_METADATA_REL
    try:
        mtime_ns = metadata_path.stat().st_mtime_ns
    except OSError:
        # File doesn't exist or unreadable — read_marketplace_metadata handles
        # the same case below; we just skip the cache lookup.
        return read_marketplace_metadata(repo_root)
    key = (marketplace_id, mtime_ns)
    cached = _PLUGIN_METADATA_CACHE.get(key)
    if cached is not None:
        # Touch the entry so the LRU bookkeeping treats it as recently used.
        _PLUGIN_METADATA_CACHE.move_to_end(key)
        return cached
    parsed = read_marketplace_metadata(repo_root)
    _PLUGIN_METADATA_CACHE[key] = parsed
    # Bounded LRU: drop oldest entries until we're back under the cap.
    while len(_PLUGIN_METADATA_CACHE) > _PLUGIN_METADATA_CACHE_MAX:
        _PLUGIN_METADATA_CACHE.popitem(last=False)
    return parsed


def _curated_plugin_enrichment(
    marketplace_id: str,
    plugin_name: str,
) -> Dict[str, Any]:
    """Load plugin-level rich content from marketplace-metadata.json.

    Returns a dict shaped for direct merge into ``PluginDetailResponse``:
    ``display_name``, ``tagline``, ``description_long_html``, ``use_cases``,
    ``sample_interaction`` — any subset of those keys may be missing when
    the curator hasn't filled the corresponding field.

    Markdown is rendered to safe HTML here (via
    :func:`app.markdown_render.render_safe`) so the template can inject the
    body with ``{{ x | safe }}`` without a client-side markdown library.

    This is the plugin-level analogue of ``_curated_inner_enrichment`` — both
    read on demand from the working tree so curator edits don't need a full
    sync cycle to land in the UI. The other persisted plugin-level fields
    (cover_photo_url, video_url, category, doc_links) continue to flow via
    ``marketplace_plugins`` rows written at sync time — only the NEW rich
    fields are read on-demand here.
    """
    from app.markdown_render import render_safe
    from src.marketplace_metadata import resolve_plugin_metadata

    metadata = _read_metadata_cached(marketplace_id)
    resolved = resolve_plugin_metadata(metadata, plugin_name)
    if not resolved:
        return {}

    out: Dict[str, Any] = {}
    # Presence check (`in`) so future falsy-but-valid resolver fields
    # (bool / int / "" with explicit-empty semantics) survive — see the
    # parallel comment in `_curated_inner_enrichment` for the trap rationale.
    if "display_name" in resolved:
        out["display_name"] = resolved["display_name"]
    if "tagline" in resolved:
        out["tagline"] = resolved["tagline"]
    if "description" in resolved:
        out["description_long_html"] = render_safe(resolved["description"])

    use_cases_raw = resolved.get("use_cases") or []
    if use_cases_raw:
        cards = [_safe_use_case(uc) for uc in use_cases_raw]
        cards = [c for c in cards if c is not None]
        if cards:
            out["use_cases"] = cards

    sample = _safe_sample_interaction(resolved.get("sample_interaction"))
    if sample is not None:
        out["sample_interaction"] = sample

    return out


def _curated_inner_cover(
    marketplace_id: str,
    plugin_name: str,
    kind: str,
    inner_name: str,
    manifest: Optional[Dict[Tuple[str, str], Any]] = None,
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
        # Route through the mtime cache; this helper gets called once per
        # inner item on the parent-plugin's skills/agents card list, so
        # cache miss only happens on the first card.
        metadata = _read_metadata_cached(marketplace_id)
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
    # Merge parent fallback fields with curator overrides BEFORE unpacking
    # — Python function-call `**a, **b` with overlapping keys raises
    # TypeError, it doesn't merge like a literal dict does. Today only
    # `category` overlaps (both parent + enrichment may set it), but the
    # explicit merge keeps the unpack future-proof against any new field
    # added to both layers.
    merged = {**parent, **enrichment}
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
        **merged,
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
    # See curated_skill_detail above — explicit merge avoids the
    # TypeError that `**parent, **enrichment` raises when both supply
    # an overlapping key (e.g. `category`).
    merged = {**parent, **enrichment}
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
        **merged,
    )


# ---------------------------------------------------------------------------
# Asset / doc / mirrored serving endpoints (v32)
# ---------------------------------------------------------------------------
#
# Three sibling endpoints that serve the binary content referenced from
# `marketplace-metadata.json`. All three:
#
#   * are gated by `require_resource_access(MARKETPLACE_PLUGIN, "{mp}/{plugin}")`
#     so a user without RBAC can't side-load assets even with a direct URL,
#   * resolve a candidate path with `Path.resolve(strict=True)` and verify the
#     result lives under the expected root via `is_relative_to()` — defense
#     against `..` / absolute paths / symlinks pointing out of the tree,
#   * use FastAPI's `FileResponse` so Content-Type detection comes from the
#     stdlib mimetypes module (good enough for the allowlisted set; binary
#     fallback for anything we don't recognize).


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
    referenced from ``marketplace-metadata.json`` and from inner skill / agent
    cards. A curator who could land an arbitrary file in the cloned repo
    (HTML, JS, SVG with inline ``<script>``) would otherwise have a
    same-origin XSS via this endpoint, since the response shares the
    cookie scope with ``/admin`` and ``/api/me/*``. Three layered checks:

    1. Extension must be in :data:`src.marketplace_asset_validation.IMAGE_EXTENSIONS`
       (``.png``/``.jpg``/``.jpeg``/``.webp``); anything else → 415.
    2. Body must pass :func:`src.marketplace_asset_validation.validate_image_file`
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
    from src.marketplace_asset_validation import IMAGE_EXTENSIONS, validate_image_file

    repo_root = Path(get_marketplaces_dir()) / marketplace_id
    if not repo_root.exists():
        raise HTTPException(status_code=404, detail="marketplace_not_synced")
    safe = _safe_join(repo_root, path)
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
    even though the marketplace-metadata parser already rejects out-of-allowlist
    extensions during the doc_link parse — a curator who edits the working
    tree directly (or whose JSON survived parsing because of a generic
    extension match elsewhere) shouldn't be able to land a .docx through
    a re-served doc URL.

    Force-download via Content-Disposition: attachment — clicking a doc
    link in the UI saves the file to disk rather than opening it in a tab.
    """
    from src.marketplace_asset_validation import DOC_EXTENSIONS

    repo_root = Path(get_marketplaces_dir()) / marketplace_id
    if not repo_root.exists():
        raise HTTPException(status_code=404, detail="marketplace_not_synced")
    safe = _safe_join(repo_root, path)
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
    safe = _safe_join(cache_root, key)
    if safe is None or not safe.is_file():
        raise HTTPException(status_code=404, detail="mirrored_asset_not_found")
    if key.startswith("docs/"):
        return FileResponse(safe, headers=_doc_disposition(safe.name))
    return FileResponse(safe)
