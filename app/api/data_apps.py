"""Control-plane REST API for hosted data apps (v96 registry).

Composes everything the earlier data-apps tasks built: the ``data_apps``
registry (``src/repositories/data_apps.py``), the container-spec / runtime
config.json builders (``src/data_apps/spec.py``), the git-repo lifecycle
helpers (``src/data_apps/git_repos.py``), the sidecar HTTP client
(``src/data_apps/runner_client.py``), and the secret vault
(``app/secrets_vault.py``).

Endpoints (see ``docs/superpowers/plans/2026-07-21-data-apps-platform.md``
Task 7 for the full design rationale):

  - ``GET    /api/data-apps``                — list apps the caller can see
  - ``POST   /api/data-apps``                — create (quota + slug checks)
  - ``GET    /api/data-apps/{slug}``          — detail (RBAC-gated)
  - ``POST   /api/data-apps/{slug}/deploy``   — fast-forward + mint service
    token + build spec + hand to the runner sidecar
  - ``POST   /api/data-apps/{slug}/stop``     — runner stop, state -> stopped
  - ``DELETE /api/data-apps/{slug}``          — runner stop + token revoke +
    row delete (repo directory intentionally left on disk)
  - ``PUT    /api/data-apps/{slug}/secrets``  — encrypt + store secrets
  - ``GET    /api/data-apps/{slug}/logs``     — runner logs (owner/Admin)
  - ``GET    /api/data-apps/{slug}/readiness``— any RBAC-passing caller
  - ``POST   /api/data-apps/reap-idle``       — admin-only idle sweep

RBAC: owner of the app, Admin (god-mode), or a group holding a
``resource_grants`` row on ``(data_app, <slug>)`` may *view*; only owner or
Admin may mutate (deploy/stop/delete/secrets/logs).

``_runner()`` is a module-level indirection (not a constructed singleton) so
tests can monkeypatch ``app.api.data_apps._runner`` with a stub — the single
seam the whole feature's tests rely on.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from typing import Any, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth.access import can_access, is_user_admin, require_admin
from app.auth.dependencies import _get_db, get_current_user
from app.auth.jwt import create_access_token
from app.instance_config import get_data_apps_config, get_public_url
from app.resource_types import ResourceType
from app.secrets_vault import VaultKeyNotConfiguredError, decrypt_secret, encrypt_secret
from src.data_apps.git_repos import fast_forward_live, init_app_repo
from src.data_apps.runner_client import RunnerClient, RunnerError, RunnerUnavailable
from src.data_apps.spec import AGNES_INTERNAL_URL, SLUG_RE, build_config_json, build_container_spec
from src.repositories import access_token_repo, audit_repo, data_apps_repo, users_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data-apps", tags=["data-apps"])

# idle_timeout_s clamp — 5 minutes .. 24 hours. Prevents an accidental 0/huge
# value from either reaping an app instantly or never reaping it at all.
_IDLE_TIMEOUT_MIN = 300
_IDLE_TIMEOUT_MAX = 86400

# Fallback values for keys instance.yaml's `data_apps:` block may omit —
# mirrors the documented defaults in config/instance.yaml.example so an
# operator who only sets `enabled: true` still gets a working feature.
_CONFIG_DEFAULTS = {
    "runtime_image": "keboolapublic.azurecr.io/data-app-python-js:1.6.2_python-3.13_node-24",
    "subdomain_base": "",
    "default_idle_timeout_s": 1800,
    "default_sleep_mode": "recreate",
    "default_mem_limit": "1g",
    "default_cpus": 1.0,
    "max_apps_per_user": 3,
}


def _runner() -> RunnerClient:
    return RunnerClient()


def _effective_config() -> dict:
    return {**_CONFIG_DEFAULTS, **get_data_apps_config()}


def _audit(
    conn: duckdb.DuckDBPyConnection,
    actor_id: str,
    action: str,
    resource: str,
    params: Optional[dict] = None,
) -> None:
    try:
        audit_repo().log(user_id=actor_id, action=action, resource=resource, params=params)
    except Exception:
        logger.warning("audit log failed for %s/%s", action, resource)


def _feature_gate() -> None:
    if not get_data_apps_config().get("enabled"):
        raise HTTPException(status_code=404, detail="data_apps_disabled")


def _can_view(user: dict, row: dict) -> bool:
    if user["id"] == row["owner_user_id"]:
        return True
    if is_user_admin(user["id"]):
        return True
    return can_access(user["id"], ResourceType.DATA_APP.value, row["slug"])


def _require_owner_or_admin(user: dict, row: dict) -> None:
    if user["id"] == row["owner_user_id"] or is_user_admin(user["id"]):
        return
    raise HTTPException(status_code=403, detail="forbidden")


def _get_row_or_404(slug: str) -> dict:
    row = data_apps_repo().get_by_slug(slug)
    if not row:
        raise HTTPException(status_code=404, detail="data_app_not_found")
    return row


def _app_url(slug: str, cfg: dict) -> str:
    base = (cfg.get("subdomain_base") or "").strip()
    if base:
        return f"https://{slug}.{base}/"
    return f"/apps/{slug}/"


def _serialize(row: dict, cfg: Optional[dict] = None) -> dict:
    cfg = cfg if cfg is not None else _effective_config()
    out = {k: v for k, v in row.items() if k not in ("secrets_enc", "service_token_id")}
    out["url"] = _app_url(row["slug"], cfg)
    return out


def _clamp_idle_timeout(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    if value < _IDLE_TIMEOUT_MIN or value > _IDLE_TIMEOUT_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"idle_timeout_s must be between {_IDLE_TIMEOUT_MIN} and {_IDLE_TIMEOUT_MAX}",
        )
    return value


def _decrypt_secrets(row: dict) -> dict:
    enc = row.get("secrets_enc")
    if not enc:
        return {}
    try:
        return json.loads(decrypt_secret(enc.encode("ascii")))
    except Exception:
        logger.warning("failed to decrypt secrets for data app %s; deploying with none", row["slug"])
        return {}


def _revoke_service_token(row: dict) -> None:
    token_id = row.get("service_token_id")
    if not token_id:
        return
    try:
        access_token_repo().revoke(token_id)
    except Exception:
        logger.warning("failed to revoke previous service token %s for data app %s", token_id, row["slug"])


def _mint_service_token(slug: str, owner: dict) -> tuple[str, str]:
    """Mint a PAT scoped to this app (`extra_claims={"scope": "data-app:<slug>"}`),
    store it via `access_token_repo().create`, and return the new token id.

    Mirrors `app/api/tokens.py::create_token`'s minting lines exactly (JWT +
    sha256 hash + prefix) — the raw JWT is only handed to `build_config_json`
    (as `#password`/`AGNES_TOKEN`), never returned to the caller.
    """
    token_id = str(uuid.uuid4())
    jwt_token = create_access_token(
        user_id=owner["id"],
        email=owner["email"],
        token_id=token_id,
        typ="pat",
        extra_claims={"scope": f"data-app:{slug}"},
    )
    prefix = token_id.replace("-", "")[:8]
    token_hash = hashlib.sha256(jwt_token.encode()).hexdigest()
    access_token_repo().create(
        id=token_id,
        user_id=owner["id"],
        name=f"data-app:{slug}",
        token_hash=token_hash,
        prefix=prefix,
        expires_at=None,
    )
    return token_id, jwt_token


def _handle_runner_failure(repo, app_id: str, exc: Exception) -> None:
    detail = getattr(exc, "detail", None) or str(exc)
    repo.set_state(app_id, "error", str(detail))


class CreateDataAppRequest(BaseModel):
    slug: str
    name: str
    description: str = ""
    repo_mode: str = "internal"
    repo_url: str = ""
    repo_branch: str = "main"
    idle_timeout_s: Optional[int] = None
    sleep_mode: Optional[str] = None


class DeployRequest(BaseModel):
    sha: Optional[str] = None


class SecretsRequest(BaseModel):
    secrets: dict[str, str] = {}


@router.get("")
async def list_data_apps(user: dict = Depends(get_current_user)):
    _feature_gate()
    cfg = _effective_config()
    rows = data_apps_repo().list()
    return [_serialize(r, cfg) for r in rows if _can_view(user, r)]


@router.post("", status_code=201)
async def create_data_app(
    payload: CreateDataAppRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _feature_gate()
    if not SLUG_RE.match(payload.slug):
        raise HTTPException(status_code=400, detail="invalid_slug")
    if payload.repo_mode not in ("internal", "external"):
        raise HTTPException(status_code=400, detail="invalid_repo_mode")
    idle_timeout_s = _clamp_idle_timeout(payload.idle_timeout_s)

    cfg = _effective_config()
    if not is_user_admin(user["id"]):
        max_apps = cfg["max_apps_per_user"]
        existing = data_apps_repo().list(owner_user_id=user["id"])
        if len(existing) >= max_apps:
            raise HTTPException(status_code=403, detail="app_quota_exceeded")

    repo = data_apps_repo()
    kwargs: dict[str, Any] = dict(
        slug=payload.slug,
        name=payload.name,
        owner_user_id=user["id"],
        description=payload.description,
        repo_mode=payload.repo_mode,
        repo_url=payload.repo_url,
        repo_branch=payload.repo_branch,
    )
    if idle_timeout_s is not None:
        kwargs["idle_timeout_s"] = idle_timeout_s
    if payload.sleep_mode is not None:
        kwargs["sleep_mode"] = payload.sleep_mode

    try:
        app_id = repo.create(**kwargs)
    except duckdb.ConstraintException:
        raise HTTPException(status_code=409, detail="slug_exists")

    if payload.repo_mode == "internal":
        init_app_repo(payload.slug)

    _audit(conn, user["id"], "data_app.create", f"data_app:{payload.slug}", {"name": payload.name})

    public = get_public_url()
    git_url = f"{public}/data-apps.git/{payload.slug}" if public else f"/data-apps.git/{payload.slug}"
    return {"id": app_id, "slug": payload.slug, "git_url": git_url}


@router.get("/{slug}")
async def get_data_app(slug: str, user: dict = Depends(get_current_user)):
    _feature_gate()
    row = _get_row_or_404(slug)
    if not _can_view(user, row):
        raise HTTPException(status_code=403, detail="forbidden")
    return _serialize(row)


@router.post("/{slug}/deploy")
async def deploy_data_app(
    slug: str,
    payload: DeployRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _feature_gate()
    row = _get_row_or_404(slug)
    _require_owner_or_admin(user, row)

    repo = data_apps_repo()
    try:
        sha = fast_forward_live(slug, payload.sha)
    except ValueError as exc:
        if "no commits to deploy" in str(exc):
            raise HTTPException(status_code=409, detail="deploy_empty_repo")
        raise HTTPException(status_code=400, detail=str(exc))

    owner = users_repo().get_by_id(row["owner_user_id"])
    if not owner:
        raise HTTPException(status_code=500, detail="owner_not_found")

    _revoke_service_token(row)
    new_token_id, jwt_token = _mint_service_token(slug, owner)
    repo.update(row["id"], service_token_id=new_token_id)

    row = repo.get(row["id"])  # refresh — carries the new service_token_id
    secrets = _decrypt_secrets(row)
    clone_url = f"{AGNES_INTERNAL_URL}/data-apps.git/{slug}"

    try:
        config_json = build_config_json(row, secrets=secrets, clone_url=clone_url, clone_token=jwt_token)
        spec = build_container_spec(row, defaults=_effective_config(), data_dir=os.environ.get("DATA_DIR", "/data"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        _runner().up(slug, spec, config_json)
    except (RunnerUnavailable, RunnerError) as exc:
        _handle_runner_failure(repo, row["id"], exc)
        raise HTTPException(status_code=502, detail="runner_unavailable")

    repo.record_deploy(row["id"], sha)
    repo.set_state(row["id"], "running")
    _audit(conn, user["id"], "data_app.deploy", f"data_app:{slug}", {"sha": sha})

    return {"state": "running", "deployed_sha": sha}


@router.post("/{slug}/stop")
async def stop_data_app(
    slug: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _feature_gate()
    row = _get_row_or_404(slug)
    _require_owner_or_admin(user, row)

    repo = data_apps_repo()
    try:
        _runner().stop(slug, mode="recreate")
    except (RunnerUnavailable, RunnerError) as exc:
        _handle_runner_failure(repo, row["id"], exc)
        raise HTTPException(status_code=502, detail="runner_unavailable")

    repo.set_state(row["id"], "stopped")
    _audit(conn, user["id"], "data_app.stop", f"data_app:{slug}")
    return {"state": "stopped"}


@router.delete("/{slug}", status_code=204)
async def delete_data_app(
    slug: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Runner stop + service-token revoke + registry row delete.

    204 No Content, no body (project convention — see
    ``tests/test_api_design_rules.py::test_delete_returns_204``). The repo
    directory under ``${DATA_DIR}/apps/git/<slug>.git`` is intentionally
    left on disk — deletion is a registry-only operation; that fact is
    recorded in the audit log params, not the response body.
    """
    _feature_gate()
    row = _get_row_or_404(slug)
    _require_owner_or_admin(user, row)

    # Best-effort: a dead runner must not block deleting the registry row
    # (there'd otherwise be no way to remove an app whose container host is
    # gone).
    try:
        _runner().stop(slug, mode="recreate")
    except (RunnerUnavailable, RunnerError):
        logger.warning("delete_data_app: runner stop failed for %s (continuing)", slug)

    _revoke_service_token(row)
    data_apps_repo().delete(row["id"])
    _audit(
        conn,
        user["id"],
        "data_app.delete",
        f"data_app:{slug}",
        {"repo_dir_left_on_disk": True},
    )


