"""Auth endpoints — login, token generation, bootstrap."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import duckdb
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.auth.jwt import create_access_token
from app.auth.access import is_user_admin
from app.auth.dependencies import _get_db, get_current_user
from app.auth.rate_limit import limiter as _rate_limiter
from src.db import SYSTEM_ADMIN_GROUP

from src.repositories import (
    audit_repo,
    user_curated_subscriptions_repo,
    user_group_members_repo,
    user_groups_repo,
    users_repo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class TokenRequest(BaseModel):
    email: str
    password: str = ""


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    role: str


class BootstrapRequest(BaseModel):
    email: str
    name: str = ""
    password: str = ""


def _audit(user_id: str, action: str, result: str | None = None) -> None:
    """Fire-and-forget audit log entry. Swallows all errors."""
    try:
        from src.db import get_system_db

        audit_conn = get_system_db()
        audit_repo().log(
            user_id=user_id,
            action=action,
            resource="auth",
            result=result,
        )
        audit_conn.close()
    except Exception:
        pass  # Audit failure must not block auth


@router.post("/token", response_model=TokenResponse)
@_rate_limiter.limit("10/minute")
async def create_token(
    request: Request,
    body: TokenRequest,
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Issue a JWT token. Requires password authentication."""
    repo = users_repo()
    user = repo.get_by_email(body.email)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    if not bool(user.get("active", True)):
        _audit(user["id"], "login_failed", result="deactivated")
        raise HTTPException(status_code=401, detail="Account deactivated")

    # If user has password_hash, require and verify it
    if user.get("password_hash"):
        if not body.password:
            raise HTTPException(status_code=401, detail="Password required")
        try:
            ph = PasswordHasher()
            ph.verify(user["password_hash"], body.password)
        except VerifyMismatchError:
            _audit(user["id"], "login_failed", result="invalid_password")
            raise HTTPException(status_code=401, detail="Invalid password")
        except Exception:
            logger.exception("Unexpected error during password verification")
            raise HTTPException(status_code=500, detail="Internal server error")
    else:
        # No password set — must use their auth provider (Google OAuth, magic link)
        raise HTTPException(
            status_code=401,
            detail="This account uses external authentication. Please log in via your configured provider.",
        )

    role_label = "admin" if is_user_admin(user["id"], conn) else "user"
    token = create_access_token(
        user_id=user["id"],
        email=user["email"],
    )
    _audit(user["id"], "token_created")
    return TokenResponse(
        access_token=token,
        user_id=user["id"],
        email=user["email"],
        role=role_label,
    )


@router.post("/bootstrap", response_model=TokenResponse)
@_rate_limiter.limit("3/minute")
async def bootstrap(
    request: Request,
    body: BootstrapRequest,
):
    """Bootstrap the first admin account.

    Allowed when no user has a password_hash yet. This covers:
    (a) No users exist at all.
    (b) Only seed users (created by SEED_ADMIN_EMAIL at startup) exist, which
        have no password and cannot log in — bootstrap lets the operator
        activate them with a password.

    If a user with the given email already exists (e.g. as a seed), this
    endpoint sets its password_hash (or clears it, if no password was supplied —
    useful for OAuth-only flows) and promotes it to admin.

    Deactivates as soon as any user has a password_hash.
    """
    repo = users_repo()
    existing = repo.list_all()

    # Bootstrap is locked once anyone has a password set.
    users_with_password = [u for u in existing if u.get("password_hash")]
    if users_with_password:
        raise HTTPException(
            status_code=403,
            detail="Bootstrap disabled — a user with a password already exists. Use /auth/password/login.",
        )

    password_hash = PasswordHasher().hash(body.password) if body.password else None

    # If a matching user already exists (e.g. seed), update it; else create fresh.
    existing_user = next((u for u in existing if u.get("email") == body.email), None)
    if existing_user:
        user_id = existing_user["id"]
        repo.update(id=user_id, password_hash=password_hash)
        _audit(user_id, "bootstrap_activated_seed")
    else:
        user_id = str(uuid.uuid4())
        repo.create(
            id=user_id,
            email=body.email,
            name=body.name or body.email.split("@")[0],
            password_hash=password_hash,
        )
        # v39: bootstrap user is the very first user; on first install
        # there are no system plugins yet so the fanout is a noop. Wire
        # it anyway so the later bootstrap-of-rebuilt-instance path (rare
        # but supported) inherits the existing mandatory tier.
        try:
            user_curated_subscriptions_repo().fanout_system_for_user(user_id)
        except Exception:
            logger.exception(
                "system-plugin fanout failed for bootstrap user %s",
                body.email,
            )
        _audit(user_id, "bootstrap_completed")

    # Promote the bootstrap user to the Admin system group — replaces the v9
    # ``user_role_grants`` write that the old bootstrap path relied on. Look the
    # group up through the factory so we get the ACTIVE backend's id: a raw
    # _get_db (always-DuckDB) read returned the DuckDB Admin-group id, and the
    # membership written to Postgres then referenced an id absent from PG, so
    # the bootstrapped first admin had no admin access on a Postgres instance.
    admin_group = user_groups_repo().get_by_name(SYSTEM_ADMIN_GROUP)
    if admin_group:
        user_group_members_repo().add_member(
            user_id=user_id,
            group_id=admin_group["id"],
            source="system_seed",
            added_by="auth.bootstrap",
        )

    token = create_access_token(user_id=user_id, email=body.email)
    return TokenResponse(
        access_token=token,
        user_id=user_id,
        email=body.email,
        role="admin",
    )


