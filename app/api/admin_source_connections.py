"""Admin REST API for source connections (named multi-project data-source connections).

Surface (all gated by ``Depends(require_admin)``):

  GET    /api/admin/source-connections              — list (?source_type=)
  POST   /api/admin/source-connections              — create; 409 on duplicate name
  GET    /api/admin/source-connections/{id}         — detail; 404 if missing
  PUT    /api/admin/source-connections/{id}         — update config / token_env; 404 if missing
  DELETE /api/admin/source-connections/{id}         — delete; 404 if missing
  PUT    /api/admin/source-connections/{id}/secret  — store vault secret; 409 if AGNES_VAULT_KEY missing
  DELETE /api/admin/source-connections/{id}/secret  — clear vault secret
  POST   /api/admin/source-connections/{id}/test    — verify connectivity; timeout 10s
  GET    /api/admin/source-connections/{id}/tables  — list buckets/tables for the "add data
                                                       source" wizard; keboola only, REST-only
                                                       admin-UI helper (see _EXEMPT classification
                                                       in tests/test_documentation_api_triple_surface.py)
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.auth.access import require_admin
from app.secrets_vault import VaultKeyNotConfiguredError
from connectors.keboola.storage_api import KeboolaStorageClient, StorageApiError
from src.repositories import (
    connection_secrets_repo,
    source_connections_repo,
    table_registry_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/source-connections", tags=["admin"])


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------


class CreateConnectionBody(BaseModel):
    name: str
    source_type: str
    config: Dict[str, Any]
    token_env: Optional[str] = None
    is_default: bool = False


class UpdateConnectionBody(BaseModel):
    # `name` supports the "Add data source" wizard's rename-after-test step
    # (#755): the project name is only known once `POST .../test` succeeds,
    # which requires the row to already exist.
    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    token_env: Optional[str] = None
    is_default: Optional[bool] = None


class SecretBody(BaseModel):
    value: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _with_secret_status(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Annotate a connection row with ``has_secret`` (a vault secret is stored).

    The token's storage location isn't derivable from ``token_env`` alone —
    vault secrets live in the separate ``connection_secrets`` store. The UI
    badge needs this to distinguish "vault" from "env"/"unset".
    """
    if row is None:
        return None
    try:
        row["has_secret"] = bool(connection_secrets_repo().has(row["id"]))
    except Exception:
        row["has_secret"] = False
    return row


def _resolve_token(connection_id: str, row: Dict[str, Any]) -> Optional[str]:
    """Resolve the storage token for a connection: vault secret first, then
    the ``token_env`` environment-variable fallback. Shared by ``/test`` and
    ``/tables`` so both endpoints treat "how do I get the token" identically.
    """
    token: Optional[str] = None
    try:
        secrets = connection_secrets_repo()
        if secrets.has(connection_id):
            token = secrets.get(connection_id)
    except Exception as exc:
        logger.debug("vault lookup failed for %s: %s", connection_id, exc)

    if not token:
        token_env = row.get("token_env") or ""
        if token_env:
            # SECURITY: only read env vars on the remote-attach allowlist. Without
            # this, an admin could set token_env=JWT_SECRET_KEY (or DATABASE_URL,
            # ANTHROPIC_API_KEY, …) and exfiltrate that server-process secret via
            # the outbound X-StorageApi-Token header in /test and /tables. Enforced
            # here (validate-at-use) as well as at create/update, so a row written
            # before this guard existed still cannot leak an off-allowlist env var.
            from src.orchestrator_security import is_token_env_allowed

            if is_token_env_allowed(token_env):
                token = os.environ.get(token_env, "")
            else:
                logger.warning(
                    "connection %s: token_env %r is not on the remote-attach "
                    "allowlist; refusing to read it (add it to "
                    "AGNES_REMOTE_ATTACH_TOKEN_ENVS or use a vault secret)",
                    connection_id,
                    token_env,
                )
    return token or None


def _reject_disallowed_token_env(token_env: Optional[str]) -> None:
    """Reject a token_env that isn't on the remote-attach allowlist (409-style
    400). None/empty is allowed — vault-secret connections don't use token_env.
    Called on create/update so a bad name never lands in the row."""
    if not token_env:
        return
    from src.orchestrator_security import is_token_env_allowed

    if not is_token_env_allowed(token_env):
        raise HTTPException(
            status_code=400,
            detail=(
                f"token_env {token_env!r} is not allowlisted. Use a Keboola storage-"
                "token env var (or add the name to AGNES_REMOTE_ATTACH_TOKEN_ENVS), "
                "or store the token in the vault via PUT .../secret instead."
            ),
        )


