"""Admin endpoints for marketplace git repositories.

CRUD + on-demand "Sync now" mirroring the /api/users shape. Tokens supplied
through the admin UI are persisted to data/state/.env_overlay (same pattern
as /api/admin/configure for Keboola/BigQuery) — never stored in the DB.
"""

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from app.resource_types import ResourceType
from src.marketplace import (
    MarketplaceNotFound,
    delete_marketplace_dir,
    is_valid_slug,
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


class UpdateMarketplaceRequest(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    branch: Optional[str] = None
    description: Optional[str] = None
    # None = leave untouched; empty string = clear token; non-empty = rotate
    token: Optional[str] = None


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


# ---------------------------------------------------------------------------
# Token persistence — mirrors app/api/admin.py::configure_instance
# ---------------------------------------------------------------------------


def _token_env_name(slug: str) -> str:
    """Derive a conventional env-var name from a slug.

    "foundry-ai" -> "AGNES_MARKETPLACE_FOUNDRY_AI_TOKEN"
    """
    normalized = slug.upper().replace("-", "_")
    return f"AGNES_MARKETPLACE_{normalized}_TOKEN"


def _persist_token(env_name: str, value: str) -> None:
    """Write (or update) a single key in data/state/.env_overlay and os.environ."""
    data_dir = Path(os.environ.get("DATA_DIR", "./data"))
    overlay_path = data_dir / "state" / ".env_overlay"
    overlay_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, str] = {}
    if overlay_path.exists():
        for line in overlay_path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()

    if value:
        existing[env_name] = value
        os.environ[env_name] = value
    else:
        existing.pop(env_name, None)
        os.environ.pop(env_name, None)

    overlay_path.write_text(
        "\n".join(f"{k}={v}" for k, v in existing.items()) + ("\n" if existing else "")
    )
    try:
        overlay_path.chmod(0o600)
    except OSError:
        pass


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

    repo = MarketplaceRegistryRepository(conn)
    if repo.get(slug):
        raise HTTPException(status_code=409, detail=f"marketplace '{slug}' already exists")

    token_env: Optional[str] = None
    if payload.token:
        token_env = _token_env_name(slug)
        _persist_token(token_env, payload.token)

    repo.register(
        id=slug,
        name=payload.name.strip(),
        url=payload.url.strip(),
        branch=(payload.branch or "").strip() or None,
        token_env=token_env,
        description=payload.description,
        registered_by=user.get("email"),
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

    if payload.token is not None:
        # None = untouched; "" = clear token_env binding; non-empty = rotate.
        if payload.token == "":
            if updated["token_env"]:
                _persist_token(updated["token_env"], "")
            updated["token_env"] = None
            changed["token"] = "cleared"
        else:
            env_name = _token_env_name(marketplace_id)
            _persist_token(env_name, payload.token)
            updated["token_env"] = env_name
            changed["token"] = "rotated"

    repo.register(
        id=updated["id"],
        name=updated["name"],
        url=updated["url"],
        branch=updated["branch"],
        token_env=updated["token_env"],
        description=updated["description"],
        registered_by=updated["registered_by"],
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
        _persist_token(existing["token_env"], "")

    repo.unregister(marketplace_id)
    # Drop cached plugin rows and any resource grants that reference plugins
    # from this marketplace. resource_grants stores resource_id as
    # "<marketplace_slug>/<plugin_name>" — match the slash-prefix.
    try:
        conn.execute(
            "DELETE FROM marketplace_plugins WHERE marketplace_id = ?",
            [marketplace_id],
        )
        conn.execute(
            "DELETE FROM resource_grants "
            "WHERE resource_type = ? AND resource_id LIKE ? || '/%'",
            [ResourceType.MARKETPLACE_PLUGIN.value, marketplace_id],
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
