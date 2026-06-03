"""Admin REST API for Universal MCP — sources + tool registry + grants (RFC #461 M5).

Surface (all gated by ``Depends(require_admin)``):

  - ``POST   /api/admin/mcp-sources``                                — create / register
  - ``GET    /api/admin/mcp-sources``                                — list (?enabled_only=)
  - ``GET    /api/admin/mcp-sources/{source_id}``                    — detail (includes tools)
  - ``PUT    /api/admin/mcp-sources/{source_id}``                    — patch (partial)
  - ``DELETE /api/admin/mcp-sources/{source_id}``                    — cascade tools + grants

  - ``POST   /api/admin/mcp-sources/{source_id}/introspect``         — discover tools live
  - ``POST   /api/admin/mcp-sources/{source_id}/classify``           — introspect + heuristic
  - ``POST   /api/admin/mcp-sources/{source_id}/test``               — connectivity check
  - ``POST   /api/admin/mcp-sources/{source_id}/materialize``        — run extractor

  - ``POST   /api/admin/mcp-tools``                                  — register tool row
  - ``GET    /api/admin/mcp-tools``                                  — list (?source_id=)
  - ``GET    /api/admin/mcp-tools/{tool_id}``                        — detail
  - ``PUT    /api/admin/mcp-tools/{tool_id}``                        — patch (partial)
  - ``DELETE /api/admin/mcp-tools/{tool_id}``                        — drop + grants
  - ``POST   /api/admin/mcp-tools/{tool_id}/grants``                 — add group grant
  - ``DELETE /api/admin/mcp-tools/{tool_id}/grants/{group_id}``      — revoke

The ``mcp-tools`` prefix (rather than ``tools``) avoids collision with any
future generic-tool admin surface. Every mutation writes an ``audit_log``
row mirroring the ``data_packages`` admin router pattern.

POC scope: no vault, no policy engine, no PII redaction. Plain CRUD plus
the four connector helpers (introspect/classify/test/materialize).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from app.secrets_vault import SharedSecretsRepository, VaultKeyNotConfiguredError
from connectors.mcp import classifier as mcp_classifier
from connectors.mcp import extractor as mcp_extractor
from src.repositories import mcp_sources_repo, tool_registry_repo
from src.repositories.audit import AuditRepository
from src.repositories.mcp_sources import MCPSourceRepository  # noqa: F401  # kept for type-only imports + tests that monkeypatch the symbol
from src.repositories.tool_registry import (
    MATERIALIZE,
    PASSTHROUGH,
    ToolRegistryRepository,  # noqa: F401  # kept for type-only imports + tests that monkeypatch the symbol
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin-mcp"])


# ---------------------------------------------------------------------------
# Constants + small validators
# ---------------------------------------------------------------------------

_VALID_TRANSPORTS = ("stdio", "http", "sse")
_VALID_MODES = (MATERIALIZE, PASSTHROUGH)


def _validate_transport(v: str) -> str:
    v = (v or "").strip().lower()
    if v not in _VALID_TRANSPORTS:
        raise ValueError(f"transport must be one of {list(_VALID_TRANSPORTS)}")
    return v


def _validate_mode(v: str) -> str:
    v = (v or "").strip().lower()
    if v not in _VALID_MODES:
        raise ValueError(f"mode must be one of {list(_VALID_MODES)}")
    return v


# ---------------------------------------------------------------------------
# Request / response models — sources
# ---------------------------------------------------------------------------


_VALID_SCOPES = ("shared", "per_user")


def _validate_scope(v: Optional[str]) -> str:
    if v is None:
        return "shared"
    v = (v or "").strip().lower()
    if v not in _VALID_SCOPES:
        raise ValueError(f"scope must be one of {list(_VALID_SCOPES)}")
    return v


class CreateMCPSourceRequest(BaseModel):
    name: str
    transport: str
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    url: Optional[str] = None
    auth_method: Optional[str] = None
    auth_secret_env: Optional[str] = None
    enabled: bool = True
    scope: Optional[str] = None  # 'shared' (default) | 'per_user'

    @field_validator("transport")
    @classmethod
    def _check_transport(cls, v: str) -> str:
        return _validate_transport(v)

    @field_validator("scope")
    @classmethod
    def _check_scope(cls, v: Optional[str]) -> Optional[str]:
        return _validate_scope(v) if v is not None else None


class UpdateMCPSourceRequest(BaseModel):
    """Partial update — all fields optional. Omitted = leave unchanged.

    Because the underlying repository uses ``INSERT … ON CONFLICT DO UPDATE``
    with all columns, we merge the patch against the existing row in the
    handler before calling ``upsert``.
    """

    name: Optional[str] = None
    transport: Optional[str] = None
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    url: Optional[str] = None
    auth_method: Optional[str] = None
    auth_secret_env: Optional[str] = None
    enabled: Optional[bool] = None
    scope: Optional[str] = None

    @field_validator("transport")
    @classmethod
    def _check_transport(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _validate_transport(v)

    @field_validator("scope")
    @classmethod
    def _check_scope(cls, v: Optional[str]) -> Optional[str]:
        return _validate_scope(v) if v is not None else None


class MaterializeRequest(BaseModel):
    tool_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Request / response models — tools
# ---------------------------------------------------------------------------


class CreateToolRequest(BaseModel):
    tool_id: Optional[str] = None  # auto-generated when omitted
    source_id: str
    original_name: str
    exposed_name: str
    mode: str
    table_id: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None
    description: Optional[str] = None
    mutating: bool = False
    pii_fields: Optional[List[str]] = None
    rate_limit_pm: Optional[int] = None
    schedule: Optional[str] = None
    enabled: bool = True

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        return _validate_mode(v)


class UpdateToolRequest(BaseModel):
    """Partial update — merge against existing row before re-upsert."""

    source_id: Optional[str] = None
    original_name: Optional[str] = None
    exposed_name: Optional[str] = None
    mode: Optional[str] = None
    table_id: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = None
    description: Optional[str] = None
    mutating: Optional[bool] = None
    pii_fields: Optional[List[str]] = None
    rate_limit_pm: Optional[int] = None
    schedule: Optional[str] = None
    enabled: Optional[bool] = None

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _validate_mode(v)


class AddGrantRequest(BaseModel):
    group_id: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    resource: str,
    params: Optional[Dict[str, Any]] = None,
    params_before: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort audit row. Mirrors ``app/api/data_packages._audit``."""
    try:
        AuditRepository(conn).log(
            user_id=actor_id,
            action=action,
            resource=resource,
            params=params,
            params_before=params_before,
        )
    except Exception:
        logger.warning("audit log failed for %s/%s", action, resource)