def _validate_stack_url(config: Optional[Dict[str, Any]], *, required: bool) -> None:
    """SSRF guard for a connection's stack_url. Rejects non-https and
    private/reserved/link-local hosts (e.g. the cloud metadata endpoint).

    ``required=False`` (create/update): validate only if a stack_url is present,
    so partial configs from the "add data source" wizard still save.
    ``required=True`` (test/tables): a stack_url must be present AND is
    re-validated immediately before the outbound request — validate-at-use
    closes the DNS-rebind window between store-time and fetch-time.
    """
    stack_url = ((config or {}).get("stack_url") or "").rstrip("/")
    if not stack_url:
        if required:
            raise HTTPException(status_code=400, detail="no stack_url in connection config")
        return
    if not stack_url.lower().startswith("https://"):
        raise HTTPException(status_code=400, detail="stack_url must be an https:// URL")
    # Reuse the shared SSRF validator (honors the operator SSRF-allowed-hosts
    # opt-out) rather than duplicating the private-range checks.
    from app.api.admin import _validate_url_not_private

    _validate_url_not_private(stack_url, "stack_url")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_connections(
    source_type: Optional[str] = None,
    _user: dict = Depends(require_admin),
):
    """List all named source connections, optionally filtered by source_type."""
    return [_with_secret_status(r) for r in source_connections_repo().list(source_type=source_type)]


@router.post("", status_code=201)
async def create_connection(
    body: CreateConnectionBody,
    _user: dict = Depends(require_admin),
):
    """Create a named source connection. 409 if the name is already taken."""
    repo = source_connections_repo()
    if repo.get_by_name(body.name) is not None:
        raise HTTPException(status_code=409, detail="connection_name_exists")
    _reject_disallowed_token_env(body.token_env)
    conn_id = str(uuid4())
    repo.create(
        id=conn_id,
        name=body.name,
        source_type=body.source_type,
        config=body.config,
        token_env=body.token_env,
        is_default=body.is_default,
        created_by=_user.get("id"),
    )
    return _with_secret_status(repo.get(conn_id))


@router.get("/{connection_id}")
async def get_connection(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Return a single source connection. 404 if not found."""
    row = source_connections_repo().get(connection_id)
    if row is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    return _with_secret_status(row)


@router.put("/{connection_id}")
async def update_connection(
    connection_id: str,
    body: UpdateConnectionBody,
    _user: dict = Depends(require_admin),
):
    """Update name/config/token_env/is_default of an existing connection.

    404 if missing; 409 if renaming to a name already taken by a different
    connection.
    """
    repo = source_connections_repo()
    if repo.get(connection_id) is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    if body.name is not None:
        existing = repo.get_by_name(body.name)
        if existing is not None and existing["id"] != connection_id:
            raise HTTPException(status_code=409, detail="connection_name_exists")
    _reject_disallowed_token_env(body.token_env)
    repo.update(
        connection_id,
        name=body.name,
        config=body.config,
        token_env=body.token_env,
        is_default=body.is_default,
    )
    return _with_secret_status(repo.get(connection_id))


@router.delete("/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Delete a source connection. 404 if not found; 409 if tables still reference it."""
    repo = source_connections_repo()
    if repo.get(connection_id) is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    # Refuse to orphan tables: a registry row pinned to this connection would
    # start failing its sync with "connection_not_found" once the row is gone.
    referencing = [t["id"] for t in table_registry_repo().list_all() if t.get("connection_id") == connection_id]
    if referencing:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "connection_in_use",
                "message": "Repoint or unregister these tables before deleting the connection.",
                "tables": referencing,
            },
        )
    repo.delete(connection_id)
    # Best-effort: clear any vault secret — ignore if none exists.
    try:
        connection_secrets_repo().delete(connection_id)
    except Exception:
        logger.debug("no vault secret for connection %s (expected)", connection_id)


@router.put("/{connection_id}/secret", status_code=204)
async def set_connection_secret(
    connection_id: str,
    body: SecretBody,
    _user: dict = Depends(require_admin),
):
    """Store (or rotate) the vault secret for a connection token.

    409 if AGNES_VAULT_KEY is not configured on the server.
    """
    if source_connections_repo().get(connection_id) is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    if not body.value:
        raise HTTPException(status_code=400, detail="secret value required")
    try:
        connection_secrets_repo().upsert(connection_id, body.value)
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc


