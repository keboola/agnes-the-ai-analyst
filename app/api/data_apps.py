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
    (drafts excluded — see the ``{slug}`` detail's inlined ``drafts`` field)
  - ``POST   /api/data-apps``                — create (quota + slug checks)
  - ``GET    /api/data-apps/{slug}``          — detail (RBAC-gated); prod
    apps carry an inlined ``drafts: [...]`` list
  - ``POST   /api/data-apps/{slug}/deploy``   — fast-forward + mint service
    token + build spec + hand to the runner sidecar
  - ``POST   /api/data-apps/{slug}/stop``     — runner stop, state -> stopped
  - ``DELETE /api/data-apps/{slug}``          — runner stop + token revoke +
    row delete (repo directory intentionally left on disk); cascades to any
    live drafts
  - ``POST   /api/data-apps/{slug}/drafts``   — create a draft copy on a
    branch (owner/Admin of the parent)
  - ``DELETE /api/data-apps/{slug}/drafts/{draft_slug}`` — tear down one
    draft (owner/Admin of the parent)
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
import re as _re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timedelta, timezone
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
from src.data_apps.spec import AGNES_INTERNAL_URL, RESERVED_SLUGS, SLUG_RE, build_config_json, build_container_spec
from src.repositories import access_token_repo, audit_repo, data_apps_repo, users_repo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/data-apps", tags=["data-apps"])

# Branch names accepted by `POST /{slug}/drafts` — a conservative git
# ref-component charset (lowercase alnum + `.` `_` `/` `-`), not the full
# git-check-ref-format grammar; good enough to reject shell/path-hostile
# input before it reaches `ensure_branch`'s `update-ref` call. This charset
# check alone still admits several forms `git-check-ref-format` refuses
# (`a..b`, `a//b`, a trailing `/` or `.`, an `x.lock` suffix) — those are
# rejected by `_is_git_valid_branch` below, checked alongside this regex.
_BRANCH_RE = _re.compile(r"^[a-z0-9][a-z0-9._/-]{0,60}$")


def _is_git_valid_branch(branch: str) -> bool:
    """Reject branch names `_BRANCH_RE`'s charset check lets through but
    `git update-ref` itself refuses (``man git-check-ref-format``, abridged
    to the rules a lowercase-alnum-`.`-`_`-`/`-`-` charset can still hit):
    a ``..`` component separator, a ``//`` empty path component, a
    trailing ``/`` or ``.``, and an ``x.lock`` suffix (reserved for git's
    own lockfiles). Checked *before* `ensure_branch` runs so a git-invalid
    name never reaches `subprocess.run(..., check=True)` and surfaces as an
    unhandled `CalledProcessError` (belt-and-braces: `create_draft` also
    catches that exception, in case some other git-invalid form slips past
    this check)."""
    return not (
        ".." in branch or "//" in branch or branch.endswith("/") or branch.endswith(".") or branch.endswith(".lock")
    )


# idle_timeout_s clamp — 5 minutes .. 24 hours. Prevents an accidental 0/huge
# value from either reaping an app instantly or never reaping it at all.
_IDLE_TIMEOUT_MIN = 300
_IDLE_TIMEOUT_MAX = 86400

# A row stuck in `deploying` longer than this (updated_at) is recovered by
# reap-idle rather than left wedged forever — see reap_idle_data_apps. Covers
# both wake paths: the ingress proxy's fire-and-forget recreate-mode wake
# (`app/api/data_apps_proxy.py::_spawn_wake`) whose background task died
# without a caller left to notice, and an operator-triggered `POST .../deploy`
# whose request process crashed mid-flight. 10 minutes is generous relative to
# a normal container pull/start, short enough that an operator doesn't wait a
# full idle_timeout_s cycle to find out a wake silently failed.
_DEPLOY_STALE_TIMEOUT_S = 600

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

# `POST /api/data-apps` quota-check-then-create serialization. Short TTL —
# the lease is only held for the duration of one create request, never
# renewed; ttl_s is a crash-safety backstop, not the expected hold time.
_CREATE_LEASE_TTL_S = 10
_CREATE_LEASE_RETRIES = 3
_CREATE_LEASE_RETRY_DELAY_S = 0.1


def _runner() -> RunnerClient:
    return RunnerClient()


def _acquire_create_lease(user_id: str) -> tuple[bool, str, str]:
    """Serialize concurrent `POST /api/data-apps` calls for the same user so
    the count-then-create quota check can't race (two concurrent requests
    both observe `count < max_apps_per_user` and both proceed, landing the
    user over quota).

    Returns `(held, lease_name, holder)`. `held=False` with no exception
    means either the coordination backend is unavailable (single-process
    dev fallback: proceed unserialized rather than hard-fail create
    entirely) — logged, not raised. Once retries are exhausted against a
    lease actually held by a concurrent request, raises 409
    `create_in_progress` instead of returning.
    """
    from app.coordination.base import CoordinationUnavailable
    from app.coordination.factory import coordination
    from app.coordination.leases import default_holder_id

    lease_name = f"dataapp:create:{user_id}"
    holder = default_holder_id()
    try:
        for attempt in range(_CREATE_LEASE_RETRIES):
            if coordination().lease_acquire(lease_name, holder, ttl_s=_CREATE_LEASE_TTL_S):
                return True, lease_name, holder
            if attempt < _CREATE_LEASE_RETRIES - 1:
                time.sleep(_CREATE_LEASE_RETRY_DELAY_S)
    except CoordinationUnavailable:
        logger.warning("create-lease: coordination backend unavailable; proceeding unserialized")
        return False, lease_name, holder
    raise HTTPException(status_code=409, detail="create_in_progress")


def _release_create_lease(lease_name: str, holder: str) -> None:
    from app.coordination.base import CoordinationUnavailable
    from app.coordination.factory import coordination

    try:
        coordination().lease_release(lease_name, holder)
    except CoordinationUnavailable:
        pass


# `POST /{slug}/deploy`, `POST /{slug}/stop`, `DELETE /{slug}`, the
# scheduler's idle-reap sweep, and the ingress proxy's wake-on-request path
# (`_trigger_wake` in `app/api/data_apps_proxy.py`) all end up calling the
# runner sidecar's `up()`/`stop()` for the same slug.
# `services/apps_runner/api.py::up()` does an unlocked check-then-act (get
# old container -> remove -> run new) — two of these calls racing for the
# same slug can both observe the same "old" container and both call
# `containers.run(...)`, landing two containers fighting over the same
# name/network. `dataapp:op:{slug}` is the single lease shared by all these
# call sites so at most one runner-mutating operation is ever in flight per
# app. The idle-reap sweep uses the non-blocking `try_acquire_op_lease`
# directly (skip-and-retry-next-tick) rather than `require_op_lease`
# (retry-then-409) since it has no HTTP caller to return an error to.
#
# It intentionally lives here rather than inside `redeploy_current`:
# `_trigger_wake` and `deploy_data_app` both call `redeploy_current`, and
# each already holds this lease itself before doing so — acquiring it
# again inside `redeploy_current` would be a self-deadlock (`lease_acquire`
# is not reentrant for the same holder, see `CoordinationBackend`'s
# docstring).
_OP_LEASE_TTL_S = 120
_OP_LEASE_RETRIES = 3
_OP_LEASE_RETRY_DELAY_S = 0.1


def _op_lease_name(slug: str) -> str:
    return f"dataapp:op:{slug}"


def try_acquire_op_lease(slug: str) -> tuple[bool, str]:
    """One non-blocking attempt to acquire the per-slug op lease.

    Used by `_trigger_wake`, which must never block the ingress request
    on another in-flight operation — losing the race just means
    returning immediately (the caller renders the holding page either
    way), same as the wake-specific lease this replaces. Synchronous
    endpoints that need retry-then-409 semantics instead call
    `require_op_lease`.

    Returns `(acquired, holder)`. On `CoordinationUnavailable` (no
    cross-process backend configured), treats the lease as acquired —
    single-process dev fallback: proceed unserialized rather than
    refusing the operation just because coordination happens to be down.
    """
    from app.coordination.base import CoordinationUnavailable
    from app.coordination.factory import coordination
    from app.coordination.leases import default_holder_id

    holder = default_holder_id()
    try:
        acquired = coordination().lease_acquire(_op_lease_name(slug), holder, ttl_s=_OP_LEASE_TTL_S)
    except CoordinationUnavailable:
        return True, holder
    return acquired, holder


def release_op_lease(slug: str, holder: str) -> None:
    from app.coordination.base import CoordinationUnavailable
    from app.coordination.factory import coordination

    try:
        coordination().lease_release(_op_lease_name(slug), holder)
    except CoordinationUnavailable:
        pass


def require_op_lease(slug: str) -> str:
    """Synchronous-endpoint policy for `deploy_data_app`/`stop_data_app`: a
    few quick retries against `try_acquire_op_lease`, then 409
    `operation_in_progress` if the lease is still held by someone else
    (a concurrent deploy/stop request, or an in-flight wake). The retries
    only smooth over near-simultaneous requests about to release on their
    own — a genuinely in-flight operation (e.g. a wake's backgrounded
    redeploy, held for up to `_OP_LEASE_TTL_S`) is expected to make the
    caller retry later, not block the request for the full TTL.

    Returns the holder id to pass to `release_op_lease` in a `finally`.
    """
    holder = ""
    for attempt in range(_OP_LEASE_RETRIES):
        acquired, holder = try_acquire_op_lease(slug)
        if acquired:
            return holder
        if attempt < _OP_LEASE_RETRIES - 1:
            time.sleep(_OP_LEASE_RETRY_DELAY_S)
    raise HTTPException(status_code=409, detail="operation_in_progress")


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


def _rmtree_config_dir(slug: str) -> None:
    """Best-effort removal of the RUNTIME config dir (``${DATA_DIR}/apps/<slug>``,
    holding the ``config.json`` apps-runner wrote — see ``_resolve_host_path``
    in ``services/apps_runner/api.py``). It carries the now-revoked service
    JWT in plaintext, so it's removed as hygiene on both a full app delete
    (``delete_data_app``) and a single draft delete/cascade
    (``_teardown_draft``). Shared so the two call sites can't drift."""
    config_dir = os.path.join(os.environ.get("DATA_DIR", "/data"), "apps", slug)
    try:
        shutil.rmtree(config_dir, ignore_errors=False)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("failed to remove config dir %s (continuing)", config_dir)


def _mint_service_token(slug: str, owner: dict) -> tuple[str, str]:
    """Mint a PAT for this app's owner, store it via `access_token_repo().create`,
    and return the new token id.

    The `scope: "data-app:<slug>"` claim is a label for `agnes admin token
    list`/audit purposes only — no code path enforces it, so this is
    functionally a full-privilege PAT for `owner`, not one actually confined
    to this app's API surface. Any code running inside the hosted container
    (including an externally-cloned, less-trusted repo) can use it against
    the whole Agnes REST API. This mirrors the documented trade-off in
    docs/DEPLOYMENT.md ("granting access to view/open an app is an act of
    publication") — narrowing it to a real per-app scope is a follow-up.

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


_GIT_CREDENTIAL_TTL = timedelta(hours=24)


def _mint_git_credential(row: dict) -> str:
    """Mint a PAT scoped `data-app-git:<slug>` for this app's owner and
    return a clone URL with it embedded as `agnes:<jwt>@` basic-auth.

    This is the *agent's* push credential for working on the app's repo —
    independent of (and never stored as) the container's own
    `service_token_id` runtime credential minted by `_mint_service_token`.
    Unlike that one (deliberately unbounded, mirrors upstream's "unlimited
    per-app credentials"), this credential is scoped to a single authoring
    session — it expires after `_GIT_CREDENTIAL_TTL` (24h), stored on both
    the JWT's `exp` claim (via `expires_delta`) and the DB row's
    `expires_at`, kept in sync so neither outlives the other.

    The base URL is `get_public_url()` (same helper `create_data_app` uses
    for its own `git_url`) so the clone URL works from an analyst laptop,
    the MCP tool, or a remote sandbox — none of which can resolve
    `AGNES_INTERNAL_URL` (the in-cluster hostname). Unlike `create_data_app`,
    which falls back to a root-relative path when no public URL is
    configured, this credential must embed `agnes:<jwt>@` into an absolute
    URL with a scheme+host to put the basic-auth in, so an unconfigured
    public URL falls back to `AGNES_INTERNAL_URL` instead (the previous,
    always-internal behavior) rather than a schemeless path. This is
    unrelated to `redeploy_current`'s own `clone_url`, which stays on
    `AGNES_INTERNAL_URL` unconditionally — that one is used *inside* the
    container's config.json, which only ever runs in-cluster.
    """
    owner = users_repo().get_by_id(row["owner_user_id"])
    if not owner:
        raise OwnerNotFoundError(row["owner_user_id"])
    slug = row["slug"]
    token_id = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + _GIT_CREDENTIAL_TTL
    jwt_token = create_access_token(
        user_id=owner["id"],
        email=owner["email"],
        token_id=token_id,
        typ="pat",
        expires_delta=_GIT_CREDENTIAL_TTL,
        extra_claims={"scope": f"data-app-git:{slug}"},
    )
    access_token_repo().create(
        id=token_id,
        user_id=owner["id"],
        name=f"data-app-git:{slug}",
        token_hash=hashlib.sha256(jwt_token.encode()).hexdigest(),
        prefix=token_id.replace("-", "")[:8],
        expires_at=expires_at,
    )
    base = get_public_url() or AGNES_INTERNAL_URL
    return f"{base.replace('://', f'://agnes:{jwt_token}@')}/data-apps.git/{slug}"


def _handle_runner_failure(repo, app_id: str, exc: Exception) -> None:
    detail = getattr(exc, "detail", None) or str(exc)
    repo.set_state(app_id, "error", str(detail))


class OwnerNotFoundError(Exception):
    """Raised by :func:`redeploy_current` when ``app_row['owner_user_id']``
    no longer resolves to a live user row. Distinct from ``ValueError``
    (spec-build failures) so callers can tell "deploy target row is
    internally inconsistent" (500) apart from "spec inputs are invalid"
    (400) without string-matching the exception message."""


class DraftParentMissingError(Exception):
    """Raised by :func:`redeploy_current` when a draft row's
    ``parent_app_id`` no longer resolves to a live parent row (the parent
    app was deleted out from under a still-live draft). Without this
    check, ``redeploy_current`` would silently fall back to cloning the
    draft's own (nonexistent) repo slug and the deploy would appear to
    succeed. ``deploy_data_app`` maps this to HTTP 409 ``parent_not_found``."""


def _rollback_new_service_token(repo, app_id: str, new_token_id: str, previous_token_id: str) -> None:
    """Undo a tentatively-minted+stored service token after a deploy step
    following the mint fails (spec build or runner `up`).

    Revokes the just-minted token (it was never handed to a running
    container — no container ever saw it in its config.json — so it's
    pure dead weight if left live) and restores the row's
    `service_token_id` to whatever it was before this deploy attempt
    (`""` if the app had never deployed before). The previously-working
    token itself is never touched here — a failed redeploy must leave a
    still-sleeping-but-deployed app able to wake with its last-known-good
    credential.
    """
    try:
        access_token_repo().revoke(new_token_id)
    except Exception:
        logger.warning("failed to revoke rolled-back service token %s for data app %s", new_token_id, app_id)
    repo.update(app_id, service_token_id=previous_token_id)


def redeploy_current(row: dict) -> None:
    """Mint a fresh service token, build the runtime spec/config off
    ``row`` as it stands (i.e. whatever ``agnes-live`` currently points at
    — this function never touches the git ref itself), and hand it to the
    runner sidecar's ``up``.

    This is the shared mint -> config -> ``runner.up`` pipeline extracted
    from ``deploy_data_app``'s body (Task 7) so both the operator-triggered
    ``POST /{slug}/deploy`` (which fast-forwards ``agnes-live`` to a new sha
    *before* calling this) and the wake-on-request path (``_trigger_wake``
    in ``app/api/data_apps_proxy.py``, redeploying a sleeping
    ``sleep_mode="recreate"`` app at its last-deployed sha) go through
    exactly one implementation — no drift between the two call sites'
    mint/rollback semantics.

    Token mint/rollback semantics are preserved byte-for-byte from the
    original inline body: the new token is stored on the row TENTATIVELY,
    before it's known the runner call will succeed. On any failure below
    (`ValueError` from spec building, or `RunnerUnavailable`/`RunnerError`
    from the runner call) the tentative token is revoked and the row's
    `service_token_id` is restored to whatever it was before this call —
    never leaving a still-sleeping-but-deployed app without a working
    credential. The previous token is only revoked (this function's own
    side effect on success) once the runner has actually accepted the
    deploy.

    Raises `OwnerNotFoundError`, `DraftParentMissingError`, `ValueError`,
    `RunnerUnavailable`, or `RunnerError` on failure; each already left the
    row in "error" state (via `_handle_runner_failure`) for the runner-call
    case, or with an untouched state for the owner/parent/spec-build cases —
    callers decide how to surface that (HTTP response for `deploy_data_app`,
    `set_state("error", ...)` for `_trigger_wake`) without this function
    taking an opinion on HTTP status codes or wake-vs-deploy framing.
    """
    slug = row["slug"]
    repo = data_apps_repo()

    # A draft whose parent has since been deleted must fail loudly rather
    # than silently falling back (below) to cloning the draft's own
    # (nonexistent) repo slug — checked before any side effect (token mint)
    # so there's nothing to roll back on this path.
    if row.get("is_draft") and row.get("parent_app_id") and not repo.get(row["parent_app_id"]):
        raise DraftParentMissingError(row["parent_app_id"])

    owner = users_repo().get_by_id(row["owner_user_id"])
    if not owner:
        raise OwnerNotFoundError(row["owner_user_id"])

    previous_token_id = row.get("service_token_id") or ""
    new_token_id, jwt_token = _mint_service_token(slug, owner)
    repo.update(row["id"], service_token_id=new_token_id)
    row = repo.get(row["id"])  # refresh — carries the new (tentative) service_token_id

    secrets = _decrypt_secrets(row)
    # Drafts share the parent app's git repo (they never get one of their
    # own — see `create_draft`'s docstring), so the clone URL must point at
    # the PARENT's slug, not the draft's own. `build_config_json` already
    # selects the draft's pinned `draft_branch` off `row` via `is_draft`;
    # this only fixes which repo that branch is cloned from.
    repo_slug = slug
    if row.get("is_draft") and row.get("parent_app_id"):
        parent = repo.get(row["parent_app_id"])
        if parent:
            repo_slug = parent["slug"]
    clone_url = f"{AGNES_INTERNAL_URL}/data-apps.git/{repo_slug}"

    try:
        config_json = build_config_json(row, secrets=secrets, clone_url=clone_url, clone_token=jwt_token)
        spec = build_container_spec(row, defaults=_effective_config(), data_dir=os.environ.get("DATA_DIR", "/data"))
    except ValueError:
        _rollback_new_service_token(repo, row["id"], new_token_id, previous_token_id)
        raise

    try:
        _runner().up(slug, spec, config_json)
    except (RunnerUnavailable, RunnerError) as exc:
        _rollback_new_service_token(repo, row["id"], new_token_id, previous_token_id)
        _handle_runner_failure(repo, row["id"], exc)
        raise

    # The runner accepted the deploy — only now is it safe to revoke the
    # previous token. Had we revoked it eagerly (before the runner call),
    # any failure above would have left the app with NO working credential
    # at all, even though the previously-deployed container is still
    # running/sleeping and may need to wake using it.
    if previous_token_id:
        try:
            access_token_repo().revoke(previous_token_id)
        except Exception:
            logger.warning("failed to revoke previous service token %s for data app %s", previous_token_id, slug)


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
    mode: Optional[str] = None


class SecretsRequest(BaseModel):
    secrets: dict[str, str] = {}


class CreateDraftRequest(BaseModel):
    branch: str = "init"


def _draft_slug(parent_slug: str, branch: str) -> str:
    """Derive the draft's own slug from its parent + branch:
    ``<parent>--<branch>``, lowercased, non-``SLUG_RE`` characters folded to
    ``-``, truncated to 40 chars. Raises 400 ``invalid_slug`` if the result
    still fails ``SLUG_RE`` (e.g. an all-symbol branch name collapsing to
    nothing usable, or a leading/trailing ``-`` after truncation) — or if it
    collapses to the parent's own slug, which the 40-char truncation can do
    for near-max-length parent slugs (a 38-40 char parent leaves no room for
    ``--<branch>`` before truncation cuts it back down to just the parent).
    That case must not fall through to ``create_draft``'s UNIQUE constraint:
    it would surface as a misleading 409 ``slug_exists`` that has nothing to
    do with an actual name collision and is the same for every branch."""
    raw = f"{parent_slug}--{branch}".lower()
    cleaned = _re.sub(r"[^a-z0-9-]", "-", raw)[:40].strip("-")
    if not SLUG_RE.match(cleaned) or cleaned == parent_slug:
        raise HTTPException(status_code=400, detail="invalid_slug")
    return cleaned


@router.get("")
async def list_data_apps(user: dict = Depends(get_current_user)):
    _feature_gate()
    cfg = _effective_config()
    # Drafts are working copies layered on a parent app, not independent
    # apps a caller should stumble onto in the human-facing/CLI list —
    # they're reached via `GET /{slug}`'s inlined `drafts` field instead.
    rows = data_apps_repo().list(include_drafts=False)
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
    if payload.slug in RESERVED_SLUGS:
        raise HTTPException(status_code=400, detail="reserved_slug")
    if payload.repo_mode not in ("internal", "external"):
        raise HTTPException(status_code=400, detail="invalid_repo_mode")
    idle_timeout_s = _clamp_idle_timeout(payload.idle_timeout_s)

    cfg = _effective_config()
    is_admin = is_user_admin(user["id"])
    # Quota is admin-exempt, so the race this lease guards against (two
    # concurrent requests both observing count < max_apps_per_user) only
    # exists for non-admin callers — skip the lease entirely for Admin.
    lease_held = False
    lease_name = holder = ""
    if not is_admin:
        lease_held, lease_name, holder = _acquire_create_lease(user["id"])

    try:
        if not is_admin:
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
        kwargs["idle_timeout_s"] = idle_timeout_s if idle_timeout_s is not None else cfg["default_idle_timeout_s"]
        kwargs["sleep_mode"] = payload.sleep_mode if payload.sleep_mode is not None else cfg["default_sleep_mode"]

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
    finally:
        if lease_held:
            _release_create_lease(lease_name, holder)


@router.get("/{slug}")
async def get_data_app(slug: str, user: dict = Depends(get_current_user)):
    _feature_gate()
    row = _get_row_or_404(slug)
    if not _can_view(user, row):
        raise HTTPException(status_code=403, detail="forbidden")
    out = _serialize(row)
    # Drafts are hidden from the list endpoint (`include_drafts=False`);
    # this is where they surface instead — inlined on their PROD parent's
    # detail response. Empty for a draft's own detail (drafts don't have
    # drafts — `create_draft` rejects `parent_is_draft`).
    if not row.get("is_draft"):
        out["drafts"] = [
            {
                "id": d["id"],
                "slug": d["slug"],
                "branch": d["draft_branch"],
                "state": d["state"],
                "url": _app_url(d["slug"], _effective_config()),
            }
            for d in data_apps_repo().list_drafts(row["id"])
        ]
    return out


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

    holder = require_op_lease(slug)
    try:
        repo = data_apps_repo()
        if payload.mode == "dev":
            # Dev-mode deploys serve the draft's pinned `draft_branch` straight
            # from the parent's repo — `build_config_json` already selects it
            # (`is_draft`) and `redeploy_current` already clones from the
            # parent's slug for draft rows, so there is no ref to fast-forward
            # here at all (drafts never advance `agnes-live`).
            if not row.get("is_draft"):
                raise HTTPException(status_code=400, detail="dev_requires_draft")
            sha = ""
        elif row.get("is_draft"):
            # A draft has no `agnes-live` ref of its own to fast-forward to —
            # only `mode="dev"` deploys are valid for a draft row.
            raise HTTPException(status_code=400, detail="prod_on_draft")
        elif row["repo_mode"] == "external":
            # External repos have no internal bare repo (`init_app_repo` is
            # internal-only at create) — nothing for `fast_forward_live` to
            # fast-forward. The runtime clones HEAD of `repo_branch` at boot
            # (spec §2: external repos are HEAD-at-wake; sha pinning is future
            # work), so an explicit sha in the request can't be honored.
            if payload.sha:
                raise HTTPException(status_code=400, detail="external_repo_sha_unsupported")
            sha = ""
        else:
            try:
                sha = fast_forward_live(slug, payload.sha)
            except ValueError as exc:
                if "no commits to deploy" in str(exc):
                    raise HTTPException(status_code=409, detail="deploy_empty_repo")
                raise HTTPException(status_code=400, detail=str(exc))

        try:
            redeploy_current(row)
        except OwnerNotFoundError:
            raise HTTPException(status_code=500, detail="owner_not_found")
        except DraftParentMissingError:
            raise HTTPException(status_code=409, detail="parent_not_found")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except (RunnerUnavailable, RunnerError):
            raise HTTPException(status_code=502, detail="runner_unavailable")

        repo.record_deploy(row["id"], sha)
        repo.set_state(row["id"], "running")
        _audit(conn, user["id"], "data_app.deploy", f"data_app:{slug}", {"sha": sha})

        return {"state": "running", "deployed_sha": sha}
    finally:
        release_op_lease(slug, holder)


@router.post("/{slug}/git-credential")
async def mint_git_credential(
    slug: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _feature_gate()
    row = _get_row_or_404(slug)
    _require_owner_or_admin(user, row)
    try:
        url = _mint_git_credential(row)
    except OwnerNotFoundError:
        raise HTTPException(status_code=500, detail="owner_not_found")
    _audit(conn, user["id"], "data_app.git_credential", f"data_app:{slug}", {})
    return {"git_clone_url": url}


@router.post("/{slug}/drafts", status_code=201)
async def create_draft(
    slug: str,
    payload: CreateDraftRequest,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Create a draft copy of the prod app ``slug`` on ``payload.branch``.

    The draft row (Task 1's ``create_draft``) is a full ``data_apps`` row
    with ``is_draft=True`` and ``parent_app_id`` pointing at the prod app —
    it is deployed/stopped/deleted like any other app, just excluded from
    the default list (Task 6). ``ensure_branch`` (Task 2) creates the
    branch on the *prod* app's git repo (drafts don't get their own repo —
    they're a ref + a registry row layered on top of the parent's); the
    git credential handed back is likewise minted against the parent
    (``_mint_git_credential(parent)``), since that's the repo the branch —
    and thus any push — actually lives in.

    Ordering is deliberate: the registry row is inserted BEFORE the git
    branch is created. Doing it the other way round would leave an orphaned
    branch on the parent's repo whenever ``create_draft`` hits a slug
    collision (409 ``slug_exists``) — a git side effect with no
    corresponding row, invisible to any registry-driven cleanup. With the
    row created first, a slug collision has no git side effect at all; if
    ``ensure_branch`` then fails (409 ``parent_has_no_main``), the
    just-inserted row is deleted so a failed create-draft call never leaves
    a dangling draft behind either.
    """
    _feature_gate()
    parent = _get_row_or_404(slug)
    _require_owner_or_admin(user, parent)
    if parent.get("is_draft"):
        raise HTTPException(status_code=400, detail="parent_is_draft")
    if not _BRANCH_RE.match(payload.branch) or not _is_git_valid_branch(payload.branch):
        raise HTTPException(status_code=400, detail="invalid_branch")
    draft_slug = _draft_slug(slug, payload.branch)

    repo = data_apps_repo()
    try:
        draft_id = repo.create_draft(
            parent_app_id=parent["id"],
            slug=draft_slug,
            branch=payload.branch,
            owner_user_id=parent["owner_user_id"],
        )
    except duckdb.ConstraintException:
        raise HTTPException(status_code=409, detail="slug_exists")

    from src.data_apps.git_repos import ensure_branch

    try:
        ensure_branch(slug, payload.branch, base="main")
    except ValueError:
        repo.delete(draft_id)
        raise HTTPException(status_code=409, detail="parent_has_no_main")
    except subprocess.CalledProcessError:
        # Belt-and-braces: `_is_git_valid_branch` should already reject
        # everything `git update-ref` refuses, but if some other
        # git-invalid form ever slips past it, fail the same way (400
        # `invalid_branch`, draft row rolled back) rather than surfacing an
        # unhandled 500 and leaving an orphaned draft row that would turn a
        # retry into a misleading 409 `slug_exists`.
        repo.delete(draft_id)
        raise HTTPException(status_code=400, detail="invalid_branch")

    try:
        git_url = _mint_git_credential(parent)  # push credential is against the PROD repo
    except OwnerNotFoundError:
        raise HTTPException(status_code=500, detail="owner_not_found")

    _audit(
        conn,
        user["id"],
        "data_app.draft_create",
        f"data_app:{draft_slug}",
        {"parent": slug, "branch": payload.branch},
    )
    return {"id": draft_id, "slug": draft_slug, "branch": payload.branch, "git_clone_url": git_url}


def _teardown_draft(repo, parent_slug: str, draft: dict, conn: duckdb.DuckDBPyConnection, actor_id: str) -> None:
    """Shared draft-teardown body: best-effort container stop, service-token
    revoke, draft-branch delete on the PARENT repo, registry row delete,
    and config-dir cleanup. Used by both ``delete_draft`` (single draft) and
    ``delete_data_app``'s cascade (all drafts of a deleted parent) so the two
    call sites can't drift on what "delete a draft" actually tears down.
    """
    draft_slug = draft["slug"]
    try:
        _runner().stop(draft_slug, mode="recreate")
    except (RunnerUnavailable, RunnerError):
        logger.warning("_teardown_draft: runner stop failed for %s (continuing)", draft_slug)

    _revoke_service_token(draft)

    from src.data_apps.git_repos import delete_branch

    try:
        delete_branch(parent_slug, draft["draft_branch"])
    except ValueError:
        pass

    repo.delete(draft["id"])
    _rmtree_config_dir(draft_slug)

    _audit(
        conn,
        actor_id,
        "data_app.draft_delete",
        f"data_app:{draft_slug}",
        {"parent": parent_slug},
    )


@router.delete("/{slug}/drafts/{draft_slug}", status_code=204)
async def delete_draft(
    slug: str,
    draft_slug: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Tear down a single draft: owner/Admin of the PARENT app; 400
    ``not_a_draft`` if ``draft_slug`` isn't a draft of ``slug`` (including
    ``draft_slug == slug`` itself); 404 if ``draft_slug`` doesn't exist at
    all. Mirrors ``delete_data_app``'s teardown via the shared
    ``_teardown_draft`` helper — container stop, token revoke, branch
    delete, row delete, config-dir cleanup.
    """
    _feature_gate()
    parent = _get_row_or_404(slug)
    _require_owner_or_admin(user, parent)

    repo = data_apps_repo()
    draft = repo.get_by_slug(draft_slug)
    if draft is None:
        raise HTTPException(status_code=404, detail="data_app_not_found")
    if not draft.get("is_draft") or draft.get("parent_app_id") != parent["id"]:
        raise HTTPException(status_code=400, detail="not_a_draft")

    _teardown_draft(repo, slug, draft, conn, user["id"])


@router.post("/{slug}/stop")
async def stop_data_app(
    slug: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    _feature_gate()
    row = _get_row_or_404(slug)
    _require_owner_or_admin(user, row)

    holder = require_op_lease(slug)
    try:
        repo = data_apps_repo()
        try:
            _runner().stop(slug, mode="recreate")
        except (RunnerUnavailable, RunnerError) as exc:
            _handle_runner_failure(repo, row["id"], exc)
            raise HTTPException(status_code=502, detail="runner_unavailable")

        repo.set_state(row["id"], "stopped")
        # Spec §8/§10: an explicit stop revokes the service token — unlike
        # reap-idle's sleep transition (see reap_idle_data_apps), which leaves
        # it live so the app can wake later. A stop is an operator decision that
        # the app isn't coming back on its own; the credential goes with it.
        _revoke_service_token(row)
        repo.update(row["id"], service_token_id="")
        _audit(conn, user["id"], "data_app.stop", f"data_app:{slug}")
        return {"state": "stopped"}
    finally:
        release_op_lease(slug, holder)


@router.delete("/{slug}", status_code=204)
async def delete_data_app(
    slug: str,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Runner stop + service-token revoke + registry row delete.

    204 No Content, no body (project convention — see
    ``tests/test_api_design_rules.py::test_delete_returns_204``). The git
    repo directory under ``${DATA_DIR}/apps/git/<slug>.git`` is
    intentionally left on disk — deletion is a registry-only operation;
    that fact is recorded in the audit log params, not the response body.
    The RUNTIME config dir (``${DATA_DIR}/apps/<slug>``, holding the
    ``config.json`` apps-runner wrote — see ``_resolve_host_path`` in
    ``services/apps_runner/api.py``) is different: it carries the
    now-revoked service JWT in plaintext, so it's removed best-effort as
    hygiene rather than kept like the git repo.

    Cascades to any live drafts: a prod app's drafts share its git repo and
    reference its id as ``parent_app_id``, so deleting the parent without
    tearing them down first would leave orphaned draft rows/branches/
    containers behind. Each draft is torn down via the same
    ``_teardown_draft`` helper ``delete_draft`` uses, BEFORE this app's own
    teardown. (Drafts can't themselves have drafts — ``create_draft``
    rejects ``parent_is_draft`` — so this is a no-op when ``row`` is itself
    a draft.)
    """
    _feature_gate()
    row = _get_row_or_404(slug)
    _require_owner_or_admin(user, row)

    repo = data_apps_repo()
    # Drafts live on the parent's repo and have their own containers/branches;
    # tear them down first so deleting a prod app can't strand them.
    for draft in repo.list_drafts(row["id"]):
        _teardown_draft(repo, slug, draft, conn, user["id"])

    holder = require_op_lease(slug)
    try:
        # Best-effort: a dead runner must not block deleting the registry row
        # (there'd otherwise be no way to remove an app whose container host is
        # gone).
        try:
            _runner().stop(slug, mode="recreate")
        except (RunnerUnavailable, RunnerError):
            logger.warning("delete_data_app: runner stop failed for %s (continuing)", slug)

        _revoke_service_token(row)
        repo.delete(row["id"])

        _rmtree_config_dir(slug)

        _audit(
            conn,
            user["id"],
            "data_app.delete",
            f"data_app:{slug}",
            {"repo_dir_left_on_disk": True},
        )
    finally:
        release_op_lease(slug, holder)


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
    """Runner-backed readiness probe. Doubles as the wake-completion flip:
    when a `deploying` app's runner reports `ready`, this call itself
    transitions the row to `running` — the ingress proxy's holding page
    (`app/api/data_apps_proxy.py``, ``data_app_waking.html``'s poll loop)
    is the only caller that hits this endpoint on a cadence, so the flip
    happening here (rather than a dedicated poller) is what actually
    surfaces "the app is up" back to the browser tab that triggered the
    wake. See that module's docstring for the other half of this contract.
    """
    _feature_gate()
    row = _get_row_or_404(slug)
    if not _can_view(user, row):
        raise HTTPException(status_code=403, detail="forbidden")

    state = row["state"]
    ready = False
    if state in ("running", "deploying"):
        try:
            status = _runner().status(slug)
            ready = bool(status.get("ready"))
        except (RunnerUnavailable, RunnerError):
            ready = False

    if state == "deploying" and ready:
        data_apps_repo().set_state(row["id"], "running")
        state = "running"

    return {"state": state, "ready": ready}


@router.post("/reap-idle")
async def reap_idle_data_apps(
    user: dict = Depends(require_admin),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Admin-only idle sweep (called by the scheduler, whose synthetic user
    is Admin). `idle_timeout_s` is per-app, so each `running` row is
    compared, in Python, against its *own* configured threshold rather than
    one shared value — a single `repo.list(state="running", ...)` scan
    (`list_idle` stays on the repo for callers that want SQL-side filtering
    against one shared threshold — e.g. any future admin/ops tooling — and
    remains contract-tested, but reap-idle itself no longer calls it per row).

    A runner failure on one app is recorded (state -> "error",
    state_detail carries the runner's message) and does not abort the rest
    of the sweep — one dead container must not wedge every other reap.

    Also checks `deploying` rows stuck longer than `_DEPLOY_STALE_TIMEOUT_S`
    (a wake or operator-deploy that never finished — e.g. the ingress
    proxy's backgrounded `_spawn_wake` task died without anything left to
    observe it, or a `POST .../deploy` request process crashed mid-flight)
    against the runner before declaring the app dead: if the runner reports
    the container is actually up and ready, the row is recovered to
    `running` (reported as `recovered`) rather than errored out from under
    a deploy that in fact succeeded; only when the runner says otherwise
    (or can't be reached) is the row flipped to `error` (reported as
    `timed_out`).
    """
    _feature_gate()
    repo = data_apps_repo()
    reaped: list[str] = []
    now = datetime.now(timezone.utc)
    for row in repo.list(state="running", limit=100000):
        last_request_at = row.get("last_request_at")
        if last_request_at is None:
            continue
        if last_request_at.tzinfo is None:
            last_request_at = last_request_at.replace(tzinfo=timezone.utc)
        if (now - last_request_at).total_seconds() <= row["idle_timeout_s"]:
            continue
        # Non-blocking: a row with a deploy/stop/wake already in flight is
        # left running and picked up on the next scheduler tick rather than
        # having this sweep block or race the in-flight operation's own
        # runner.stop()/up() call (see the op-lease invariant above).
        acquired, holder = try_acquire_op_lease(row["slug"])
        if not acquired:
            logger.info("reap-idle: skipping %s — another operation is in flight", row["slug"])
            continue
        try:
            _runner().stop(row["slug"], mode=row.get("sleep_mode") or "recreate")
        except (RunnerUnavailable, RunnerError) as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            repo.set_state(row["id"], "error", f"reap-idle stop failed: {detail}")
            logger.warning("reap-idle: runner stop failed for %s: %s", row["slug"], detail)
            continue
        finally:
            # The state write must happen while still holding the lease —
            # releasing first (as a bare `finally: release_op_lease(...)`
            # would) opens a window where a concurrent deploy/wake grabs the
            # freed lease, starts a container, and then this sweep's
            # "sleeping" write lands after it and clobbers that state.
            repo.set_state(row["id"], "sleeping")
            reaped.append(row["slug"])
            release_op_lease(row["slug"], holder)

    recovered: list[str] = []
    timed_out: list[str] = []
    for row in repo.list(state="deploying"):
        updated_at = row.get("updated_at")
        if updated_at is None:
            continue
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        if (now - updated_at).total_seconds() <= _DEPLOY_STALE_TIMEOUT_S:
            continue
        ready = False
        try:
            status = _runner().status(row["slug"])
            ready = bool(status.get("ready"))
        except (RunnerUnavailable, RunnerError) as exc:
            logger.warning("reap-idle: status check failed for %s: %s", row["slug"], exc)
        if ready:
            repo.set_state(row["id"], "running")
            logger.info("reap-idle: %s was actually ready; recovered to running", row["slug"])
            recovered.append(row["slug"])
            continue
        repo.set_state(row["id"], "error", "wake/deploy timed out")
        logger.warning("reap-idle: %s stuck in deploying past %ds; marked error", row["slug"], _DEPLOY_STALE_TIMEOUT_S)
        timed_out.append(row["slug"])

    _audit(
        conn,
        user["id"],
        "data_app.reap_idle",
        "data_app:*",
        {"reaped": reaped, "timed_out": timed_out, "recovered": recovered},
    )
    return {"reaped": reaped, "timed_out": timed_out, "recovered": recovered}