def _serialize_source(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project a ``mcp_sources`` row to the API shape (timestamps as ISO)."""
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "transport": row.get("transport"),
        "command": row.get("command"),
        "args": row.get("args") or [],
        "env": row.get("env") or {},
        "url": row.get("url"),
        "auth_method": row.get("auth_method"),
        "auth_secret_env": row.get("auth_secret_env"),
        "enabled": bool(row.get("enabled")) if row.get("enabled") is not None else True,
        "scope": row.get("scope") or "shared",
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def _serialize_tool(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project a ``tool_registry`` row to the API shape."""
    return {
        "tool_id": row.get("tool_id"),
        "source_id": row.get("source_id"),
        "original_name": row.get("original_name"),
        "exposed_name": row.get("exposed_name"),
        "mode": row.get("mode"),
        "table_id": row.get("table_id"),
        "input_schema": row.get("input_schema"),
        "description": row.get("description"),
        "mutating": bool(row.get("mutating")) if row.get("mutating") is not None else False,
        "pii_fields": row.get("pii_fields") or [],
        "rate_limit_pm": row.get("rate_limit_pm"),
        "schedule": row.get("schedule"),
        "enabled": bool(row.get("enabled")) if row.get("enabled") is not None else True,
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def _merge_source_patch(
    existing: Dict[str, Any], patch: UpdateMCPSourceRequest
) -> Dict[str, Any]:
    """Merge a partial source patch onto the existing row.

    Returns the kwargs dict to pass to ``MCPSourceRepository.upsert``.
    """
    data = patch.model_dump(exclude_unset=True)
    return {
        "id": existing["id"],
        "name": data.get("name", existing.get("name")),
        "transport": data.get("transport", existing.get("transport")),
        "command": data.get("command", existing.get("command")),
        "args": data.get("args", existing.get("args")),
        "env": data.get("env", existing.get("env")),
        "url": data.get("url", existing.get("url")),
        "auth_method": data.get("auth_method", existing.get("auth_method")),
        "auth_secret_env": data.get(
            "auth_secret_env", existing.get("auth_secret_env")
        ),
        "enabled": data.get(
            "enabled",
            bool(existing.get("enabled")) if existing.get("enabled") is not None else True,
        ),
        "scope": data.get("scope", existing.get("scope") or "shared"),
    }


def _merge_tool_patch(
    existing: Dict[str, Any], patch: UpdateToolRequest
) -> Dict[str, Any]:
    """Merge a partial tool patch onto the existing row → upsert kwargs."""
    data = patch.model_dump(exclude_unset=True)
    return {
        "tool_id": existing["tool_id"],
        "source_id": data.get("source_id", existing.get("source_id")),
        "original_name": data.get("original_name", existing.get("original_name")),
        "exposed_name": data.get("exposed_name", existing.get("exposed_name")),
        "mode": data.get("mode", existing.get("mode")),
        "table_id": data.get("table_id", existing.get("table_id")),
        "input_schema": data.get("input_schema", existing.get("input_schema")),
        "description": data.get("description", existing.get("description")),
        "mutating": data.get(
            "mutating",
            bool(existing.get("mutating")) if existing.get("mutating") is not None else False,
        ),
        "pii_fields": data.get("pii_fields", existing.get("pii_fields")),
        "rate_limit_pm": data.get("rate_limit_pm", existing.get("rate_limit_pm")),
        "schedule": data.get("schedule", existing.get("schedule")),
        "enabled": data.get(
            "enabled",
            bool(existing.get("enabled")) if existing.get("enabled") is not None else True,
        ),
    }


# ---------------------------------------------------------------------------
# Source CRUD
# ---------------------------------------------------------------------------


@router.post("/mcp-sources", status_code=201)
async def create_mcp_source(
    payload: CreateMCPSourceRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Register a new MCP source. Returns ``{"id": ...}``.

    ``name`` is unique (DB constraint); the repo's ``upsert`` keys on ``id``,
    so we generate one and translate UNIQUE-name collisions to 409.
    """
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    repo = mcp_sources_repo()
    if repo.get_by_name(name) is not None:
        raise HTTPException(status_code=409, detail="name_exists")
    source_id = str(uuid.uuid4())
    try:
        repo.upsert(
            id=source_id,
            name=name,
            transport=payload.transport,
            command=payload.command,
            args=payload.args,
            env=payload.env,
            url=payload.url,
            auth_method=payload.auth_method,
            auth_secret_env=payload.auth_secret_env,
            enabled=payload.enabled,
            scope=payload.scope or "shared",
        )
    except ValueError as exc:
        # transport/command/url validation errors from the repo
        raise HTTPException(status_code=400, detail=str(exc))
    except duckdb.ConstraintException:
        raise HTTPException(status_code=409, detail="name_exists")
    _audit(
        conn,
        user["id"],
        "mcp_source.create",
        f"mcp_source:{source_id}",
        {"name": name, "transport": payload.transport},
    )
    return {"id": source_id}


@router.get("/mcp-sources")
async def list_mcp_sources(
    enabled_only: bool = False,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    repo = mcp_sources_repo()
    rows = repo.list_all(enabled_only=enabled_only)
    return [_serialize_source(r) for r in rows]


@router.get("/mcp-sources/{source_id}")
async def get_mcp_source(
    source_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Detail view — includes the list of tools registered against this source."""
    repo = mcp_sources_repo()
    src = repo.get(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    tools_repo = tool_registry_repo()
    tools = tools_repo.list_for_source(source_id)
    out = _serialize_source(src)
    out["tools"] = [_serialize_tool(t) for t in tools]
    return out


@router.put("/mcp-sources/{source_id}")
async def update_mcp_source(
    source_id: str,
    payload: UpdateMCPSourceRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Partial update. Audit row carries before/after for the changed fields."""
    repo = mcp_sources_repo()
    existing = repo.get(source_id)
    if not existing:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")

    # If renaming, ensure no collision against a different source.
    new_name = (payload.name or "").strip() if payload.name is not None else None
    if new_name and new_name != existing.get("name"):
        collision = repo.get_by_name(new_name)
        if collision and collision["id"] != source_id:
            raise HTTPException(status_code=409, detail="name_exists")

    merged = _merge_source_patch(existing, payload)
    before = {k: existing.get(k) for k in ("name", "transport", "command", "url", "enabled")}
    try:
        repo.upsert(**merged)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except duckdb.ConstraintException:
        raise HTTPException(status_code=409, detail="name_exists")
    fresh = repo.get(source_id)
    after = {k: (fresh or {}).get(k) for k in ("name", "transport", "command", "url", "enabled")}
    _audit(
        conn,
        user["id"],
        "mcp_source.update",
        f"mcp_source:{source_id}",
        {"after": after},
        params_before={"before": before},
    )
    return _serialize_source(fresh) if fresh else {"id": source_id}


@router.delete("/mcp-sources/{source_id}", status_code=204)
async def delete_mcp_source(
    source_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Hard delete — cascades to ``tool_registry`` + ``tool_grants`` for this
    source via :py:meth:`ToolRegistryRepository.delete_for_source` (which
    deletes grants per tool before the registry row)."""
    src_repo = mcp_sources_repo()
    existing = src_repo.get(source_id)
    if not existing:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    tool_repo = tool_registry_repo()
    tool_count = len(tool_repo.list_for_source(source_id))
    tool_repo.delete_for_source(source_id)
    src_repo.delete(source_id)
    _audit(
        conn,
        user["id"],
        "mcp_source.delete",
        f"mcp_source:{source_id}",
        {"name": existing.get("name"), "tool_count": tool_count},
    )


# ---------------------------------------------------------------------------
# Source secret (server-wide vault) — RFC #461 §4
# ---------------------------------------------------------------------------


class SecretBody(BaseModel):
    value: str


@router.put("/mcp-sources/{source_id}/secret", status_code=204)
async def set_mcp_source_secret(
    source_id: str,
    body: SecretBody,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Store (or rotate) the server-wide vault secret for ``source_id``.

    The plaintext lives only in the request body — Fernet-encrypted at
    rest in ``mcp_secrets``. ``connectors/mcp/client._lookup_secret_for_source``
    pulls it on every call, falling back to the legacy
    ``auth_secret_env`` lookup if the vault has no row, so an operator
    can roll out the vault without a flag-day rewrite of source rows.
    """
    src_repo = mcp_sources_repo()
    if not src_repo.get(source_id):
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    if not body.value:
        raise HTTPException(status_code=400, detail="secret value required")
    try:
        SharedSecretsRepository(conn).upsert(source_id, body.value)
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc
    _audit(
        conn, user["id"], "mcp_source.secret.set",
        f"mcp_source:{source_id}", {},
    )


@router.delete("/mcp-sources/{source_id}/secret", status_code=204)
async def delete_mcp_source_secret(
    source_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Drop the vault row for ``source_id``. Source then falls back to
    its ``auth_secret_env`` env-var, or to anonymous if neither is set."""
    src_repo = mcp_sources_repo()
    if not src_repo.get(source_id):
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    SharedSecretsRepository(conn).delete(source_id)
    _audit(
        conn, user["id"], "mcp_source.secret.delete",
        f"mcp_source:{source_id}", {},
    )


# ---------------------------------------------------------------------------
# Source actions — introspect / classify / test / materialize
# ---------------------------------------------------------------------------


@router.post("/mcp-sources/{source_id}/introspect")
async def introspect_mcp_source(
    source_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Live-connect to the source and list its tools verbatim."""
    src_repo = mcp_sources_repo()
    src = src_repo.get(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    try:
        # introspect_source_async — async-safe; the sync variant calls
        # asyncio.run() which blows up inside FastAPI's running loop.
        tools = await mcp_extractor.introspect_source_async(src)
    except Exception as exc:
        logger.exception("introspect failed for source %s", source_id)
        raise HTTPException(
            status_code=502, detail=f"introspect_failed: {exc}"
        )
    _audit(
        conn,
        user["id"],
        "mcp_source.introspect",
        f"mcp_source:{source_id}",
        {"tool_count": len(tools)},
    )
    return {"tools": tools}


@router.post("/mcp-sources/{source_id}/classify")
async def classify_mcp_source(
    source_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Introspect + run heuristic classifier; return per-tool proposals."""
    src_repo = mcp_sources_repo()
    src = src_repo.get(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    try:
        from connectors.mcp.client import list_tools_async as _list_tools_async
        tool_infos = await _list_tools_async(src)
    except Exception as exc:
        logger.exception("classify (list_tools) failed for source %s", source_id)
        raise HTTPException(
            status_code=502, detail=f"introspect_failed: {exc}"
        )
    proposals = mcp_classifier.classify_all(tool_infos)
    _audit(
        conn,
        user["id"],
        "mcp_source.classify",
        f"mcp_source:{source_id}",
        {"tool_count": len(proposals)},
    )
    return {
        "proposals": [
            {
                "name": p.name,
                "suggested_mode": p.suggested_mode,
                "reason": p.reason,
                "description": p.description,
                "input_schema": p.input_schema,
            }
            for p in proposals
        ]
    }


@router.post("/mcp-sources/{source_id}/test")
async def test_mcp_source(
    source_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Lightweight connectivity probe. Returns ``{ok, tool_count, error}``;
    HTTP 200 even on connect failure so the UI can render the diagnostic."""
    src_repo = mcp_sources_repo()
    src = src_repo.get(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    try:
        tools = await mcp_extractor.introspect_source_async(src)
        result = {"ok": True, "tool_count": len(tools), "error": None}
    except Exception as exc:
        logger.warning("test connection failed for source %s: %s", source_id, exc)
        result = {"ok": False, "tool_count": 0, "error": str(exc)}
    _audit(
        conn,
        user["id"],
        "mcp_source.test",
        f"mcp_source:{source_id}",
        {"ok": result["ok"], "tool_count": result["tool_count"]},
    )
    return result


@router.post("/mcp-sources/{source_id}/materialize")
async def materialize_mcp_source(
    source_id: str,
    payload: Optional[MaterializeRequest] = None,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Run the extractor for this source (optionally restricted to one tool).

    Returns the extractor's summary dict (source_name, extract_duckdb path,
    tables, errors). Use the SyncOrchestrator's next rebuild to attach.
    """
    src_repo = mcp_sources_repo()
    src = src_repo.get(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    only_tool_id = payload.tool_id if payload else None
    try:
        # extract_source_async — async-safe; the sync variant wraps
        # ``_materialize_one_tool`` with asyncio.run() which blows up
        # inside FastAPI's running event loop (same root cause as the
        # introspect/classify/test handlers above).
        result = await mcp_extractor.extract_source_async(
            system_conn=conn,
            source_id=source_id,
            only_tool_id=only_tool_id,
        )
    except ValueError as exc:
        # source disabled / not found / no list-of-dicts in response, etc.
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("materialize failed for source %s", source_id)
        raise HTTPException(
            status_code=500, detail=f"materialize_failed: {exc}"
        )
    _audit(
        conn,
        user["id"],
        "mcp_source.materialize",
        f"mcp_source:{source_id}",
        {
            "only_tool_id": only_tool_id,
            "table_count": len(result.get("tables", [])),
            "error_count": len(result.get("errors", [])),
        },
    )
    return result


# ---------------------------------------------------------------------------
# Tool CRUD
# ---------------------------------------------------------------------------


@router.post("/mcp-tools", status_code=201)
async def create_mcp_tool(
    payload: CreateToolRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Register a tool row against an existing source.

    The repo enforces mode-specific rules (e.g. materialize requires
    ``schedule``); we surface those as 400s.
    """
    src_repo = mcp_sources_repo()
    if not src_repo.get(payload.source_id):
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    tool_id = payload.tool_id or str(uuid.uuid4())
    repo = tool_registry_repo()
    if repo.get(tool_id) is not None:
        raise HTTPException(status_code=409, detail="tool_id_exists")
    try:
        repo.upsert(
            tool_id=tool_id,
            source_id=payload.source_id,
            original_name=payload.original_name,
            exposed_name=payload.exposed_name,
            mode=payload.mode,
            table_id=payload.table_id,
            input_schema=payload.input_schema,
            description=payload.description,
            mutating=payload.mutating,
            pii_fields=payload.pii_fields,
            rate_limit_pm=payload.rate_limit_pm,
            schedule=payload.schedule,
            enabled=payload.enabled,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except duckdb.ConstraintException as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _audit(
        conn,
        user["id"],
        "mcp_tool.create",
        f"mcp_tool:{tool_id}",
        {
            "source_id": payload.source_id,
            "exposed_name": payload.exposed_name,
            "mode": payload.mode,
        },
    )
    return {"tool_id": tool_id}


@router.get("/mcp-tools")
async def list_mcp_tools(
    source_id: Optional[str] = None,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """List all tools, optionally restricted to one source."""
    repo = tool_registry_repo()
    rows = repo.list_for_source(source_id) if source_id else repo.list_all()
    return [_serialize_tool(r) for r in rows]


@router.get("/mcp-tools/{tool_id}")
async def get_mcp_tool(
    tool_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Detail view — includes the list of group_ids granted access."""
    repo = tool_registry_repo()
    row = repo.get(tool_id)
    if not row:
        raise HTTPException(status_code=404, detail="mcp_tool_not_found")
    out = _serialize_tool(row)
    out["grants"] = repo.grants_for_tool(tool_id)
    return out


@router.put("/mcp-tools/{tool_id}")
async def update_mcp_tool(
    tool_id: str,
    payload: UpdateToolRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Partial update. Audit row carries before/after for changed fields."""
    repo = tool_registry_repo()
    existing = repo.get(tool_id)
    if not existing:
        raise HTTPException(status_code=404, detail="mcp_tool_not_found")

    # If source_id is being changed, validate the new source exists.
    if payload.source_id and payload.source_id != existing.get("source_id"):
        if not mcp_sources_repo().get(payload.source_id):
            raise HTTPException(status_code=404, detail="mcp_source_not_found")

    merged = _merge_tool_patch(existing, payload)
    before = {
        k: existing.get(k)
        for k in ("source_id", "exposed_name", "mode", "schedule", "enabled")
    }
    try:
        repo.upsert(**merged)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except duckdb.ConstraintException as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    fresh = repo.get(tool_id)
    after = {
        k: (fresh or {}).get(k)
        for k in ("source_id", "exposed_name", "mode", "schedule", "enabled")
    }
    _audit(
        conn,
        user["id"],
        "mcp_tool.update",
        f"mcp_tool:{tool_id}",
        {"after": after},
        params_before={"before": before},
    )
    return _serialize_tool(fresh) if fresh else {"tool_id": tool_id}


@router.delete("/mcp-tools/{tool_id}", status_code=204)
async def delete_mcp_tool(
    tool_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Hard delete — cascades grants via the repo."""
    repo = tool_registry_repo()
    existing = repo.get(tool_id)
    if not existing:
        raise HTTPException(status_code=404, detail="mcp_tool_not_found")
    grant_count = len(repo.grants_for_tool(tool_id))
    repo.delete(tool_id)
    _audit(
        conn,
        user["id"],
        "mcp_tool.delete",
        f"mcp_tool:{tool_id}",
        {
            "source_id": existing.get("source_id"),
            "exposed_name": existing.get("exposed_name"),
            "grant_count": grant_count,
        },
    )


# ---------------------------------------------------------------------------
# Tool grants
# ---------------------------------------------------------------------------


@router.post("/mcp-tools/{tool_id}/grants")
async def add_mcp_tool_grant(
    tool_id: str,
    payload: AddGrantRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Grant a user group access to the tool. Idempotent (ON CONFLICT DO NOTHING)."""
    repo = tool_registry_repo()
    if not repo.get(tool_id):
        raise HTTPException(status_code=404, detail="mcp_tool_not_found")
    group_id = (payload.group_id or "").strip()
    if not group_id:
        raise HTTPException(status_code=400, detail="group_id is required")
    # Validate the group exists so we don't dangle FK-less rows.
    row = conn.execute(
        "SELECT id FROM user_groups WHERE id = ?", [group_id]
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="user_group_not_found")
    try:
        repo.add_grant(tool_id, group_id)
    except duckdb.ConstraintException as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    _audit(
        conn,
        user["id"],
        "mcp_tool.grant.add",
        f"mcp_tool:{tool_id}",
        {"group_id": group_id},
    )
    return {"granted": True, "tool_id": tool_id, "group_id": group_id}


@router.delete("/mcp-tools/{tool_id}/grants/{group_id}", status_code=204)
async def remove_mcp_tool_grant(
    tool_id: str,
    group_id: str,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Revoke a group grant. Idempotent (DELETE missing row is a no-op)."""
    repo = tool_registry_repo()
    if not repo.get(tool_id):
        raise HTTPException(status_code=404, detail="mcp_tool_not_found")
    repo.remove_grant(tool_id, group_id)
    _audit(
        conn,
        user["id"],
        "mcp_tool.grant.remove",
        f"mcp_tool:{tool_id}",
        {"group_id": group_id},
    )
