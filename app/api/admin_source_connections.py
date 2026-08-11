"""Admin REST API for source connections (named multi-project data-source connections).

Surface (all gated by ``Depends(require_admin)``):

  GET    /api/admin/source-connections              — list (?source_type=)
  POST   /api/admin/source-connections              — create; 409 on duplicate name
  GET    /api/admin/source-connections/{id}         — detail; 404 if missing
  PUT    /api/admin/source-connections/{id}         — update config / token_env; 404 if missing
  DELETE /api/admin/source-connections/{id}         — delete; 404 if missing
  PUT    /api/admin/source-connections/{id}/secret  — store vault secret (kind=storage|master
                                                       in body); 409 if AGNES_VAULT_KEY missing;
                                                       kind=master is keboola-only and validated
                                                       live via a verify_token preflight
  DELETE /api/admin/source-connections/{id}/secret  — clear vault secret (?kind=storage|master)
  POST   /api/admin/source-connections/{id}/test    — verify connectivity; timeout 10s
  GET    /api/admin/source-connections/{id}/tables  — list buckets/tables for the "add data
                                                       source" wizard; keboola only, REST-only
                                                       admin-UI helper (see _EXEMPT classification
                                                       in tests/test_documentation_api_triple_surface.py)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import requests
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from app.auth.access import require_admin
from app.secrets_vault import VaultKeyNotConfiguredError, can_store_secrets
from connectors.keboola.semantic_layer import MasterTokenRequiredError, require_master_token
from connectors.keboola.storage_api import KeboolaStorageClient, StorageApiError, is_upstream_client_error
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
    kind: str = "storage"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def master_secret_key(connection_id: str) -> str:
    """Vault key for a connection's Keboola master (owner) Storage API token —
    a separate slot from the plain storage token (see ``SecretBody.kind``).
    Shared with the semantic-layer sync (which requires a master token)."""
    return f"{connection_id}:master"


def _log_host(stack_url: str) -> str:
    """Hostname of a connection's stack_url for log lines (never the token)."""
    return urlsplit(stack_url).hostname or stack_url