@router.delete("/{connection_id}/secret", status_code=204)
async def delete_connection_secret(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Clear the vault secret for a connection (idempotent)."""
    if source_connections_repo().get(connection_id) is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    connection_secrets_repo().delete(connection_id)


@router.post("/{connection_id}/test")
async def test_connection(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Verify connectivity for the connection.

    Resolves the stack URL and token from the connection row (token_env →
    environment lookup, or vault secret), then calls
    ``GET {stack_url}/v2/storage?exclude=components`` with a 10-second
    timeout.

    Returns ``{ok: true, project_name: "…"}`` on success or
    ``{ok: false, error: "…"}`` on failure.
    """
    row = source_connections_repo().get(connection_id)
    if row is None:
        raise HTTPException(status_code=404, detail="connection_not_found")

    config = row.get("config") or {}
    try:
        # Re-validate immediately before the outbound call (validate-at-use)
        # so a stored-but-now-rebound host is caught, not just at store time.
        _validate_stack_url(config, required=True)
    except HTTPException as exc:
        return {"ok": False, "error": exc.detail if isinstance(exc.detail, str) else "invalid stack_url"}
    stack_url = (config.get("stack_url") or "").rstrip("/")

    token = _resolve_token(connection_id, row)
    if not token:
        return {"ok": False, "error": "no token available (vault empty, token_env unset)"}

    url = f"{stack_url}/v2/storage?exclude=components"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={"X-StorageApi-Token": token})
        if resp.status_code == 200:
            data = resp.json()
            project_name = data.get("owner", {}).get("name") or data.get("name") or ""
            return {"ok": True, "project_name": project_name}
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


@router.get("/{connection_id}/tables")
async def list_connection_tables(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """List Keboola buckets + tables reachable via this connection's token.

    Powers the admin "Add data source" wizard's table picker (#755): after a
    connection tests OK, the UI calls this to render a bucket-grouped
    checkbox list, then registers the selected tables one-by-one via
    ``POST /api/admin/register-table`` with this connection's ``id``.

    REST-only — admin-UI helper with no analyst-facing CLI/MCP analogue (see
    ``_EXEMPT`` in ``tests/test_documentation_api_triple_surface.py``).

    404 if the connection doesn't exist. 400 if the connection isn't
    ``source_type='keboola'`` (the only source type supported today), if no
    ``stack_url`` is configured, or if no token is resolvable (vault empty,
    ``token_env`` unset). 502 if the upstream Storage API call fails.

    Returns ``{"buckets": [{"id", "name", "stage", "description", "tables": [
    {"id", "name", "rows", "size_bytes"}, ...]}, ...]}``.
    """
    row = source_connections_repo().get(connection_id)
    if row is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    if row.get("source_type") != "keboola":
        raise HTTPException(status_code=400, detail="tables_listing_only_supported_for_keboola")

    config = row.get("config") or {}
    # Validate-at-use SSRF guard (also defeats DNS rebinding since store time).
    _validate_stack_url(config, required=True)
    stack_url = (config.get("stack_url") or "").rstrip("/")

    token = _resolve_token(connection_id, row)
    if not token:
        raise HTTPException(
            status_code=400,
            detail="no token available (vault empty, token_env unset)",
        )

    client = KeboolaStorageClient(url=stack_url, token=token)
    try:
        buckets = await run_in_threadpool(client.list_buckets)
        tables = await run_in_threadpool(client.list_tables)
    except StorageApiError as exc:
        raise HTTPException(status_code=502, detail=f"keboola_storage_api_error: {exc}") from exc

    tables_by_bucket: Dict[str, List[Dict[str, Any]]] = {}
    for t in tables:
        bucket_id = (t.get("bucket") or {}).get("id", "")
        tables_by_bucket.setdefault(bucket_id, []).append(
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "rows": t.get("rowsCount"),
                "size_bytes": t.get("dataSizeBytes"),
            }
        )

    result = []
    for b in buckets:
        bucket_id = b.get("id")
        result.append(
            {
                "id": bucket_id,
                "name": b.get("name"),
                "stage": b.get("stage"),
                "description": b.get("description"),
                "tables": tables_by_bucket.pop(bucket_id, []),
            }
        )
    # Defensive: a table whose bucket wasn't in the buckets listing (stale
    # permissions edge case) still surfaces, grouped under its bucket id.
    for bucket_id, tbls in tables_by_bucket.items():
        result.append({"id": bucket_id, "name": bucket_id, "stage": None, "description": None, "tables": tbls})

    return {"buckets": result}