@router.put("/{slug}/secrets")
async def set_data_app_secrets(
    slug: str,
    payload: SecretsRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _feature_gate()
    row = _get_row_or_404(slug)
    _require_owner_or_admin(user, row)

    try:
        enc = encrypt_secret(json.dumps(payload.secrets))
    except VaultKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=409,
            detail="vault_key_not_configured: set AGNES_VAULT_KEY on the server before storing secrets",
        ) from exc

    data_apps_repo().update(row["id"], secrets_enc=enc.decode("ascii"))
    _audit(conn, user["id"], "data_app.secrets_update", f"data_app:{slug}", {"keys": sorted(payload.secrets)})
    return {"updated": True}


@router.get("/{slug}/logs")
async def get_data_app_logs(slug: str, tail: int = 200, user: dict = Depends(get_current_user)):
    _feature_gate()
    row = _get_row_or_404(slug)
    _require_owner_or_admin(user, row)

    try:
        logs = _runner().logs(slug, tail=tail)
    except (RunnerUnavailable, RunnerError):
        raise HTTPException(status_code=502, detail="runner_unavailable")
    return {"logs": logs}


@router.get("/{slug}/readiness")
async def get_data_app_readiness(slug: str, user: dict = Depends(get_current_user)):
    _feature_gate()
    row = _get_row_or_404(slug)
    if not _can_view(user, row):
        raise HTTPException(status_code=403, detail="forbidden")

    ready = False
    if row["state"] in ("running", "deploying"):
        try:
            status = _runner().status(slug)
            ready = bool(status.get("ready"))
        except (RunnerUnavailable, RunnerError):
            ready = False
    return {"state": row["state"], "ready": ready}


