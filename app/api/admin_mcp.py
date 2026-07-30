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
from pydantic import BaseModel, field_validator, model_validator

from app.auth.access import require_admin
from app.auth.dependencies import _get_db
from src.identifier_validation import is_safe_identifier

from app.secrets_vault import (
    VaultKeyNotConfiguredError,
)
from connectors.mcp import classifier as mcp_classifier
from connectors.mcp import extractor as mcp_extractor
from connectors.mcp.client import exc_summary as _exc_summary
from src.repositories import (
    audit_repo,
    mcp_sources_repo,
    per_user_secrets_repo,
    shared_secrets_repo,
    tool_registry_repo,
)
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


def _validate_oauth_scope_coupling(auth_method: Optional[str], transport: Optional[str], scope: Optional[str]) -> None:
    """``auth_method='oauth'`` is only valid with a network transport AND
    ``scope='per_user'`` (2026-07-30 outbound MCP OAuth spec §1) — the same
    rule ``MCPSourceRepository.upsert()`` enforces at the data layer.
    Mirrored here so a bad combo 400s with a clear message before ever
    reaching the repo (and again, defense-in-depth, when it does)."""
    if (auth_method or "").strip().lower() == "oauth" and (transport not in ("http", "sse") or scope != "per_user"):
        raise ValueError(
            "auth_method='oauth' requires transport in ('http', 'sse') and scope='per_user' "
            f"(got transport={transport!r}, scope={scope!r})"
        )


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
    connect_hint: Optional[str] = None

    @field_validator("transport")
    @classmethod
    def _check_transport(cls, v: str) -> str:
        return _validate_transport(v)

    @field_validator("scope")
    @classmethod
    def _check_scope(cls, v: Optional[str]) -> Optional[str]:
        return _validate_scope(v) if v is not None else None

    @model_validator(mode="after")
    def _check_oauth_scope_coupling(self) -> "CreateMCPSourceRequest":
        _validate_oauth_scope_coupling(self.auth_method, self.transport, self.scope or "shared")
        return self


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
    connect_hint: Optional[str] = None

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
    # Linked data apps: ONLY when the caller explicitly designates the
    # targeted tool as the data-app lister (the /admin/linked-apps wizard
    # does) is its output table projected into `data_apps`. Without this,
    # every targeted run — e.g. the per-tool "Materialize now" button on the
    # source detail page — would be treated as the lister and could fabricate
    # linked rows from an unrelated table (Devin Review on #1116).
    lister: bool = False


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
# Request / response models — outbound OAuth client (2026-07-30 spec §2)
# ---------------------------------------------------------------------------


class OAuthRegisterRequest(BaseModel):
    """Body for ``POST …/oauth/register`` — RFC 7591 dynamic registration.

    Everything else (issuer, endpoints, PKCE support) is discovered live
    from the source's own ``url``; ``scopes`` is the one admin override the
    spec calls out (default empty — AS/resource defaults apply)."""

    scopes: Optional[str] = None


class OAuthClientConfigRequest(BaseModel):
    """Body for ``PUT …/oauth/client`` — the manual escape hatch for an AS
    without dynamic client registration.

    ``issuer`` is optional: when omitted it defaults to the
    ``authorization_endpoint``'s origin — the schema's ``issuer`` column is
    NOT NULL but the spec's manual-config shape doesn't call out a separate
    issuer field, so this keeps the escape hatch a 4-field form in the
    common case (same-origin AS) while still accepting an explicit value
    when the issuer differs from the endpoint host."""

    client_id: str
    client_secret: Optional[str] = None
    authorization_endpoint: str
    token_endpoint: str
    issuer: Optional[str] = None
    scopes: Optional[str] = None


