"""Authorization helpers — group membership and resource grants.

Two layers of access control replace the v9 internal_roles / group_mappings
machinery:

1. **App-level access** is whether the user is in the ``Admin`` group. There
   is no hierarchy — ``Admin`` is god mode (short-circuits every grant
   check), every other group is just a label binding members to grants.

2. **Resource access** is whether any group the user is in holds a grant on
   ``(resource_type, resource_id)`` in ``resource_grants``. ``Admin`` group
   short-circuits this so admins never need explicit grants.

Two FastAPI dependencies cover the API surface:

  - ``require_admin`` — gates app-level mutations (admin UI, user mgmt,
    settings, …). 403 unless user is in Admin.
  - ``require_resource_access(resource_type, path_template)`` — gates
    entity-scoped endpoints. The path_template is a Python format string
    resolved against the request's path_params at call time — e.g.
    ``"{slug}/{plugin_name}"`` becomes the resource_id we look up.

The resolver is intentionally cache-less: every authorization check does one
or two DuckDB queries. DuckDB is in-process, so a per-request DB hit costs
sub-millisecond — the upstream session.internal_roles cache + dual-path
fallback solved a problem we don't have.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import duckdb
from fastapi import Depends, HTTPException, Request, status

from app.auth.dependencies import _get_db, get_current_user
from app.auth.session_principal import PRINCIPAL_TYPES, Principal
from app.resource_types import ResourceType
from src.db import SYSTEM_ADMIN_GROUP

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Google group self-heal: re-sync on access-miss
# ---------------------------------------------------------------------------
# Per-user last-resync timestamp (in-process cache). Guards against repeated
# Admin SDK calls on every denied request — one resync attempt per user per
# RESYNC_COOLDOWN_SECONDS window, regardless of outcome.
_google_resync_last: dict[str, float] = {}
_RESYNC_COOLDOWN_SECONDS = 60


def _maybe_resync_google_groups(user_id: str, email: str) -> bool:
    """Re-fetch Workspace groups for *user_id* if Google sync is configured
    and the per-user cooldown has passed.

    Returns True when a resync was attempted (caller should re-read groups),
    False when skipped (cooldown active, no Google config, or fetch error).

    Fail-soft: any exception is swallowed and logged; the existing membership
    snapshot is never cleared by this path.
    """
    if "GOOGLE_ADMIN_SDK_SUBJECT" not in os.environ and "GOOGLE_ADMIN_SDK_MOCK_GROUPS" not in os.environ:
        return False

    now = time.monotonic()
    if now - _google_resync_last.get(user_id, 0) < _RESYNC_COOLDOWN_SECONDS:
        return False

    _google_resync_last[user_id] = now
    try:
        from app.auth.group_sync import apply_user_groups

        # apply_user_groups ignores conn and routes through the repo factory,
        # so no raw connection is needed here (backend-safe on PG too).
        result = apply_user_groups(user_id, email, None)
        logger.info(
            "google-group self-heal: user=%s applied=%s groups=%s",
            user_id,
            result.applied,
            result.relevant,
        )
        return True
    except Exception:
        logger.warning("google-group self-heal failed for user %s", user_id, exc_info=True)
        return False


def _get_group_id_by_name(name: str, conn: Optional[duckdb.DuckDBPyConnection] = None) -> Optional[str]:
    """Look up a group's id by its (unique) name. Returns None if absent —
    typically only happens during the very first migration pass before
    _seed_system_groups has run, or in mis-seeded test fixtures.

    Honors ``conn`` only when the active backend is DuckDB and ``conn``
    is a DuckDB connection (test-isolation escape hatch for fixtures that
    seed into a per-test DuckDB). When the active backend is Postgres,
    ``conn`` is the local DuckDB view-handle which would be stale; we
    route through the global factory which reads from PG instead.
    """
    from src.repositories import use_pg, user_groups_repo

    if conn is not None and not use_pg():
        from src.repositories.user_groups import UserGroupsRepository

        row = UserGroupsRepository(conn).get_by_name(name)
    else:
        row = user_groups_repo().get_by_name(name)
    return row["id"] if row else None


def _user_group_ids(user_id: str, conn: Optional[duckdb.DuckDBPyConnection] = None) -> set[str]:
    """Set of group_ids the user is in.

    Returns only the rows present in ``user_group_members``. The implicit
    "every user is in Everyone" virtual row was removed when Google-prefix
    mapping landed — every membership is now sourced from a concrete row
    (``admin``, ``google_sync``, or ``system_seed``) so an operator
    auditing /admin/access sees the same set the authorization layer
    enforces. Callers that want Everyone-style "always granted" plugins
    must grant them to a real group the user is a member of.

    Honors ``conn`` only in DuckDB-backend mode (see ``_get_group_id_by_name``
    for rationale); routes through the global factory otherwise.
    """
    from src.repositories import use_pg, user_group_members_repo

    if conn is not None and not use_pg():
        from src.repositories.user_group_members import UserGroupMembersRepository

        return set(UserGroupMembersRepository(conn).list_groups_for_user(user_id))
    return set(user_group_members_repo().list_groups_for_user(user_id))


def is_user_admin(user_id: str, conn: Optional[duckdb.DuckDBPyConnection] = None) -> bool:
    """True iff the user is a member of the Admin system group.

    ``conn`` honored when explicitly passed (test isolation); falls back
    to the global factory otherwise.
    """
    admin_id = _get_group_id_by_name(SYSTEM_ADMIN_GROUP, conn=conn)
    if admin_id is None:
        # No Admin group seeded — defensively deny. Fail-closed beats the
        # alternative of silently granting elevated access.
        logger.warning("is_user_admin: Admin group missing in user_groups; denying access")
        return False
    return admin_id in _user_group_ids(user_id, conn=conn)


# ---------------------------------------------------------------------------
# God-mode observability
# ---------------------------------------------------------------------------
# When the Admin short-circuit in ``can_access`` grants a resource the admin
# holds no explicit group grant for, emit one deduplicated log line. Pure
# observability — never changes the decision — but it is the data that shows
# which surfaces actually rely on god-mode before any future narrowing.
# Best-effort in-process dedup (same pattern as ``_google_resync_last``); a
# benign race at worst duplicates a line. The grant lookup runs at most once
# per (user, resource) per cooldown window, so the auth hot path stays cheap.
_god_mode_logged: dict[str, float] = {}
_GOD_MODE_LOG_COOLDOWN_SECONDS = 900
_GOD_MODE_CACHE_MAX = 4096


def _note_god_mode_hit(
    user_id: str,
    resource_type: str,
    resource_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> None:
    # The ENTIRE body is guarded: observability must never break
    # authorization. This runs on FastAPI's thread pool (require_* deps are
    # plain ``def``), so the lock-free dedup cache can race — e.g. the
    # eviction sweep iterating while another thread inserts raises
    # RuntimeError — and any such failure must degrade to a lost log line,
    # never to an exception out of ``can_access``.
    try:
        key = f"{user_id}|{resource_type}|{resource_id}"
        now = time.time()
        last = _god_mode_logged.get(key)
        if last is not None and (now - last) < _GOD_MODE_LOG_COOLDOWN_SECONDS:
            return
        if len(_god_mode_logged) >= _GOD_MODE_CACHE_MAX:
            cutoff = now - _GOD_MODE_LOG_COOLDOWN_SECONDS
            for k in [k for k, t in list(_god_mode_logged.items()) if t < cutoff]:
                _god_mode_logged.pop(k, None)
            if len(_god_mode_logged) >= _GOD_MODE_CACHE_MAX:
                # pathological churn: reset rather than grow without bound
                _god_mode_logged.clear()
        _god_mode_logged[key] = now
        explicit = resource_id in _allowed_ids_for_user(user_id, resource_type, conn=conn)
        if not explicit:
            logger.info(
                "god_mode_bypass: admin %s accessed %s:%s with no explicit group grant",
                user_id,
                resource_type,
                resource_id,
            )
    except Exception:
        logger.warning(
            "god_mode_bypass: observability failed for %s %s:%s",
            user_id,
            resource_type,
            resource_id,
            exc_info=True,
        )


def can_access(
    user_id: str,
    resource_type: str,
    resource_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> bool:
    """Generic access check. Admin short-circuits; otherwise group JOIN.

    God-mode hits on resources the admin has no explicit grant for are
    logged (deduplicated) via :func:`_note_god_mode_hit` — observability
    only, the decision is unchanged.

    Internal data-source tables (``agnes_sessions``/``_usage``/``_audit``) are
    implicitly granted to every authenticated user. Security there is
    row-level (the per-request view filters to the caller's rows) and
    enforced in the query path; the table-grain gate just waves them
    through so they appear in /catalog and /api/v2/catalog for analysts,
    not just admins.

    ``conn`` honored when explicitly passed (test isolation); falls back
    to the global factory otherwise.
    """
    if resource_type == "table":
        from connectors.internal.access import is_internal_table

        if is_internal_table(resource_id):
            return True

    group_ids = _user_group_ids(user_id, conn=conn)
    admin_id = _get_group_id_by_name(SYSTEM_ADMIN_GROUP, conn=conn)
    if admin_id is not None and admin_id in group_ids:
        from app.auth.elevation import elevation_paused

        if not elevation_paused():
            _note_god_mode_hit(user_id, resource_type, resource_id, conn=conn)
            return True
        # Elevation paused (consent gate): fall through to the explicit
        # group-grant path — the admin sees exactly what their grants say.

    if not group_ids:
        return False

    from src.repositories import use_pg, resource_grants_repo

    if conn is not None and not use_pg():
        from src.repositories.resource_grants import ResourceGrantsRepository

        return ResourceGrantsRepository(conn).has_grant(
            list(group_ids),
            resource_type,
            resource_id,
        )
    return resource_grants_repo().has_grant(
        list(group_ids),
        resource_type,
        resource_id,
    )


def _allowed_ids_for_user(
    user_id: str,
    resource_type: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> frozenset[str]:
    """Set of resource_ids the user is granted for ``resource_type``.

    Deliberately does NOT apply the Admin god-mode short-circuit and does
    NOT add internal-table implicit grants — it reports only what was
    explicitly granted to a group the user belongs to. This is the single
    no-short-circuit grant primitive that both ``can_access`` (union/admin
    path) and ``compute_grant_intersection`` build on, so an admin-leak
    cannot reappear by drift.

    Routes through the repository factory (same split as ``can_access``) so
    DuckDB and Postgres behave identically — never raw SQL on ``conn``.
    """
    group_ids = _user_group_ids(user_id, conn=conn)
    if not group_ids:
        return frozenset()
    from src.repositories import use_pg, resource_grants_repo

    if conn is not None and not use_pg():
        from src.repositories.resource_grants import ResourceGrantsRepository

        rows = ResourceGrantsRepository(conn).list_for_groups(
            list(group_ids),
            resource_type,
        )
    else:
        rows = resource_grants_repo().list_for_groups(
            list(group_ids),
            resource_type,
        )
    return frozenset(r["resource_id"] for r in rows)


def has_explicit_grant(
    user_id: str,
    resource_type: str,
    resource_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> bool:
    """True iff one of the user's groups holds an explicit ``resource_grant``
    for ``(resource_type, resource_id)``.

    Unlike :func:`can_access`, this does **not** short-circuit for the Admin
    god-mode group and does **not** apply internal-table implicit grants — it
    reports only what was explicitly granted to a group the user belongs to.

    Use it for UI affordances that should reflect actual rollout state rather
    than *effective* access: e.g. hiding the cloud-chat nav link until chat is
    granted to a group, even for admins (who can still reach the page by URL,
    since the route guard uses :func:`can_access` and admins keep god-mode
    there). Never use it as a security gate — that is :func:`can_access`'s job.

    ``conn`` honored only in DuckDB-backend mode (test isolation); routes
    through the global factory otherwise — same backend-split rule as
    :func:`can_access`. (Previously this ran a raw ``conn.execute`` against
    ``resource_grants``, which read the stale/empty DuckDB table on a
    Postgres-backed instance and hid the nav link even when chat was granted.)
    """
    group_ids = _user_group_ids(user_id, conn=conn)
    if not group_ids:
        return False
    from src.repositories import use_pg, resource_grants_repo

    if conn is not None and not use_pg():
        from src.repositories.resource_grants import ResourceGrantsRepository

        return ResourceGrantsRepository(conn).has_grant(
            list(group_ids),
            resource_type,
            resource_id,
        )
    return resource_grants_repo().has_grant(
        list(group_ids),
        resource_type,
        resource_id,
    )


def can_access_session(
    principal: "Principal",
    resource_type: str,
    resource_id: str,
) -> bool:
    """Restricted-principal access: membership in the live intersection.

    Must NOT call is_user_admin / can_access (PR checklist item) — both a
    ``SessionPrincipal``'s and an ``AgentPrincipal``'s ``intersection`` were
    already built without the admin short-circuit, so consulting either here
    would re-introduce god-mode through the back door."""
    return resource_id in principal.intersection.get(resource_type, frozenset())


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def require_admin(
    user=Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
):
    """Dependency: require user is in the Admin group. Raises 403 otherwise.

    Replaces the v9 ``require_role(Role.ADMIN)`` and
    ``require_internal_role("core.admin")`` thin wrappers. Same calling
    convention as before — endpoints write ``Depends(require_admin)`` (no
    parens) and receive the user dict.

    Any restricted principal (``SessionPrincipal`` co-session runner token,
    ``AgentPrincipal`` agent-session sandbox token) is HARD-DENIED before any
    ``is_user_admin`` check. This ordering is load-bearing: an
    ``AgentPrincipal`` carries its owner's user id, so a lookup that ran
    first would return True for an admin-owned agent and hand the sandbox
    god-mode. An agent is a *restriction* of its owner, never an elevation.

    Plain ``def`` (not ``async def``) so FastAPI offloads it to the anyio
    thread pool — the body is a sync ``is_user_admin`` RBAC read that must
    not run on the event loop (Tier 1, PR #188).
    """
    if isinstance(user, PRINCIPAL_TYPES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    if not is_user_admin(user["id"], conn):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    from app.auth.elevation import elevation_paused

    if elevation_paused():
        # Consent gate: the caller IS an admin but has paused their own
        # elevation for this browser. Distinct detail so clients can offer
        # a "re-enable admin mode" action instead of a generic 403.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin_elevation_paused",
        )
    return user


def require_resource_access(
    resource_type: ResourceType,
    path_template: str,
):
    """Dependency factory: require access to ``resource_type`` at the path
    derived from ``path_template`` formatted with the request's path_params.

    Example::

        @router.get("/marketplace/{slug}/plugins/{name}/install")
        async def install_plugin(
            slug: str, name: str,
            user = Depends(require_resource_access(
                ResourceType.MARKETPLACE_PLUGIN, "{slug}/{name}",
            )),
        ): ...

    Admin short-circuits — admins never need explicit grants. Non-admins
    raise 403 with the resolved path in the detail so the client knows what
    they failed against.
    """

    # Plain ``def`` (not ``async def``) so FastAPI offloads the returned
    # dependency to the anyio thread pool — its body is a sync RBAC read
    # (``can_access`` / ``can_access_session``) that must not run on the
    # event loop (Tier 1, PR #188).
    def dep(
        request: Request,
        user=Depends(get_current_user),
        conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    ):
        try:
            resource_id = path_template.format(**request.path_params)
        except KeyError as e:
            # Path template references a param the route doesn't expose —
            # programmer error, fail loud.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(f"require_resource_access: path_template {path_template!r} references missing path_param {e}"),
            )
        if isinstance(user, PRINCIPAL_TYPES):
            # Restricted principal (co-session or agent-session): the live
            # intersection is the sole authority — no admin short-circuit,
            # no owner-group resolution, and no ``user["id"]`` to subscript.
            allowed = can_access_session(user, resource_type.value, resource_id)
        else:
            allowed = can_access(user["id"], resource_type.value, resource_id, conn)
            if not allowed and _maybe_resync_google_groups(user["id"], user.get("email", "")):
                # Groups were refreshed — re-check with the updated snapshot.
                allowed = can_access(user["id"], resource_type.value, resource_id, conn)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(f"Access denied to {resource_type.value} {resource_id!r}"),
            )
        return user

    return dep


def mint_session_jwt(user_email: str, chat_id: str, *, ttl_seconds: int = 3600) -> str:
    """Mint a short-lived service JWT scoped to one chat session.

    Used by ChatManager._spawn_runner to inject AGNES_TOKEN into the
    subprocess env. The token is verified by the existing get_current_user
    dependency (app/auth/pat_resolver.py calls UserRepository.get_by_id on
    the ``sub`` claim), so ``sub`` MUST be the user's UUID — not the email.

    Secret is read from the ``JWT_SECRET_KEY`` environment variable —
    the same key used by the rest of the auth layer (see app/auth/jwt.py).
    """
    import jwt  # PyJWT — already a project dependency
    from src.repositories import users_repo

    # Factory-routed: honors use_pg() so a Postgres instance reads the live
    # PG users table, not the frozen DuckDB system file (#518).
    row = users_repo().get_by_email(user_email)
    if not row:
        raise ValueError(f"mint_session_jwt: user not found: {user_email!r}")
    user_id = row["id"]

    now = int(time.time())
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + ttl_seconds,
        "scope": "chat",
        "chat_session_id": chat_id,
        "email": user_email,
    }
    secret = os.environ.get(
        "JWT_SECRET_KEY",
        "test-jwt-secret-key-minimum-32-chars!!",
    )
    return jwt.encode(payload, secret, algorithm="HS256")


def mint_co_session_jwt(session_id: str, *, ttl: int = 3600) -> str:
    """Mint a co-session runner token. Carries ONLY chat_session_id +
    typ='co_session' + a synthetic sub (never a user UUID). No participant
    email list is baked in (SR-4) — the resolver reads chat_session_participants
    live as the sole source of truth, eliminating the stale-grant replay window.

    Encoded with the canonical auth secret (app/auth/jwt) so verify_token
    decodes it in every env.
    """
    from datetime import timedelta
    from app.auth.jwt import create_access_token

    return create_access_token(
        user_id=f"session:{session_id}",
        email="",  # no real identity; resolver never reads this
        expires_delta=timedelta(seconds=ttl),
        typ="co_session",
        # scope="chat" triggers the per-session BigQuery budget stash
        # (`_stash_chat_session_id_from_token`), same as the solo path — a
        # co-session is a chat session and must be capped too. (#849)
        extra_claims={"scope": "chat", "chat_session_id": session_id},
    )


def mint_agent_session_jwt(session_id: str, *, ttl: int = 3600) -> str:
    """Mint an agent-scoped session runner token (V1d). Carries ONLY
    chat_session_id + typ='agent_session' + a synthetic sub (never a user
    UUID or the agent_id) — the same no-baked-in-authority contract as
    ``mint_co_session_jwt``: no grants, no real user id, no agent identity.

    The resolver (``app.auth.pat_resolver``) rebuilds the owner-grants ∩
    agent-scope intersection live per request
    (``src.agent_scope_intersection.compute_agent_intersection``), so
    narrowing an agent or revoking a grant takes effect on the very next
    request — no stale-replay window.

    Encoded with the canonical auth secret (app/auth/jwt) so verify_token
    decodes it in every env.
    """
    from datetime import timedelta
    from app.auth.jwt import create_access_token

    return create_access_token(
        user_id=f"agent-session:{session_id}",
        email="",  # no real identity; resolver never reads this
        expires_delta=timedelta(seconds=ttl),
        typ="agent_session",
        # scope="chat" triggers the per-session BigQuery budget stash
        # (`_stash_chat_session_id_from_token`) — a brokered agent session
        # must stay capped too, same as the co-session and solo paths.
        extra_claims={"scope": "chat", "chat_session_id": session_id},
    )