@router.post("/reap-idle")
async def reap_idle_data_apps(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Admin-only idle sweep (called by the scheduler, whose synthetic user
    is Admin). `list_idle` takes a single global threshold, but
    `idle_timeout_s` is per-app, so each `running` row is checked against
    its *own* configured threshold rather than one shared value.

    A runner failure on one app is recorded (state -> "error",
    state_detail carries the runner's message) and does not abort the rest
    of the sweep — one dead container must not wedge every other reap.
    """
    _feature_gate()
    repo = data_apps_repo()
    reaped: list[str] = []
    for row in repo.list(state="running"):
        idle_rows = repo.list_idle(row["idle_timeout_s"])
        if not any(r["id"] == row["id"] for r in idle_rows):
            continue
        try:
            _runner().stop(row["slug"], mode=row.get("sleep_mode") or "recreate")
        except (RunnerUnavailable, RunnerError) as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            repo.set_state(row["id"], "error", f"reap-idle stop failed: {detail}")
            logger.warning("reap-idle: runner stop failed for %s: %s", row["slug"], detail)
            continue
        repo.set_state(row["id"], "sleeping")
        reaped.append(row["slug"])

    _audit(conn, user["id"], "data_app.reap_idle", "data_app:*", {"reaped": reaped})
    return {"reaped": reaped}