def _serialize_oauth_client(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project an ``mcp_source_oauth_clients`` row to the API shape.

    Write-only fields (``client_secret``, ``registration_access_token``) are
    NEVER echoed back — same contract as the vault secret endpoints
    (``has_vault_secret`` flag, never the value). ``has_client_secret``
    mirrors that pattern here.
    """
    return {
        "source_id": row.get("source_id"),
        "issuer": row.get("issuer"),
        "client_id": row.get("client_id"),
        "has_client_secret": bool(row.get("client_secret")),
        "authorization_endpoint": row.get("authorization_endpoint"),
        "token_endpoint": row.get("token_endpoint"),
        "scopes": row.get("scopes"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


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
        audit_repo().log(
            user_id=actor_id,
            action=action,
            resource=resource,
            params=params,
            params_before=params_before,
        )
    except Exception:
        logger.warning("audit log failed for %s/%s", action, resource)


def _serialize_source(row: Dict[str, Any], *, has_vault_secret: bool = False) -> Dict[str, Any]:
    """Project a ``mcp_sources`` row to the API shape (timestamps as ISO).

    ``has_vault_secret`` is a write-only-secret status flag — True iff a
    vault-stored secret exists for this source (the value is never read
    back into the API).
    """
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
        "connect_hint": row.get("connect_hint"),
        "has_vault_secret": has_vault_secret,
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


def _merge_source_patch(existing: Dict[str, Any], patch: UpdateMCPSourceRequest) -> Dict[str, Any]:
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
        "auth_secret_env": data.get("auth_secret_env", existing.get("auth_secret_env")),
        "enabled": data.get(
            "enabled",
            bool(existing.get("enabled")) if existing.get("enabled") is not None else True,
        ),
        "scope": data.get("scope", existing.get("scope") or "shared"),
        "connect_hint": data.get("connect_hint", existing.get("connect_hint")),
    }


def _merge_tool_patch(existing: Dict[str, Any], patch: UpdateToolRequest) -> Dict[str, Any]:
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


def _probe_caller_user_id(src: Dict[str, Any], user: dict) -> Optional[str]:
    """Caller identity for the admin connect probes (introspect/classify/test).

    A ``per_user``-scoped source is probed under the calling admin's own
    connected secret when they have one; otherwise (and always for
    ``shared`` scope) the probe stays on the caller-less shared-vault path,
    preserving the pre-existing fallback behavior. The client's fail-closed
    rule (an identified caller never borrows the shared credential) is why
    this pre-check lives here rather than passing ``user["id"]`` blindly.

    ``auth_method='oauth'`` sources have no ``mcp_user_secrets`` row at all
    — the probe instead checks ``mcp_user_oauth_tokens`` for the calling
    admin's own OAuth connection (2026-07-30 outbound MCP OAuth sources spec
    §5), so an admin who has connected their own account gets probed under
    it; otherwise the probe stays caller-less exactly like every other
    per_user source with no stored credential.
    """
    if (src.get("scope") or "shared") != "per_user":
        return None
    try:
        if per_user_secrets_repo().get(src["id"], user["id"]):
            return user["id"]
    except Exception:  # vault/db unavailable — keep the legacy path
        pass
    try:
        from src.repositories import mcp_user_oauth_tokens_repo

        if mcp_user_oauth_tokens_repo().has(src["id"], user["id"]):
            return user["id"]
    except Exception:  # vault/db unavailable — keep the legacy path
        pass
    return None


# ---------------------------------------------------------------------------
# Source CRUD
# ---------------------------------------------------------------------------


def _require_safe_source_name(name: str) -> None:
    """Reject names the orchestrator's extract scan would refuse to ATTACH.

    The extractor writes ``/data/extracts/<name>/`` and the orchestrator
    validates that directory with the STRICT identifier rule before
    attaching — a name that fails it (hyphen, leading digit, >64 chars)
    materializes "successfully" but silently never reaches the catalog,
    with only a server-log WARNING. Same validator, no second regex.
    """
    if not is_safe_identifier(name):
        raise HTTPException(
            status_code=400,
            detail=(
                "source name must be a safe SQL identifier — letters, "
                "digits and underscores, not starting with a digit, max "
                f"64 chars (the sync engine refuses to attach anything "
                f"else); got {name!r}"
            ),
        )


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
    _require_safe_source_name(name)
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
            connect_hint=payload.connect_hint,
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
    secrets = shared_secrets_repo()
    return [_serialize_source(r, has_vault_secret=secrets.has(r["id"])) for r in rows]


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
    out = _serialize_source(src, has_vault_secret=shared_secrets_repo().has(source_id))
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
    if payload.name is not None and not new_name:
        raise HTTPException(status_code=400, detail="name is required")
    if new_name and new_name != existing.get("name"):
        _require_safe_source_name(new_name)
        collision = repo.get_by_name(new_name)
        if collision and collision["id"] != source_id:
            raise HTTPException(status_code=409, detail="name_exists")

    merged = _merge_source_patch(existing, payload)
    if new_name is not None:
        # Store the STRIPPED name. _merge_source_patch passes the raw payload
        # value through, so a padded " valid_name" would validate above (on
        # the stripped form) yet be persisted with whitespace — which the
        # orchestrator's identifier check then rejects at attach time, the
        # exact silent failure this validation exists to prevent.
        merged["name"] = new_name
    before = {k: existing.get(k) for k in ("name", "transport", "command", "url", "enabled")}
    try:
        # A partial update can flip auth_method to 'oauth' (or transport/
        # scope away from what oauth requires) without ever mentioning the
        # other two fields — validate the MERGED combo explicitly rather
        # than relying solely on the repo raising mid-write.
        _validate_oauth_scope_coupling(merged.get("auth_method"), merged.get("transport"), merged.get("scope"))
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
    return (
        _serialize_source(fresh, has_vault_secret=shared_secrets_repo().has(source_id)) if fresh else {"id": source_id}
    )


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
    # Clean up vault secrets so a deleted source leaves no orphaned encrypted
    # blobs — the shared secret plus any per-user rows. (Devin Review on #530.)
    shared_secrets_repo().delete(source_id)
    # Per-user secrets were migrated to Postgres (#530), so they must be dropped
    # through the factory — a raw PerUserSecretsRepository(conn) deletes from the
    # always-DuckDB connection and leaves orphaned rows on a PG instance.
    pu_secrets = per_user_secrets_repo()
    for uid in pu_secrets.list_for_source(source_id):
        pu_secrets.delete(source_id, uid)
    # OAuth trio: every user's tokens, in-flight PKCE flows, and Agnes's own
    # client registration for this source — a deleted source must leave no
    # orphaned credential material (Devin Review on #1124).
    from src.repositories import (
        mcp_oauth_flows_repo,
        mcp_source_oauth_clients_repo,
        mcp_user_oauth_tokens_repo,
    )

    mcp_user_oauth_tokens_repo().delete_for_source(source_id)
    mcp_oauth_flows_repo().delete_for_source(source_id)
    mcp_source_oauth_clients_repo().delete(source_id)
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
        shared_secrets_repo().upsert(source_id, body.value)
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc
    _audit(
        conn,
        user["id"],
        "mcp_source.secret.set",
        f"mcp_source:{source_id}",
        {},
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
    shared_secrets_repo().delete(source_id)
    _audit(
        conn,
        user["id"],
        "mcp_source.secret.delete",
        f"mcp_source:{source_id}",
        {},
    )


# ---------------------------------------------------------------------------
# Outbound OAuth client registration (2026-07-30 spec §2)
# ---------------------------------------------------------------------------


def _url_origin(url: str) -> str:
    """``scheme://netloc`` of ``url`` — used as the default ``issuer`` for a
    manually-configured OAuth client when the admin omits it."""
    from urllib.parse import urlparse

    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


def _require_oauth_source(src: Dict[str, Any]) -> None:
    if (src.get("auth_method") or "").lower() != "oauth":
        raise HTTPException(
            status_code=400,
            detail="source is not configured with auth_method='oauth'",
        )


def _oauth_redirect_uri() -> str:
    """``{server_url}/api/mcp/oauth-client/callback`` — the outbound
    client's callback path, deliberately distinct from the inbound issuer's
    ``/api/mcp/oauth/*`` (spec §3 routing note)."""
    from app.instance_config import get_public_url

    base = get_public_url()
    if not base:
        raise HTTPException(
            status_code=409,
            detail="server.public_url is not configured — required to build the OAuth redirect_uri",
        )
    return f"{base}/api/mcp/oauth-client/callback"


@router.post("/mcp-sources/{source_id}/oauth/register")
async def register_oauth_client(
    source_id: str,
    payload: Optional[OAuthRegisterRequest] = None,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """RFC 9728 + RFC 8414 discovery, PKCE-S256 fail-closed check, RFC 7591
    dynamic client registration against the source's own authorization
    server. Idempotent — a second call replaces the row, best-effort
    revoking the OLD registration first (spec §2 step 5).
    """
    from connectors.mcp.oauth_client import (
        OAuthDiscoveryError,
        best_effort_revoke_registration,
        build_oauth_http_client,
        discover_as_metadata,
        discover_protected_resource_metadata,
        register_dynamic_client,
        require_https_endpoints,
        require_pkce_s256,
        resolve_issuer,
    )

    import httpx

    from src.net.ssrf_safe_client import SSRFRejected
    from src.repositories import mcp_source_oauth_clients_repo

    src_repo = mcp_sources_repo()
    src = src_repo.get(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    _require_oauth_source(src)
    if not src.get("url"):
        raise HTTPException(status_code=400, detail="source has no 'url' to discover OAuth metadata from")

    redirect_uri = _oauth_redirect_uri()
    scopes = payload.scopes if payload else None
    clients_repo = mcp_source_oauth_clients_repo()
    existing = clients_repo.get(source_id)

    try:
        async with build_oauth_http_client() as http_client:
            resource_meta = await discover_protected_resource_metadata(src["url"], client=http_client)
            issuer = resolve_issuer(resource_meta)
            as_metadata = await discover_as_metadata(issuer, client=http_client)
            require_https_endpoints(as_metadata)
            require_pkce_s256(as_metadata)

            if existing:
                await best_effort_revoke_registration(
                    registration_endpoint=as_metadata.get("registration_endpoint"),
                    client_id=existing["client_id"],
                    registration_access_token=existing.get("registration_access_token"),
                    client=http_client,
                )

            registered = await register_dynamic_client(
                as_metadata,
                redirect_uri=redirect_uri,
                scopes=scopes,
                client=http_client,
            )
    except OAuthDiscoveryError as exc:
        raise HTTPException(status_code=502, detail=f"oauth_discovery_failed: {_exc_summary(exc)}")
    except SSRFRejected as exc:
        # The discovery target (or an endpoint it advertised) resolved to a
        # blocked address — an admin-actionable configuration problem, not an
        # internal error (Devin Review on #1124).
        raise HTTPException(status_code=400, detail=f"oauth_endpoint_rejected: {_exc_summary(exc)}")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"oauth_discovery_failed: {_exc_summary(exc)}")

    try:
        clients_repo.upsert(
            source_id,
            issuer=registered.issuer,
            client_id=registered.client_id,
            authorization_endpoint=registered.authorization_endpoint,
            token_endpoint=registered.token_endpoint,
            client_secret=registered.client_secret,
            registration_access_token=registered.registration_access_token,
            scopes=registered.scopes,
        )
    except VaultKeyNotConfiguredError as exc:
        # The DCR registration already happened upstream; the next successful
        # register re-registers and best-effort revokes it. Same actionable
        # 409 the other credential endpoints give (Devin Review on #1124).
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc
    _audit(
        conn,
        user["id"],
        "mcp_oauth.client_register",
        f"mcp_source:{source_id}",
        {
            "method": "dcr",
            "issuer": registered.issuer,
            "client_id": registered.client_id,
            "re_registered": bool(existing),
        },
    )
    fresh = clients_repo.get(source_id)
    return _serialize_oauth_client(fresh) if fresh else {"source_id": source_id}


@router.put("/mcp-sources/{source_id}/oauth/client")
async def set_oauth_client_config(
    source_id: str,
    payload: OAuthClientConfigRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Manual OAuth client configuration — the escape hatch for an
    authorization server without dynamic client registration (spec §2).

    Same https + SSRF-safe validation as the discovered path, even though
    no outbound call happens here: these endpoint URLs get dialed for real
    at token exchange/refresh time, so a bad (internal/metadata) URL must
    be rejected at configuration time, not silently accepted and only
    caught when a real caller triggers a forward.
    """
    from src.net.ssrf_safe_client import resolve_safe
    from src.repositories import mcp_source_oauth_clients_repo

    src_repo = mcp_sources_repo()
    src = src_repo.get(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="mcp_source_not_found")
    _require_oauth_source(src)

    for field, url in (
        ("authorization_endpoint", payload.authorization_endpoint),
        ("token_endpoint", payload.token_endpoint),
    ):
        ok, reason, _ip = resolve_safe(url, https_only=True)
        if not ok:
            raise HTTPException(status_code=400, detail=f"{field} failed SSRF/https validation: {reason}")

    issuer = payload.issuer or _url_origin(payload.authorization_endpoint)

    clients_repo = mcp_source_oauth_clients_repo()
    try:
        clients_repo.upsert(
            source_id,
            issuer=issuer,
            client_id=payload.client_id,
            authorization_endpoint=payload.authorization_endpoint,
            token_endpoint=payload.token_endpoint,
            client_secret=payload.client_secret,
            scopes=payload.scopes,
        )
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc
    _audit(
        conn,
        user["id"],
        "mcp_oauth.client_register",
        f"mcp_source:{source_id}",
        {"method": "manual", "issuer": issuer, "client_id": payload.client_id},
    )
    fresh = clients_repo.get(source_id)
    return _serialize_oauth_client(fresh) if fresh else {"source_id": source_id}


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
        tools = await mcp_extractor.introspect_source_async(src, caller_user_id=_probe_caller_user_id(src, user))
    except Exception as exc:
        logger.exception("introspect failed for source %s", source_id)
        raise HTTPException(status_code=502, detail=f"introspect_failed: {_exc_summary(exc)}")
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

        tool_infos = await _list_tools_async(src, caller_user_id=_probe_caller_user_id(src, user))
    except Exception as exc:
        logger.exception("classify (list_tools) failed for source %s", source_id)
        raise HTTPException(status_code=502, detail=f"introspect_failed: {_exc_summary(exc)}")
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
        tools = await mcp_extractor.introspect_source_async(src, caller_user_id=_probe_caller_user_id(src, user))
        result = {"ok": True, "tool_count": len(tools), "error": None}
    except Exception as exc:
        summary = _exc_summary(exc)
        logger.warning("test connection failed for source %s: %s", source_id, summary)
        result = {"ok": False, "tool_count": 0, "error": summary}
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
        raise HTTPException(status_code=500, detail=f"materialize_failed: {exc}")
    # Linked data apps: project the lister's FRESHLY materialized table into
    # the data_apps registry as grantable `linked` rows. Two gates, both from
    # `result["tables"]` (what this run actually wrote) rather than mere table
    # presence in the persistent extract file — an `only_tool_id` run for an
    # unrelated tool would otherwise re-project an arbitrarily stale lister
    # table and re-activate rows a previous reconcile had hidden:
    #  * targeted run (`only_tool_id` — the wizard's path): the admin has
    #    explicitly designated THAT tool as the data-app lister, so project
    #    from the table it wrote under its own exposed_name (the extractor
    #    names tables by exposed_name, which is almost never the literal
    #    `keboola_data_apps` — Devin Review on #1116);
    #  * full-source run: no tool is designated, so keep the conservative
    #    documented contract — only a table literally named
    #    `keboola_data_apps` projects.
    # Best-effort — a projection failure must not fail the materialize call.
    try:
        from src.data_apps.keboola_adapter import MATERIALIZED_TABLE
        from src.data_apps.linked_projection import project_from_extract

        freshly_written = {t.get("table") for t in result.get("tables") or []}
        lister_table = None
        if only_tool_id and payload is not None and payload.lister:
            # Explicit designation only (the wizard's path) — see the
            # MaterializeRequest.lister comment. A targeted run WITHOUT the
            # flag (per-tool "Materialize now") never projects.
            tool = tool_registry_repo().get(only_tool_id)
            exposed = (tool or {}).get("exposed_name") or ""
            if exposed in freshly_written:
                lister_table = exposed
        elif not only_tool_id and MATERIALIZED_TABLE in freshly_written:
            lister_table = MATERIALIZED_TABLE
        proj = None
        if lister_table:
            proj = project_from_extract(source_id, result.get("extract_duckdb"), table_name=lister_table)
        if proj is not None:
            result["linked_projection"] = {
                "created": proj.created,
                "updated": proj.updated,
                "hidden": proj.hidden,
            }
    except Exception:
        logger.exception("linked-app projection failed for source %s", source_id)

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
    before = {k: existing.get(k) for k in ("source_id", "exposed_name", "mode", "schedule", "enabled")}
    try:
        repo.upsert(**merged)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except duckdb.ConstraintException as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    fresh = repo.get(tool_id)
    after = {k: (fresh or {}).get(k) for k in ("source_id", "exposed_name", "mode", "schedule", "enabled")}
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
    # Validate the group exists so we don't dangle FK-less rows. Backend-aware:
    # user_groups lives in the active backend (Postgres on a PG instance).
    from src.repositories import user_groups_repo

    if not user_groups_repo().get(group_id):
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
