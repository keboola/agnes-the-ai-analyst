"""Per-instance Initial Workspace Template — admin + analyst endpoints.

Config lives in ``${DATA_DIR}/state/instance.yaml`` under the
``initial_workspace:`` key. Token (PAT) lives in ``.env_overlay`` via
``app.secrets.persist_overlay_token``; YAML stores only the env-var name.

Endpoints:

  GET    /api/admin/initial-workspace          admin: read current config
  POST   /api/admin/initial-workspace          admin: register / edit
  DELETE /api/admin/initial-workspace          admin: remove
  POST   /api/admin/initial-workspace/sync     admin: manual "Sync now"

  GET    /api/initial-workspace                analyst (PAT): status + manifest
  GET    /api/initial-workspace.zip            analyst (PAT): content
  POST   /api/initial-workspace/applied        analyst (PAT): audit event

When admin registers a template, ``agnes init`` (new CLI flow) probes
``GET /api/initial-workspace``; on ``configured: true`` it downloads the
zip + extracts to the analyst's workspace, bypassing the default
Agnes-generated workspace files entirely. See ``docs/initial-workspace-override.md``
for the full responsibility-transfer contract.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.auth.access import require_admin
from app.auth.dependencies import _get_db, get_current_user, get_optional_user
from app.secrets import persist_overlay_token
from src.initial_workspace import (
    TemplateValidationError,
    build_zip,
    delete_template_dir,
    list_template_files,
    sync_template,
)
from src.repositories.audit import AuditRepository

logger = logging.getLogger(__name__)

# Two routers so the admin path can sit under one prefix and the
# analyst-facing path under another; both are registered in app/main.py.
router = APIRouter(tags=["initial_workspace"])

# Conventional env-var name for the singleton template PAT. Mirrors the
# marketplace pattern (`AGNES_MARKETPLACE_<SLUG>_TOKEN`) so an operator
# poking around ``.env_overlay`` can recognize the line at a glance.
_TOKEN_ENV_NAME = "AGNES_INITIAL_WORKSPACE_TOKEN"


# ---------------------------------------------------------------------------
# Pydantic shapes
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    """Upsert payload — fields not provided are left untouched on update.

    ``token = None`` means "leave existing PAT alone".
    ``token = ""``   means "clear PAT".
    ``token = "ghp_..."`` means "set/rotate PAT".
    """

    url: str
    branch: Optional[str] = None
    token: Optional[str] = None


class AdminInitialWorkspaceResponse(BaseModel):
    """Admin-facing view; surfaces sync state + has_token (no secret leak)."""

    configured: bool = False
    url: Optional[str] = None
    branch: Optional[str] = None
    has_token: bool = False
    last_synced_at: Optional[str] = None
    last_commit_sha: Optional[str] = None
    last_error: Optional[str] = None
    file_count: int = 0


class AnalystInitialWorkspaceResponse(BaseModel):
    """PAT-authed analyst view; what ``agnes init`` consumes."""

    configured: bool = False
    synced: bool = False
    template_source: Optional[str] = None
    template_sha: Optional[str] = None
    synced_at: Optional[str] = None
    files: list[str] = []


class AppliedRequest(BaseModel):
    """CLI audit event after the analyst's workspace has been extracted."""

    mode: str  # "force_overwrite" | "fresh_install"
    template_sha: Optional[str] = None
    files_overwritten: int = 0
    files_created: int = 0


# ---------------------------------------------------------------------------
# YAML overlay read/write helpers
# ---------------------------------------------------------------------------


def _read_section() -> dict:
    """Return ``initial_workspace:`` section from the merged static + overlay
    instance.yaml, or empty dict when the section is absent.
    """
    from app.api.admin import _load_current_instance_yaml
    cfg = _load_current_instance_yaml()
    section = cfg.get("initial_workspace") if isinstance(cfg, dict) else None
    return section if isinstance(section, dict) else {}