class RefreshGroupsResponse(BaseModel):
    """Response shape for ``POST /auth/refresh-groups``.

    ``applied``: True iff the synced membership set was rewritten. False when
    ``soft_failed`` (transient Admin SDK failure / empty fetch — previous
    snapshot preserved) or ``denied`` (prefix filter excluded every fetched
    group — caller has no eligible group on this instance).

    ``added``/``removed``: diff of synced (``source='google_sync'``) rows
    versus the snapshot before the call. Admin- and seed-sourced rows are
    untouched and excluded from the diff. Reported as group display names
    (Workspace email for synced rows, ``Admin``/``Everyone`` for mapped
    system rows).

    ``current``: every group name the caller is in **after** the refresh
    across all sources (admin, seed, google_sync). Useful for the CLI to
    show the user where their access stands.
    """

    applied: bool
    denied: bool = False
    soft_failed: bool = False
    fetched: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    current: list[str] = []


@router.post("/refresh-groups", response_model=RefreshGroupsResponse)
@_rate_limiter.limit("5/minute")
async def refresh_groups(
    request: Request,
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Re-sync the caller's Workspace group memberships against the Admin SDK.

    Hot path the OSS callback covers via the browser OAuth round-trip; this
    endpoint is the CLI / PAT counterpart so a user who's been added to a
    new Workspace group between their last browser sign-ins can refresh
    without re-logging in. Reuses the same write path as the OAuth
    callback (``app.auth.group_sync.apply_user_groups``), so policy
    (prefix filter, admin/everyone mapping, fail-soft on empty fetch)
    stays consistent.

    Returns the diff of synced rows + the post-refresh group set so the
    caller can see exactly what changed. Rate-limited at 5/min/IP — the
    slowapi default key is the request's remote IP, not the authenticated
    user, so a shared-NAT / VPN scenario divides the budget across users
    on the same egress. Matches the pattern of the other rate-limited
    endpoints in this router (``/token``, ``/bootstrap``). Refreshing is
    cheap on our side but each call costs a Workspace Admin SDK quota
    unit, so the limit guards the upstream quota.
    """
    from app.auth.group_sync import apply_user_groups

    # Read the membership graph through the repo factory so the diff
    # computation runs against the active backend — `user_group_members_repo()`
    # routes to Postgres when `use_pg()` is True, matching where
    # `apply_user_groups` writes (it uses the same factory internally).
    # The `conn` dependency is a DuckDB cursor for legacy callers, but it's
    # not what we want for the read-back here; using it would produce a
    # `before == after` (both empty/stale) and a lying response on PG.
    # See PR #520 Devin review for the original drift report.
    members_repo = user_group_members_repo()

    def _synced_names() -> set[str]:
        return {
            row["name"]
            for row in members_repo.list_groups_with_meta_for_user(user["id"])
            if row["source"] == "google_sync"
        }

    def _all_names() -> list[str]:
        return sorted(row["name"] for row in members_repo.list_groups_with_meta_for_user(user["id"]))

    before = _synced_names()
    result = apply_user_groups(user["id"], user["email"], conn)
    after = _synced_names() if result.applied else before

    added = sorted(after - before)
    removed = sorted(before - after)
    current = _all_names()

    _audit(
        user["id"],
        "auth.refresh_groups",
        result=("applied" if result.applied else "denied" if result.denied else "soft_failed"),
    )

    return RefreshGroupsResponse(
        applied=result.applied,
        denied=result.denied,
        soft_failed=result.soft_failed,
        fetched=result.fetched,
        added=added,
        removed=removed,
        current=current,
    )
