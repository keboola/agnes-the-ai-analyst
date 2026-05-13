"""Admin endpoints for marketplace git repositories.

CRUD + on-demand "Sync now" mirroring the /api/users shape. Tokens supplied
through the admin UI are persisted to data/state/.env_overlay (same pattern
as /api/admin/configure for Keboola/BigQuery) — never stored in the DB.
"""

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from app.resource_types import ResourceType
from app.secrets import persist_overlay_token
from src.marketplace import (
    MarketplaceNotFound,
    delete_marketplace_dir,
    is_valid_slug,
    sync_marketplaces,
    sync_one,
)
from src.repositories.audit import AuditRepository
from src.repositories.marketplace_plugins import MarketplacePluginsRepository
from src.repositories.marketplace_registry import MarketplaceRegistryRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/marketplaces", tags=["marketplaces"])


# ---------------------------------------------------------------------------
# Audit helper — same shape as app/api/users.py::_audit
# ---------------------------------------------------------------------------


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    target_id: str,
    params: Optional[dict] = None,
) -> None:
    try:
        safe_params = None
        if params:
            safe_params = {}
            for k, v in params.items():
                if isinstance(v, datetime):
                    safe_params[k] = v.isoformat()
                else:
                    safe_params[k] = v
        AuditRepository(conn).log(
            user_id=actor_id,
            action=action,
            resource=f"marketplace:{target_id}",
            params=safe_params,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class CreateMarketplaceRequest(BaseModel):
    name: str
    slug: str
    url: str
    branch: Optional[str] = None
    description: Optional[str] = None
    token: Optional[str] = None
    # v32: required at create time. Surfaced on /marketplace cards + plugin
    # detail in place of the historical owner_todo placeholder. The plan
    # decision was to validate at the application layer (no DB NOT NULL)
    # so existing rows survive the schema migration without forcing all
    # admins to refill before the next request lands.
    curator_name: Optional[str] = None
    curator_email: Optional[str] = None


class UpdateMarketplaceRequest(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    branch: Optional[str] = None
    description: Optional[str] = None
    # None = leave untouched; empty string = clear token; non-empty = rotate
    token: Optional[str] = None
    # Either field None = leave untouched; non-empty string = update.
    # Empty string is treated the same as None on update so admins can't
    # accidentally null out a curator by submitting an empty form input.
    curator_name: Optional[str] = None
    curator_email: Optional[str] = None


class MarketplaceResponse(BaseModel):
    id: str
    name: str
    url: str
    branch: Optional[str] = None
    description: Optional[str] = None
    registered_by: Optional[str] = None
    registered_at: Optional[str] = None
    last_synced_at: Optional[str] = None
    last_commit_sha: Optional[str] = None
    last_error: Optional[str] = None
    has_token: bool = False
    plugin_count: int = 0
    curator_name: Optional[str] = None
    curator_email: Optional[str] = None


# Liberal email regex — RFC 5322 is too permissive to be useful at the
# admin-form layer. Anchored, requires `local@domain.tld`, no whitespace,
# 1-254 chars total. Matches what /admin UI expects an admin to type.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _to_response(row: dict, plugin_count: int = 0) -> MarketplaceResponse:
    token_env = row.get("token_env") or ""
    has_token = bool(token_env) and bool(os.environ.get(token_env, ""))
    return MarketplaceResponse(
        id=row["id"],
        name=row["name"],
        url=row["url"],
        branch=row.get("branch"),
        description=row.get("description"),
        registered_by=row.get("registered_by"),
        registered_at=str(row["registered_at"]) if row.get("registered_at") else None,
        last_synced_at=str(row["last_synced_at"]) if row.get("last_synced_at") else None,
        last_commit_sha=row.get("last_commit_sha"),
        last_error=row.get("last_error"),
        has_token=has_token,
        plugin_count=plugin_count,
        curator_name=row.get("curator_name"),
        curator_email=row.get("curator_email"),
    )


class PluginResponse(BaseModel):
    name: str
    description: Optional[str] = None
    version: Optional[str] = None
    author_name: Optional[str] = None
    homepage: Optional[str] = None
    category: Optional[str] = None
    source_type: Optional[str] = None
    source_spec: Optional[Any] = None
    # v39: surfaced so the admin Details modal renders the SYSTEM pill
    # + flips the "Mark as system" / "Unmark system" toggle button.
    is_system: bool = False


class SystemFlagResponse(BaseModel):
    """Return shape of the mark/unmark_system endpoints."""

    marketplace_id: str
    plugin_name: str
    is_system: bool
    affected_groups: int = 0
    affected_users: int = 0


# ---------------------------------------------------------------------------
# Token env-var naming
# ---------------------------------------------------------------------------
#
# Read-modify-write of `.env_overlay` lives in `app.secrets.persist_overlay_token`
# (single shared helper with a process-wide lock). Multiple admins clicking
# Save in /admin/marketplaces + /admin/server-config concurrently must not
# corrupt the overlay file.


def _token_env_name(slug: str) -> str:
    """Derive a conventional env-var name from a slug.

    "foundry-ai" -> "AGNES_MARKETPLACE_FOUNDRY_AI_TOKEN"
    """
    normalized = slug.upper().replace("-", "_")
    return f"AGNES_MARKETPLACE_{normalized}_TOKEN"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=List[MarketplaceResponse])
async def list_marketplaces(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    counts = MarketplacePluginsRepository(conn).count_by_marketplace()
    return [
        _to_response(row, counts.get(row["id"], 0))
        for row in MarketplaceRegistryRepository(conn).list_all()
    ]


@router.get("/{marketplace_id}/plugins", response_model=List[PluginResponse])
async def list_plugins(
    marketplace_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return the cached plugin list for a marketplace.

    Rows come from `marketplace_plugins`, which is refreshed from
    `.claude-plugin/marketplace.json` on every successful sync. An
    unsynced marketplace will return an empty list.
    """
    if not MarketplaceRegistryRepository(conn).get(marketplace_id):
        raise HTTPException(status_code=404, detail="marketplace not found")
    rows = MarketplacePluginsRepository(conn).list_for_marketplace(marketplace_id)
    return [
        PluginResponse(
            name=r["name"],
            description=r.get("description"),
            version=r.get("version"),
            author_name=r.get("author_name"),
            homepage=r.get("homepage"),
            category=r.get("category"),
            source_type=r.get("source_type"),
            source_spec=r.get("source_spec"),
            is_system=bool(r.get("is_system")),
        )
        for r in rows
    ]


@router.post("", response_model=MarketplaceResponse, status_code=201)
async def create_marketplace(
    payload: CreateMarketplaceRequest,
    request: Request,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    slug = (payload.slug or "").strip().lower()
    if not is_valid_slug(slug):
        raise HTTPException(
            status_code=400,
            detail="slug must match [a-z0-9][a-z0-9_-]{0,63} (1-64 chars, start with alnum)",
        )
    if not (payload.url or "").strip().lower().startswith("https://"):
        raise HTTPException(status_code=400, detail="url must start with https://")
    if not (payload.name or "").strip():
        raise HTTPException(status_code=400, detail="name is required")

    # v32: curator is mandatory at create time. Validation lives here (not in
    # DB schema) so legacy rows that pre-date the column survive — admins
    # patch them via the edit modal at their leisure.
    curator_name = (payload.curator_name or "").strip()
    curator_email = (payload.curator_email or "").strip()
    if not curator_name:
        raise HTTPException(
            status_code=400, detail="curator_name is required",
        )
    if not curator_email:
        raise HTTPException(
            status_code=400, detail="curator_email is required",
        )
    if not _EMAIL_RE.match(curator_email):
        raise HTTPException(
            status_code=400, detail="curator_email is not a valid email address",
        )

    repo = MarketplaceRegistryRepository(conn)
    if repo.get(slug):
        raise HTTPException(status_code=409, detail=f"marketplace '{slug}' already exists")

    token_env: Optional[str] = None
    if payload.token:
        token_env = _token_env_name(slug)
        persist_overlay_token(token_env, payload.token)

    repo.register(
        id=slug,
        name=payload.name.strip(),
        url=payload.url.strip(),
        branch=(payload.branch or "").strip() or None,
        token_env=token_env,
        description=payload.description,
        registered_by=user.get("email"),
        curator_name=curator_name,
        curator_email=curator_email,
    )
    _audit(
        conn,
        user["id"],
        "marketplace.create",
        slug,
        {"url": payload.url, "has_token": bool(payload.token)},
    )
    return _to_response(repo.get(slug))


@router.patch("/{marketplace_id}", response_model=MarketplaceResponse)
async def update_marketplace(
    marketplace_id: str,
    payload: UpdateMarketplaceRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = MarketplaceRegistryRepository(conn)
    existing = repo.get(marketplace_id)
    if not existing:
        raise HTTPException(status_code=404, detail="marketplace not found")

    # Start with the existing row; override fields the caller provided.
    updated = {
        "id": existing["id"],
        "name": existing["name"],
        "url": existing["url"],
        "branch": existing.get("branch"),
        "token_env": existing.get("token_env"),
        "description": existing.get("description"),
        "registered_by": existing.get("registered_by"),
        "curator_name": existing.get("curator_name"),
        "curator_email": existing.get("curator_email"),
    }
    changed: dict = {}
    if payload.name is not None:
        if not payload.name.strip():
            raise HTTPException(status_code=400, detail="name cannot be empty")
        updated["name"] = payload.name.strip()
        changed["name"] = updated["name"]
    if payload.url is not None:
        url = payload.url.strip()
        if not url.lower().startswith("https://"):
            raise HTTPException(status_code=400, detail="url must start with https://")
        updated["url"] = url
        changed["url"] = url
    if payload.branch is not None:
        updated["branch"] = payload.branch.strip() or None
        changed["branch"] = updated["branch"]
    if payload.description is not None:
        updated["description"] = payload.description
        changed["description"] = payload.description

    # Curator fields: empty-string treated as "no change" so an admin
    # editing only the URL doesn't accidentally null out curator metadata.
    if payload.curator_name is not None and payload.curator_name.strip():
        updated["curator_name"] = payload.curator_name.strip()
        changed["curator_name"] = updated["curator_name"]
    if payload.curator_email is not None and payload.curator_email.strip():
        if not _EMAIL_RE.match(payload.curator_email.strip()):
            raise HTTPException(
                status_code=400,
                detail="curator_email is not a valid email address",
            )
        updated["curator_email"] = payload.curator_email.strip()
        changed["curator_email"] = updated["curator_email"]

    if payload.token is not None:
        # None = untouched; "" = clear token_env binding; non-empty = rotate.
        if payload.token == "":
            if updated["token_env"]:
                persist_overlay_token(updated["token_env"], "")
            updated["token_env"] = None
            changed["token"] = "cleared"
        else:
            env_name = _token_env_name(marketplace_id)
            persist_overlay_token(env_name, payload.token)
            updated["token_env"] = env_name
            changed["token"] = "rotated"

    # Mandatory curator on UPDATE too — legacy rows that pre-date v32 have
    # NULL curator and survive the migration, but the moment an admin opens
    # the edit modal they must fill the gap. The previous PATCH flow let
    # URL/description tweaks persist indefinitely with OWNER_TODO_PLACEHOLDER
    # showing on every /marketplace card. The DB column itself stays nullable
    # so untouched legacy rows are not broken.
    if not (updated.get("curator_name") or "").strip():
        raise HTTPException(
            status_code=400, detail="curator_name is required",
        )
    if not (updated.get("curator_email") or "").strip():
        raise HTTPException(
            status_code=400, detail="curator_email is required",
        )

    repo.register(
        id=updated["id"],
        name=updated["name"],
        url=updated["url"],
        branch=updated["branch"],
        token_env=updated["token_env"],
        description=updated["description"],
        registered_by=updated["registered_by"],
        curator_name=updated["curator_name"],
        curator_email=updated["curator_email"],
    )
    _audit(conn, user["id"], "marketplace.update", marketplace_id, changed)
    counts = MarketplacePluginsRepository(conn).count_by_marketplace()
    return _to_response(repo.get(marketplace_id), counts.get(marketplace_id, 0))


@router.delete("/{marketplace_id}", status_code=204)
async def delete_marketplace(
    marketplace_id: str,
    purge: bool = False,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = MarketplaceRegistryRepository(conn)
    existing = repo.get(marketplace_id)
    if not existing:
        raise HTTPException(status_code=404, detail="marketplace not found")

    # Also clear any overlay token binding so a re-created marketplace of the
    # same slug doesn't accidentally inherit the old PAT.
    if existing.get("token_env"):
        persist_overlay_token(existing["token_env"], "")

    repo.unregister(marketplace_id)
    # Drop cached plugin rows and any resource grants that reference plugins
    # from this marketplace. resource_grants stores resource_id as
    # "<marketplace_slug>/<plugin_name>" — match the slash-prefix via
    # starts_with(), not LIKE: marketplace slugs may contain '_' (validated
    # by [a-z0-9][a-z0-9_-]{0,63}) and LIKE would interpret it as a
    # single-char wildcard, silently dropping grants from sibling
    # marketplaces whose slug differs by exactly one character.
    try:
        conn.execute(
            "DELETE FROM marketplace_plugins WHERE marketplace_id = ?",
            [marketplace_id],
        )
        conn.execute(
            "DELETE FROM resource_grants "
            "WHERE resource_type = ? AND starts_with(resource_id, ? || '/')",
            [ResourceType.MARKETPLACE_PLUGIN.value, marketplace_id],
        )
        # Drop user subscriptions to plugins from this marketplace so a
        # re-registered slug doesn't inherit stale subscribe state.
        from src.repositories.user_curated_subscriptions import (
            UserCuratedSubscriptionsRepository,
        )
        UserCuratedSubscriptionsRepository(conn).delete_for_marketplace(
            marketplace_id
        )
    except Exception as e:
        logger.warning("cleanup for marketplace %s failed: %s", marketplace_id, e)
    purged = False
    if purge:
        try:
            purged = delete_marketplace_dir(marketplace_id)
        except Exception as e:
            logger.warning("delete_marketplace_dir(%s) failed: %s", marketplace_id, e)

    _audit(
        conn,
        user["id"],
        "marketplace.delete",
        marketplace_id,
        {"purged_disk": purged},
    )


@router.post("/{marketplace_id}/sync")
async def trigger_sync(
    marketplace_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    try:
        result = sync_one(marketplace_id)
    except MarketplaceNotFound:
        raise HTTPException(status_code=404, detail="marketplace not found")
    except (RuntimeError, ValueError) as e:
        _audit(conn, user["id"], "marketplace.sync_failed", marketplace_id, {"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))
    _audit(
        conn,
        user["id"],
        "marketplace.sync",
        marketplace_id,
        {"commit": result["commit"], "action": result["action"]},
    )
    return result


@router.post("/sync-all")
def trigger_sync_all(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Sync every registered marketplace.

    Wired up so the scheduler service can drive the nightly refresh over
    HTTP. The previous implementation called ``src.marketplace.sync_marketplaces``
    in-process from the scheduler container, which conflicted with the app's
    long-lived ``system.duckdb`` handle (DuckDB allows only one writer per
    file across processes). Routing through the app inherits the existing
    connection without contention.

    Declared ``def`` (not ``async def``) so FastAPI runs it in a thread
    pool — :func:`sync_marketplaces` does blocking I/O (subprocess git
    clones with ``GIT_TIMEOUT_SEC=300`` per repo, DuckDB writes, a
    process-wide threading.Lock) and would freeze the event loop for the
    duration of a bulk sync if it ran on the asyncio thread. Health
    checks, login redirects, and every other concurrent request keep
    serving while the bulk sync churns through the registry.

    One audit row per call summarises the outcome — per-marketplace details
    live in ``marketplace_registry`` and the per-call result payload below.
    """
    result = sync_marketplaces()
    # _audit appends "marketplace:" to the target id when writing the
    # resource column. "_all" produces "marketplace:_all" — a stable,
    # greppable sentinel for bulk-sync rows; the real per-marketplace
    # commit/error breakdown is in the params payload.
    _audit(
        conn,
        user["id"],
        "marketplace.sync_all",
        "_all",
        {
            "synced": [r.get("id") for r in result.get("synced", [])],
            "errors": [{"id": e.get("id"), "error": e.get("error")} for e in result.get("errors", [])],
        },
    )
    return result


# ---------------------------------------------------------------------------
# v39: system plugin mark / unmark
#
# A plugin marked as "system" is materialized into:
#   * resource_grants — one row per existing user_groups row
#   * user_plugin_optouts — one row per existing users row
# so the resolver's existing (rbac ∩ subscriptions) computation naturally
# includes it for every user. The UI then locks the corresponding controls
# (admin can't revoke per-group, user can't unsubscribe). Unmark flips the
# flag only — materialized rows survive so unmark cannot accidentally rip
# the plugin out of every user's stack mid-day; admin curates the cleanup
# afterwards via the standard resource_grants UI.
# ---------------------------------------------------------------------------


def _invalidate_marketplace_etag() -> None:
    """Drop the served-marketplace ETag cache so the next ZIP / git fetch
    rehashes against the post-mark/post-unmark RBAC view. Best-effort —
    the cache layer survives an import error during early startup."""
    try:
        from app.marketplace_server import packager
        packager.invalidate_etag_cache()
    except Exception:
        logger.exception("failed to invalidate marketplace etag cache")


@router.post(
    "/{marketplace_id}/plugins/{plugin_name}/system",
    response_model=SystemFlagResponse,
)
def mark_plugin_system(
    marketplace_id: str,
    plugin_name: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Mark a plugin as system (mandatory for every user).

    Idempotent — re-running a mark on an already-system plugin still
    runs the fanout (cheap; ON CONFLICT DO NOTHING) so any user/group
    that slipped past the creation hooks gets caught up.
    """
    from src.repositories.resource_grants import ResourceGrantsRepository
    from src.repositories.user_curated_subscriptions import (
        UserCuratedSubscriptionsRepository,
    )

    plugin_row = conn.execute(
        "SELECT 1 FROM marketplace_plugins WHERE marketplace_id = ? AND name = ?",
        [marketplace_id, plugin_name],
    ).fetchone()
    if not plugin_row:
        raise HTTPException(status_code=404, detail="plugin not found")

    resource_id = f"{marketplace_id}/{plugin_name}"
    affected_groups = 0
    affected_users = 0

    conn.execute(
        "UPDATE marketplace_plugins SET is_system = TRUE "
        "WHERE marketplace_id = ? AND name = ?",
        [marketplace_id, plugin_name],
    )

    # Pivot fanout: this plugin × every group / every user. We
    # intentionally do NOT wrap the loop in a single BEGIN/COMMIT —
    # DuckDB aborts the whole transaction on a ConstraintException, so
    # an idempotent re-run (where most rows are duplicates) would die
    # on the very first existing grant. Each row stands alone here:
    # duplicate grants raise ConstraintException which we swallow
    # (mirrors ``ON CONFLICT DO NOTHING`` semantics without DuckDB
    # multi-target conflict resolution); duplicate subscriptions go
    # through DuckDB's ON CONFLICT on the PK, which is well-supported.
    # Partial-application is safe: a re-run completes the remainder.
    groups = conn.execute("SELECT id FROM user_groups").fetchall()
    grants_repo = ResourceGrantsRepository(conn)
    actor_email = user.get("email") or user.get("id")
    for (group_id,) in groups:
        try:
            grants_repo.create(
                group_id=group_id,
                resource_type=ResourceType.MARKETPLACE_PLUGIN.value,
                resource_id=resource_id,
                assigned_by=actor_email,
            )
            affected_groups += 1
        except duckdb.ConstraintException:
            continue

    affected_users = UserCuratedSubscriptionsRepository(
        conn,
    ).fanout_system_for_plugin(marketplace_id, plugin_name)

    _audit(
        conn,
        user["id"],
        "marketplace.plugin.mark_system",
        f"{marketplace_id}/{plugin_name}",
        {"affected_groups": affected_groups, "affected_users": affected_users},
    )
    _invalidate_marketplace_etag()

    return SystemFlagResponse(
        marketplace_id=marketplace_id,
        plugin_name=plugin_name,
        is_system=True,
        affected_groups=affected_groups,
        affected_users=affected_users,
    )


@router.delete(
    "/{marketplace_id}/plugins/{plugin_name}/system",
    response_model=SystemFlagResponse,
)
def unmark_plugin_system(
    marketplace_id: str,
    plugin_name: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Flip ``is_system`` to FALSE. Materialized grants/subscriptions
    survive — admin curates cleanup via the standard /admin/access UI
    (which immediately unlocks the checkboxes for this plugin) and
    users can unsubscribe normally on /marketplace?tab=my.
    """
    plugin_row = conn.execute(
        "SELECT 1 FROM marketplace_plugins WHERE marketplace_id = ? AND name = ?",
        [marketplace_id, plugin_name],
    ).fetchone()
    if not plugin_row:
        raise HTTPException(status_code=404, detail="plugin not found")

    conn.execute(
        "UPDATE marketplace_plugins SET is_system = FALSE "
        "WHERE marketplace_id = ? AND name = ?",
        [marketplace_id, plugin_name],
    )
    _audit(
        conn,
        user["id"],
        "marketplace.plugin.unmark_system",
        f"{marketplace_id}/{plugin_name}",
        None,
    )
    _invalidate_marketplace_etag()

    return SystemFlagResponse(
        marketplace_id=marketplace_id,
        plugin_name=plugin_name,
        is_system=False,
    )