def _write_section(patch: dict) -> dict:
    """Deep-merge ``patch`` into the ``initial_workspace:`` overlay section
    and write the file atomically. Returns the resulting section.

    Mirrors ``app/api/admin.py::update_server_config``'s read-modify-write
    sequence but scoped to one section: invalidate cache, read overlay,
    merge, atomic write, invalidate cache again. Serialized by the same
    ``_overlay_write_lock`` so concurrent saves from this endpoint AND
    from /admin/server-config don't race.
    """
    import yaml

    from app.api.admin import _deep_merge, _overlay_write_lock
    from app.instance_config import reset_cache
    from app.secrets import _state_dir

    config_path = _state_dir() / "instance.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    with _overlay_write_lock:
        reset_cache()

        # Read existing overlay payload — never the merged static + overlay
        # view. Writing the merged result back would copy static keys (and
        # resolved env-var placeholders) into the overlay, shadowing future
        # updates to the static file. Same rationale as the marketplace +
        # server-config write paths.
        overlay_payload: dict[str, Any] = {}
        if config_path.exists():
            try:
                overlay_payload = yaml.safe_load(config_path.read_text()) or {}
            except Exception as e:
                logger.exception(
                    "initial-workspace: refusing to overwrite corrupt overlay at %s",
                    config_path,
                )
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"refusing to overwrite corrupt overlay at {config_path} ({e}); "
                        "back up and remove the file, or fix it by hand"
                    ),
                ) from e

        existing = overlay_payload.get("initial_workspace")
        if not isinstance(existing, dict):
            existing = {}
        merged = _deep_merge(existing, patch)
        overlay_payload["initial_workspace"] = merged

        tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        tmp_path.write_text(
            yaml.dump(overlay_payload, default_flow_style=False, sort_keys=False)
        )
        os.replace(tmp_path, config_path)
        logger.info(
            "initial-workspace: wrote `initial_workspace:` section to %s",
            config_path,
        )

        reset_cache()
        return merged


def _drop_section() -> bool:
    """Remove the ``initial_workspace:`` section from the overlay file.
    Returns True iff a section was present and removed.
    """
    import yaml

    from app.api.admin import _overlay_write_lock
    from app.instance_config import reset_cache
    from app.secrets import _state_dir

    config_path = _state_dir() / "instance.yaml"
    if not config_path.exists():
        return False

    with _overlay_write_lock:
        reset_cache()
        try:
            overlay_payload = yaml.safe_load(config_path.read_text()) or {}
        except Exception as e:
            logger.exception(
                "initial-workspace: refusing to overwrite corrupt overlay at %s",
                config_path,
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    f"refusing to overwrite corrupt overlay at {config_path} ({e}); "
                    "back up and remove the file, or fix it by hand"
                ),
            ) from e
        if "initial_workspace" not in overlay_payload:
            return False
        overlay_payload.pop("initial_workspace", None)
        tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
        tmp_path.write_text(
            yaml.dump(overlay_payload, default_flow_style=False, sort_keys=False)
        )
        os.replace(tmp_path, config_path)
        reset_cache()
        return True


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: Optional[str],
    action: str,
    params: Optional[dict] = None,
) -> None:
    """Same shape as ``app/api/marketplaces.py::_audit``. Best-effort —
    audit failure must never abort the actual operation.
    """
    try:
        safe_params: Optional[dict] = None
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
            resource="initial_workspace",
            params=safe_params,
        )
    except Exception:
        logger.exception("audit log write failed for %s", action)


def _section_to_admin_response(
    section: dict, file_count: int = 0
) -> AdminInitialWorkspaceResponse:
    if not section.get("url"):
        return AdminInitialWorkspaceResponse(configured=False)
    token_env = section.get("token_env") or ""
    has_token = bool(token_env) and bool(os.environ.get(token_env, ""))
    return AdminInitialWorkspaceResponse(
        configured=True,
        url=section.get("url"),
        branch=section.get("branch"),
        has_token=has_token,
        last_synced_at=section.get("last_synced_at"),
        last_commit_sha=section.get("last_commit_sha"),
        last_error=section.get("last_error"),
        file_count=file_count,
    )


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/api/admin/initial-workspace",
    response_model=AdminInitialWorkspaceResponse,
)
async def admin_get(
    user: dict = Depends(require_admin),
):
    """Return the current ``initial_workspace:`` config + sync state."""
    section = _read_section()
    file_count = len(list_template_files()) if section.get("last_commit_sha") else 0
    return _section_to_admin_response(section, file_count=file_count)