def _with_secret_status(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Annotate a connection row with ``has_secret``/``has_master_secret``
    (whether a vault secret is stored under each slot).

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
    try:
        row["has_master_secret"] = bool(connection_secrets_repo().has(master_secret_key(row["id"])))
    except Exception:
        row["has_master_secret"] = False
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


def project_identity(payload: Optional[Dict[str, Any]]) -> tuple[Optional[Any], str]:
    """``(project_id, project_name)`` from a Storage API payload that carries
    an ``owner`` block — both ``GET /tokens/verify`` and ``GET /v2/storage``
    do, so one reader serves the token preflights and the /test probe.

    Returns ``(None, "")`` when the payload has no owner id: an identity we
    cannot read must never be persisted as a *known* identity, or the
    cross-token check below would compare against a hole and pass anything.
    """
    owner = (payload or {}).get("owner") or {}
    owner_id = owner.get("id")
    if owner_id is None:
        return None, ""
    return owner_id, owner.get("name") or ""


def _record_project_identity(connection_id: str, row: Dict[str, Any], payload: Dict[str, Any]) -> None:
    """Persist the upstream project's id + name onto the connection config.

    A connection is one Keboola project, but nothing used to record WHICH —
    so an instance with several projects showed several identical-looking
    connections and a "master token: SET" badge that said nothing about
    which project the token actually opened. Recording the identity at every
    point a token verifies is what makes :func:`_reject_project_mismatch`
    possible at all.
    """
    project_id, project_name = project_identity(payload)
    if project_id is None:
        return
    config = dict(row.get("config") or {})
    known_id = config.get("project_id")
    if known_id is not None and str(known_id) != str(project_id):
        # NEVER silently re-bind. Callers are expected to detect the
        # disagreement and report it, but this is the backstop: a caller that
        # forgets would rewrite the binding under a stored master token that
        # still matches the ORIGINAL project, and that token then starts
        # failing a mismatch check nobody triggered (Devin Review on #1242).
        logger.warning(
            "connection %s is bound to Keboola project %s but a token for project %s verified; "
            "leaving the binding alone",
            connection_id,
            known_id,
            project_id,
        )
        return
    if known_id == project_id and config.get("project_name") == project_name:
        return
    config["project_id"] = project_id
    config["project_name"] = project_name
    source_connections_repo().update(connection_id, config=config)


def project_mismatch_message(row: Dict[str, Any], payload: Dict[str, Any], *, what: str) -> Optional[str]:
    """Why this token disagrees with the connection's recorded project, or
    ``None`` when there is no disagreement.

    The failure this closes: a master token pasted onto the wrong connection
    was stored happily and badged "SET", and the semantic layer then synced
    a *different* project's metrics under this connection's name — with no
    surface anywhere showing the two tokens disagreed. Silent, and only
    visible as metrics that make no sense for the project you thought you
    were looking at.

    Returns ``None`` when the connection has no recorded identity yet
    (nothing to contradict — the caller records it instead).

    Separate from the raising wrapper because ``/test`` reports failures as
    ``{"ok": false, "error": …}`` rather than an HTTP error, and it must be
    able to say the same thing in its own shape.
    """
    config = row.get("config") or {}
    known_id = config.get("project_id")
    if known_id is None:
        return None
    token_id, token_name = project_identity(payload)
    # Compared as strings: the id round-trips through a JSON config column on
    # two different backends (DuckDB JSON, PG JSONB), and a 5947 that comes
    # back as "5947" would otherwise read as a permanent, unfixable mismatch
    # on a correctly-configured connection. Devin Review on #1242 raised the
    # same risk across endpoints.
    if token_id is None or str(token_id) == str(known_id):
        return None
    known_name = config.get("project_name") or "unnamed"
    return (
        f"project_mismatch: this {what} belongs to Keboola project "
        f"{token_id} ({token_name or 'unnamed'}), but the connection is bound to project "
        f"{known_id} ({known_name}). Use a token from that project, or create a separate "
        f"connection for project {token_id}."
    )


def _reject_project_mismatch(row: Dict[str, Any], payload: Dict[str, Any], *, what: str) -> None:
    """400 if this token opens a different Keboola project than the one the
    connection is already bound to. See :func:`project_mismatch_message`."""
    message = project_mismatch_message(row, payload, what=what)
    if message is not None:
        raise HTTPException(status_code=400, detail=message)


class _VerifiedTokenInfo:
    """Adapts an already-fetched ``verify_token()`` response so it can be
    passed to ``connectors.keboola.semantic_layer.require_master_token``
    (which expects an object exposing ``verify_token() -> dict``) without a
    second Storage API round-trip just to reuse its exact error message."""

    def __init__(self, info: Dict[str, Any]) -> None:
        self._info = info

    def verify_token(self) -> Dict[str, Any]:
        return self._info


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

    Moving the connection to a different ``stack_url`` drops any recorded
    project identity: a project id is only meaningful on the stack it came
    from, and keeping the old binding would make every token from the new
    stack fail ``project_mismatch`` with no way to clear it from the UI. This
    is also the supported way to re-point a bound connection — see the note
    on :func:`project_mismatch_message`.
    """
    repo = source_connections_repo()
    existing_row = repo.get(connection_id)
    if existing_row is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    if body.name is not None:
        existing = repo.get_by_name(body.name)
        if existing is not None and existing["id"] != connection_id:
            raise HTTPException(status_code=409, detail="connection_name_exists")
    _reject_disallowed_token_env(body.token_env)
    config = body.config
    if config is not None:
        old_config = existing_row.get("config") or {}
        old_stack = (old_config.get("stack_url") or "").rstrip("/")
        new_stack = (config.get("stack_url") or "").rstrip("/")
        if old_stack and new_stack and old_stack != new_stack:
            config = {k: v for k, v in config.items() if k not in ("project_id", "project_name")}
            logger.info(
                "connection %s moved to a different stack; clearing its recorded project identity",
                connection_id,
            )
        else:
            # `config` REPLACES the stored dict, and the admin form posts only
            # the fields it renders — `project_id`/`project_name` are not among
            # them, because they are recorded by the connection itself rather
            # than typed. So every ordinary edit (a rename, a token_env change,
            # re-saving the same form) silently dropped the binding, and the
            # next token from any project was accepted again: the safeguard
            # this change set exists to add, removed by using the UI. Carried
            # forward unless the caller explicitly supplies the key — an
            # explicit `null` still clears it, which is how a mis-recorded
            # binding is reset without moving the stack. The branch above stays
            # the one deliberate clear: a project id means nothing on a
            # different stack. (Devin Review on this PR.)
            # Keboola only. `project_id`/`project_name` are *recorded* there —
            # written by the connection from its own token's owner block, never
            # typed — which is why an absent key must mean "unchanged". On a
            # BigQuery connection `project_id` is an ordinary configuration
            # field the admin DOES type, so carrying it forward would make it
            # unclearable: an admin who empties the field would see it come
            # straight back. (Devin Review on this PR.)
            if existing_row.get("source_type") == "keboola":
                config = {
                    **{k: v for k, v in old_config.items() if k in ("project_id", "project_name")},
                    **config,
                }
    repo.update(
        connection_id,
        name=body.name,
        config=config,
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
    try:
        connection_secrets_repo().delete(master_secret_key(connection_id))
    except Exception:
        logger.debug("no master vault secret for connection %s (expected)", connection_id)


@router.put("/{connection_id}/secret", status_code=204)
async def set_connection_secret(
    connection_id: str,
    body: SecretBody,
    _user: dict = Depends(require_admin),
):
    """Store (or rotate) the vault secret for a connection token.

    ``kind="storage"`` (default): the plain Storage API token used for pulls.
    For a Keboola connection it is preflighted like the master token, and the
    project it opens (``owner.id``/``owner.name``) is recorded on the
    connection — that identity is what later tokens are checked against.
    400 ``project_mismatch`` if the token opens a different project than the
    one this connection is already bound to; a connection is one project.
    ``kind="master"``: a *separate* slot for a Keboola master (owner) Storage
    API token, required by the semantic-layer sync (Metastore API rejects
    non-master tokens). 400 if the connection isn't ``source_type="keboola"``,
    if the token fails a live ``verify_token`` preflight (not a master token),
    or if the Storage API refuses the token outright (4xx — an invalid or
    expired token is the admin's to fix, not a gateway failure). 502 only when
    the Storage API is unreachable or answers 5xx.

    409 if AGNES_VAULT_KEY is not configured on the server — checked FIRST, so
    an instance that cannot store secrets says so instead of spending an
    upstream round-trip and reporting a token problem it never had.
    """
    row = source_connections_repo().get(connection_id)
    if row is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    if not body.value:
        raise HTTPException(status_code=400, detail="secret value required")
    if body.kind not in ("storage", "master"):
        raise HTTPException(status_code=400, detail="invalid_kind")

    # Refuse a doomed write before asking Keboola anything. Both branches below
    # now preflight the token upstream, and a round-trip to validate a secret
    # this instance cannot store is both wasted and actively misleading — the
    # admin would get a token error for what is really an unconfigured vault.
    if not can_store_secrets():
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        )

    # Stays None for a non-Keboola connection (no Storage API to ask), which
    # is what the identity-recording step below keys off.
    info: Optional[Dict[str, Any]] = None

    if body.kind == "master":
        if row.get("source_type") != "keboola":
            raise HTTPException(status_code=400, detail="master_token_only_for_keboola")
        config = row.get("config") or {}
        # Validate-at-use SSRF guard, re-checked immediately before the
        # outbound preflight call (same rationale as /test and /tables).
        _validate_stack_url(config, required=True)
        stack_url = (config.get("stack_url") or "").rstrip("/")
        client = KeboolaStorageClient(url=stack_url, token=body.value)
        try:
            info = await run_in_threadpool(client.verify_token)
        except (StorageApiError, requests.RequestException) as exc:
            # A freshly typed token is in flight — never surface bare str(exc);
            # route through the client's own token-aware redaction. Only these
            # two named types map to an upstream error at all; an unrelated
            # programming error should surface as a 500, not be mistaken for
            # an upstream outage.
            #
            # A 4xx means the Storage API understood us and said no — the
            # pasted token is invalid, expired, or belongs to another stack.
            # That is the admin's to fix, so it must not come back as 502:
            # a Bad Gateway reads as "Agnes is broken" and sends people
            # hunting infrastructure instead of re-reading the error.
            redacted = client._redact(exc)
            logger.warning(
                "master-token preflight failed for connection %s (%s): %s",
                connection_id,
                _log_host(stack_url),
                redacted,
            )
            status = 400 if is_upstream_client_error(exc) else 502
            raise HTTPException(status_code=status, detail=f"storage_api_error: {redacted}") from exc
        if not info.get("isMasterToken"):
            # Reuse require_master_token's exact message rather than duplicating
            # it — it already fetched isMasterToken, so hand it the cached
            # response instead of a second Storage API round-trip.
            try:
                require_master_token(_VerifiedTokenInfo(info))
            except MasterTokenRequiredError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        # A master token is only "the" master token for THIS connection if it
        # opens the same project the connection is bound to. Without this, a
        # token pasted onto the wrong row stored fine, badged "SET", and the
        # semantic layer synced another project's metrics under this name.
        _reject_project_mismatch(row, info, what="master token")
        key = master_secret_key(connection_id)
    else:
        if row.get("source_type") == "keboola":
            config = row.get("config") or {}
            # Keboola storage tokens get the same preflight as master tokens.
            # It is what makes the project identity knowable at all — the
            # storage token is the one the wizard fills, so it is where a
            # connection's project comes from. The cost is real and worth
            # naming: a Storage API outage now blocks storing a storage token
            # instead of accepting one nobody has checked.
            #
            # No stack_url yet → nothing to preflight AGAINST, so the token is
            # stored unverified and unbound rather than refused. The wizard
            # creates the row before the config is complete (create validates
            # stack_url with required=False for exactly that reason), so
            # demanding one here would have broken storing a token on a
            # half-built connection — a regression this change introduced and
            # Devin Review on #1242 caught. Identity gets recorded later, at
            # /test or when a master token is stored.
            if (config.get("stack_url") or "").strip():
                _validate_stack_url(config, required=True)
                stack_url = (config.get("stack_url") or "").rstrip("/")
                client = KeboolaStorageClient(url=stack_url, token=body.value)
                try:
                    info = await run_in_threadpool(client.verify_token)
                except (StorageApiError, requests.RequestException) as exc:
                    redacted = client._redact(exc)
                    logger.warning(
                        "storage-token preflight failed for connection %s (%s): %s",
                        connection_id,
                        _log_host(stack_url),
                        redacted,
                    )
                    status = 400 if is_upstream_client_error(exc) else 502
                    raise HTTPException(status_code=status, detail=f"storage_api_error: {redacted}") from exc
                _reject_project_mismatch(row, info, what="storage token")
        key = connection_id

    try:
        connection_secrets_repo().upsert(key, body.value)
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc

    # Only after the secret is safely stored: a recorded identity whose token
    # failed to persist would bind the connection to a project it cannot open.
    if row.get("source_type") == "keboola" and info is not None:
        # Bookkeeping, same as on `/test`: the token is already safely
        # stored by this point, so a vault or DB fault here must not turn a
        # successful save into an error response. (Devin Review.)
        try:
            _record_project_identity(connection_id, row, info)
        except Exception:  # noqa: BLE001 — the secret itself landed
            logger.warning(
                "stored the token for connection %s but could not record its project identity",
                connection_id,
                exc_info=True,
            )


@router.delete("/{connection_id}/secret", status_code=204)
async def delete_connection_secret(
    connection_id: str,
    kind: str = "storage",
    _user: dict = Depends(require_admin),
):
    """Clear the vault secret for a connection (idempotent).

    ``kind`` is a query param: ``storage`` (default) or ``master``.
    """
    if source_connections_repo().get(connection_id) is None:
        raise HTTPException(status_code=404, detail="connection_not_found")
    if kind not in ("storage", "master"):
        raise HTTPException(status_code=400, detail="invalid_kind")
    key = master_secret_key(connection_id) if kind == "master" else connection_id
    connection_secrets_repo().delete(key)


@router.post("/{connection_id}/test")
async def test_connection(
    connection_id: str,
    _user: dict = Depends(require_admin),
):
    """Verify connectivity for the connection.

    Resolves the stack URL and token from the connection row (token_env →
    environment lookup, or vault secret), then calls
    ``GET {stack_url}/v2/storage/tokens/verify`` with a 10-second timeout.

    Returns ``{ok: true, project_name: "…"}`` on success or
    ``{ok: false, error: "…"}`` on failure.

    It used to probe ``/v2/storage?exclude=components``, which measured
    verified live (2026-08-10): that endpoint is the unauthenticated stack
    index — it answers **200 with no token at all** and carries no ``owner``
    block. So "Test" reported OK for any token, including a garbage one, and
    the ``project_name`` it returned was always the empty string. Verifying
    the token is the only probe that answers the question the button asks,
    and it is what makes the project identity below readable at all.
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
        err = exc.detail if isinstance(exc.detail, str) else "invalid stack_url"
        logger.info("connection test for %s: failed — %s", connection_id, err)
        return {"ok": False, "error": err}
    stack_url = (config.get("stack_url") or "").rstrip("/")

    token = _resolve_token(connection_id, row)
    if not token:
        logger.info(
            "connection test for %s (%s): failed — no token available",
            connection_id,
            _log_host(stack_url),
        )
        return {"ok": False, "error": "no token available (vault empty, token_env unset)"}

    url = f"{stack_url}/v2/storage/tokens/verify"
    # Outcome log lines carry the status/reason but never the response body —
    # a proxy-echoed token must not land in server logs (the body still goes
    # to the admin client, same as before).
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={"X-StorageApi-Token": token})
        if resp.status_code == 200:
            data = resp.json()
            project_name = (data.get("owner") or {}).get("name") or ""
            # This is the wizard's own last step, so verifying here is what
            # binds a connection to its project the moment it is created.
            #
            # A disagreement is a FAILED test, not a re-binding: the token this
            # probe resolves is not necessarily the one that established the
            # binding (`_resolve_token` falls back to `token_env`), so
            # overwriting here would quietly re-point the connection and leave
            # the stored master token failing a mismatch nobody caused
            # (Devin Review on #1242).
            if row.get("source_type") == "keboola":
                mismatch = project_mismatch_message(row, data, what="connection's token")
                if mismatch:
                    logger.warning(
                        "connection test for %s (%s): project mismatch",
                        connection_id,
                        _log_host(stack_url),
                    )
                    return {"ok": False, "error": mismatch}
                # Recording the identity is BOOKKEEPING — it must not be able
                # to turn a passing connectivity check into a failure report.
                # It sat inside the same try/except that catches the outbound
                # call's network errors, so a vault or DB fault surfaced to the
                # admin as "connection test failed" with a database message,
                # for a project that is in fact correctly configured.
                # (Devin Review on this PR.)
                try:
                    _record_project_identity(connection_id, row, data)
                except Exception:  # noqa: BLE001 — the check itself succeeded
                    logger.warning(
                        "connection test for %s (%s) passed but its project identity could not be recorded",
                        connection_id,
                        _log_host(stack_url),
                        exc_info=True,
                    )
            # project_name is response-body content — it goes to the caller
            # but deliberately NOT into the log line (see comment above).
            logger.info(
                "connection test for %s (%s): ok",
                connection_id,
                _log_host(stack_url),
            )
            return {"ok": True, "project_name": project_name}
        logger.info(
            "connection test for %s (%s): failed — HTTP %d",
            connection_id,
            _log_host(stack_url),
            resp.status_code,
        )
        return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as exc:
        # Scrub the resolved token before logging (replace, then cap, so a
        # token straddling the cap can't survive). The client-visible error
        # keeps its pre-existing shape — the caller is the admin who
        # configured this token, the aggregated server log is not.
        logger.info(
            "connection test for %s (%s): failed — %s",
            connection_id,
            _log_host(stack_url),
            str(exc).replace(token, "<redacted-storage-token>")[:300],
        )
        return {"ok": False, "error": str(exc)[:300]}


def _scoped_listing(
    client: KeboolaStorageClient, connection_id: str
) -> tuple[Optional[List[dict]], Optional[List[dict]]]:
    """Per-bucket listing driven by the token's own ``bucketPermissions``.

    Bucket-scoped (custom access) tokens can be refused the project-wide
    ``/buckets`` + ``/tables`` listings while remaining able to read the
    buckets they are scoped to. ``/tokens/verify`` works for every token and
    names those buckets, so enumerate exactly what the token can reach.

    Returns ``(buckets, tables)`` in the same shapes the project-wide
    listings produce (per-bucket table rows are stamped with a nested
    ``bucket`` object when the upstream omits it). Returns ``(None, None)``
    when the token carries no ``bucketPermissions`` — nothing to enumerate
    from, the caller re-raises the original listing error. When permissions
    exist but every single bucket fails to list, the last per-bucket error
    is raised so the caller surfaces a real 502 instead of a silently empty
    picker.
    """
    info = client.verify_token()
    perms = info.get("bucketPermissions") or {}
    if not perms:
        return None, None

    buckets: List[dict] = []
    tables: List[dict] = []
    last_exc: Optional[Exception] = None
    for bucket_id in sorted(perms):
        try:
            bucket_tables = client.list_tables(bucket_id)
        except (StorageApiError, requests.RequestException) as exc:
            logger.warning(
                "connection %s: listing tables of bucket %s failed: %s",
                connection_id,
                bucket_id,
                exc,
            )
            last_exc = exc
            continue
        try:
            bucket = client.get_bucket(bucket_id)
        except (StorageApiError, requests.RequestException) as exc:
            # Table listing worked, so keep going — render the bucket from
            # its id alone rather than dropping its tables.
            logger.warning(
                "connection %s: bucket detail for %s failed (%s); rendering from id",
                connection_id,
                bucket_id,
                exc,
            )
            stage, _, rest = bucket_id.partition(".")
            bucket = {"id": bucket_id, "name": rest or bucket_id, "stage": stage, "description": ""}
        buckets.append(bucket)
        for t in bucket_tables:
            if not isinstance(t, dict):
                continue
            if not t.get("bucket"):
                t["bucket"] = {"id": bucket_id}
            tables.append(t)
    if not buckets and last_exc is not None:
        raise last_exc
    logger.info(
        "connection %s: bucket-scoped token fallback listed %d bucket(s), %d table(s)",
        connection_id,
        len(buckets),
        len(tables),
    )
    return buckets, tables


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

    Tokens with project-wide read use the project-wide ``/buckets`` +
    ``/tables`` listings. When those are refused — typically a bucket-scoped
    (custom access) token — the endpoint falls back to enumerating the
    token's own ``bucketPermissions`` per bucket, so the picker shows
    exactly what the token can read (``scope: "token_buckets"`` in the
    response marks the fallback; the default is ``scope: "project"``).

    REST-only — admin-UI helper with no analyst-facing CLI/MCP analogue (see
    ``_EXEMPT`` in ``tests/test_documentation_api_triple_surface.py``).

    404 if the connection doesn't exist. 400 if the connection isn't
    ``source_type='keboola'`` (the only source type supported today), if no
    ``stack_url`` is configured, or if no token is resolvable (vault empty,
    ``token_env`` unset). 502 if the upstream Storage API calls fail (both
    the project-wide listing and the per-bucket fallback).

    Returns ``{"buckets": [{"id", "name", "stage", "description", "tables": [
    {"id", "name", "rows", "size_bytes"}, ...]}, ...], "scope": "project" |
    "token_buckets"}``.
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

    def _project_listing() -> tuple[List[dict], List[dict]]:
        return client.list_buckets(), client.list_tables()

    scope = "project"
    started = time.monotonic()
    try:
        buckets, tables = await run_in_threadpool(_project_listing)
    except (StorageApiError, requests.RequestException) as exc:
        # Only a PERMISSION failure justifies the per-bucket fallback. That loop
        # makes two upstream calls per bucket the token can see, so on a large
        # project a transient blip would otherwise stall the admin's "Browse &
        # register tables" for minutes and then mislabel a full-access token as
        # `token_buckets`. A 5xx or a connection error is not a scope problem, so
        # it surfaces as itself (Devin Review on #1189).
        # Require an EXPLICIT refusal. The first version of this gate read
        # `status is not None and status not in (401, 403)`, which let a
        # status-less StorageApiError fall THROUGH to the fallback — the very
        # case the gate exists to prevent. `_parse_list` always stamps a status
        # today, so it was unreachable, but the condition said the opposite of
        # its intent and would have started lying the moment some other path
        # raised without one (Devin Review on #1189).
        if getattr(exc, "status", None) not in (401, 403):
            # An HTTPException leaves no server-side trace, unlike the catch-all
            # 500 this path replaced — so a transport failure (DNS, refused
            # connection, read timeout, TLS) was visible only to the admin who
            # happened to click. Redacted: the message can echo a proxy's reply,
            # and `_redact` is what keeps a token out of the log line.
            redacted = client._redact(exc)
            logger.warning(
                "tables listing failed for connection %s (%s): %s",
                connection_id,
                _log_host(stack_url),
                redacted,
            )
            raise HTTPException(status_code=502, detail=f"keboola_storage_api_error: {redacted}") from exc
        # The client's messages already redact response bodies; the token
        # itself travels in a header and never appears in the exception.
        logger.warning(
            "connection %s: project-wide bucket/table listing was refused (%s); "
            "retrying per-bucket via the token's bucketPermissions",
            connection_id,
            exc,
        )
        try:
            buckets, tables = await run_in_threadpool(_scoped_listing, client, connection_id)
        except (StorageApiError, requests.RequestException) as scoped_exc:
            raise HTTPException(status_code=502, detail=f"keboola_storage_api_error: {scoped_exc}") from scoped_exc
        if buckets is None or tables is None:
            # Token has no bucketPermissions to enumerate — the original
            # project-wide failure is the real story.
            raise HTTPException(status_code=502, detail=f"keboola_storage_api_error: {exc}") from exc
        scope = "token_buckets"
    else:
        # A refusal is not the only way a scoped token hides the project: some
        # token shapes get a 200 with an EMPTY array from /v2/storage/buckets
        # instead of a 403. That looked identical to an empty project, so the
        # picker said "no buckets visible to this token" and the fallback never
        # ran — the exact case this endpoint's fallback exists for
        # (Devin Review on #1189).
        #
        # Safe for a genuinely empty project: `_scoped_listing` returns
        # (None, None) when the token carries no bucketPermissions, and we keep
        # the empty project-wide answer. Cost there is one extra verify_token.
        if not buckets:
            try:
                scoped_buckets, scoped_tables = await run_in_threadpool(_scoped_listing, client, connection_id)
            except (StorageApiError, requests.RequestException) as scoped_exc:
                # The project-wide call succeeded, so an empty answer is still a
                # valid one — don't turn a working (if empty) picker into a 502.
                logger.warning(
                    "connection %s: empty project-wide listing, and the per-bucket "
                    "retry failed too (%s); reporting the empty project view",
                    connection_id,
                    scoped_exc,
                )
                scoped_buckets = scoped_tables = None
            if scoped_buckets:
                buckets, tables = scoped_buckets, scoped_tables or []
                scope = "token_buckets"

    # Outcome trail for a probe that is otherwise invisible server-side.
    # Host, not stack_url, and counts, not content: the token travels in a
    # header and a proxy-echoed body must never reach the logs.
    logger.info(
        "tables listing for connection %s (%s): %d buckets, %d tables in %.1fs (scope=%s)",
        connection_id,
        _log_host(stack_url),
        len(buckets),
        len(tables),
        time.monotonic() - started,
        scope,
    )

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

    return {"buckets": result, "scope": scope}