@router.post(
    "/api/admin/initial-workspace",
    response_model=AdminInitialWorkspaceResponse,
)
async def admin_post(
    body: RegisterRequest,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Register / update the template repo.

    Only ``url``, ``branch``, and ``token_env`` land in the YAML overlay.
    Sync state (``last_synced_at`` / ``last_commit_sha`` / ``last_error``)
    is written by ``POST .../sync``, not here — saving a config change
    should NOT silently invalidate the existing sync state.
    """
    url = (body.url or "").strip()
    if not url:
        raise HTTPException(status_code=422, detail="url is required")
    if not url.startswith("https://"):
        # Match the marketplace contract: HTTPS only. file://, ssh://, http://
        # are rejected outright at this layer rather than letting `git clone`
        # surface a less-clear failure later.
        raise HTTPException(
            status_code=422,
            detail="url must be https://",
        )

    patch: dict[str, Any] = {
        "url": url,
        "branch": (body.branch or "").strip() or None,
    }

    # Token routing — same three-state semantics as the marketplace POST:
    # None = leave alone, "" = clear, non-empty = rotate.
    token_changed: Optional[str] = None
    if body.token is not None:
        if body.token == "":
            persist_overlay_token(_TOKEN_ENV_NAME, None)
            patch["token_env"] = None
            token_changed = "cleared"
        else:
            persist_overlay_token(_TOKEN_ENV_NAME, body.token)
            patch["token_env"] = _TOKEN_ENV_NAME
            token_changed = "rotated"

    merged = _write_section(patch)

    _audit(
        conn,
        actor_id=user.get("id"),
        action="initial_workspace.register",
        params={
            "url": url,
            "branch": patch.get("branch"),
            "token": token_changed,
        },
    )

    file_count = (
        len(list_template_files()) if merged.get("last_commit_sha") else 0
    )
    return _section_to_admin_response(merged, file_count=file_count)


@router.delete("/api/admin/initial-workspace", status_code=204)
async def admin_delete(
    purge: bool = False,
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Remove the ``initial_workspace:`` config + PAT.

    Optional ``?purge=true`` also wipes ``${DATA_DIR}/initial-workspace/``
    from disk. Default leaves the working copy in place so an admin can
    inspect / re-register the same URL without re-cloning.
    """
    section = _read_section()
    had_section = _drop_section()
    if section.get("token_env"):
        persist_overlay_token(section["token_env"], None)
    purged = False
    if purge:
        purged = delete_template_dir()

    _audit(
        conn,
        actor_id=user.get("id"),
        action="initial_workspace.delete",
        params={"purge": purge, "purged": purged, "had_section": had_section},
    )
    return Response(status_code=204)


@router.post("/api/admin/initial-workspace/sync")
async def admin_sync(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Manual "Sync now" — clone or fast-forward the template repo, then
    persist ``last_synced_at`` + ``last_commit_sha`` (or ``last_error``)
    back to the YAML overlay.

    Returns ``{action, commit_sha, file_count, path}`` on success or
    surfaces a ``400`` with the validation / git error so the admin sees
    it in the Sync-now modal. The error payload uses the typed-``kind``
    shape the CLI's error renderer already understands.
    """
    section = _read_section()
    if not section.get("url"):
        raise HTTPException(
            status_code=400,
            detail={"kind": "not_configured", "hint": "Register a repo first"},
        )

    try:
        result = sync_template(
            url=section["url"],
            branch=section.get("branch"),
            token_env=section.get("token_env"),
        )
    except TemplateValidationError as e:
        # Persist the error so the admin UI can render it on next page
        # load (the modal-result path also displays it inline). The repo
        # on disk stays as cloned so the admin can inspect what's there.
        _write_section({"last_error": str(e)})
        _audit(
            conn,
            actor_id=user.get("id"),
            action="initial_workspace.sync_failed",
            params={"error": str(e), "kind": "validation"},
        )
        raise HTTPException(
            status_code=400,
            detail={"kind": "template_invalid", "message": str(e)},
        ) from None
    except (RuntimeError, ValueError) as e:
        _write_section({"last_error": str(e)})
        _audit(
            conn,
            actor_id=user.get("id"),
            action="initial_workspace.sync_failed",
            params={"error": str(e), "kind": "git"},
        )
        raise HTTPException(
            status_code=400,
            detail={"kind": "git_failed", "message": str(e)},
        ) from None

    now_iso = datetime.now(timezone.utc).isoformat()
    _write_section({
        "last_synced_at": now_iso,
        "last_commit_sha": result["commit_sha"],
        "last_error": None,
    })

    _audit(
        conn,
        actor_id=user.get("id"),
        action="initial_workspace.sync",
        params={
            "commit_sha": result["commit_sha"],
            "file_count": result["file_count"],
        },
    )

    return {
        "action": "sync_ok",
        "commit_sha": result["commit_sha"],
        "file_count": result["file_count"],
        "path": result["path"],
        "synced_at": now_iso,
    }


# ---------------------------------------------------------------------------
# Analyst (PAT-authed) endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/api/initial-workspace",
    response_model=AnalystInitialWorkspaceResponse,
)
async def analyst_status(
    user: dict = Depends(get_current_user),
):
    """Status probe consumed by ``agnes init``. Always 200.

    Returns ``configured: false`` when no template is registered (CLI then
    falls through to the existing default flow). Returns ``configured:
    true, synced: false`` when registered but never synced (or last sync
    failed); CLI shows a typed error pointing at /admin/server-config.
    Returns full metadata + manifest when configured + synced.
    """
    section = _read_section()
    if not section.get("url"):
        return AnalystInitialWorkspaceResponse(configured=False)
    synced = bool(section.get("last_commit_sha"))
    return AnalystInitialWorkspaceResponse(
        configured=True,
        synced=synced,
        template_source=section.get("url"),
        template_sha=section.get("last_commit_sha"),
        synced_at=section.get("last_synced_at"),
        files=list_template_files() if synced else [],
    )


@router.get("/api/initial-workspace.zip")
async def analyst_zip(
    request: Request,
    user: Optional[dict] = Depends(get_optional_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Return the zip of the cloned template tree (sans ``.git/``).

    Writes a server-side ``initial_workspace.fetch_started`` audit row so
    we have an authoritative event the analyst's PAT-holder cannot spoof
    (the matching ``initial_workspace.applied`` event from
    ``POST /applied`` is best-effort).

    404 when not configured (the CLI status probe should have caught
    this; defense in depth). 503 when configured but never synced — the
    CLI then surfaces a typed error pointing at "Sync now".
    """
    if user is None:
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(
                url="/login?next=/api/initial-workspace.zip", status_code=302
            )
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    section = _read_section()
    if not section.get("url"):
        raise HTTPException(status_code=404, detail={"kind": "not_configured"})
    if not section.get("last_commit_sha"):
        raise HTTPException(
            status_code=503,
            detail={
                "kind": "initial_workspace_not_synced",
                "hint": "Admin must Sync now in /admin/server-config",
            },
        )

    try:
        data = build_zip()
    except TemplateValidationError as e:
        # Defense in depth — sync_template already validates, but a
        # manual edit on disk between sync and zip-fetch should fail
        # closed rather than serve invalid content.
        logger.warning("initial-workspace: build_zip validation failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"kind": "template_invalid", "message": str(e)},
        ) from None

    sha = section["last_commit_sha"]
    _audit(
        conn,
        actor_id=user.get("id"),
        action="initial_workspace.fetch_started",
        params={"template_sha": sha, "byte_count": len(data)},
    )

    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "ETag": f'"{sha}"',
            "Content-Disposition": 'attachment; filename="initial-workspace.zip"',
        },
    )


@router.post("/api/initial-workspace/applied")
async def analyst_applied(
    body: AppliedRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Best-effort audit event from ``agnes init`` confirming the
    analyst's workspace has been extracted + sentinel written.

    The authoritative anchor is the server-side
    ``initial_workspace.fetch_started`` event written by ``GET .../zip`` —
    a fetch_started without a matching applied = the analyst downloaded
    but never confirmed extraction (useful signal for operators).
    """
    if body.mode not in ("force_overwrite", "fresh_install"):
        raise HTTPException(
            status_code=422,
            detail=f"mode must be one of: force_overwrite, fresh_install (got {body.mode!r})",
        )
    _audit(
        conn,
        actor_id=user.get("id"),
        action="initial_workspace.applied",
        params={
            "mode": body.mode,
            "template_sha": body.template_sha,
            "files_overwritten": body.files_overwritten,
            "files_created": body.files_created,
        },
    )
    return {"status": "ok"}
