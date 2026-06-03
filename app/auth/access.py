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
from app.resource_types import ResourceType
from src.db import SYSTEM_ADMIN_GROUP

logger = logging.getLogger(__name__)


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
        logger.warning(
            "is_user_admin: Admin group missing in user_groups; denying access"
        )
        return False
    return admin_id in _user_group_ids(user_id, conn=conn)


def can_access(
    user_id: str,
    resource_type: str,
    resource_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> bool:
    """Generic access check. Admin short-circuits; otherwise group JOIN.

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
        return True

    if not group_ids:
        return False

    from src.repositories import use_pg, resource_grants_repo
    if conn is not None and not use_pg():
        from src.repositories.resource_grants import ResourceGrantsRepository
        return ResourceGrantsRepository(conn).has_grant(
            list(group_ids), resource_type, resource_id,
        )
    return resource_grants_repo().has_grant(
        list(group_ids), resource_type, resource_id,
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
            list(group_ids), resource_type,
        )
    else:
        rows = resource_grants_repo().list_for_groups(
            list(group_ids), resource_type,
        )
    return frozenset(r["resource_id"] for r in rows)


def has_explicit_grant(
    user_id: str,
    resource_type: str,
    resource_id: str,
    conn: duckdb.DuckDBPyConnection,
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
    """
    group_ids = _user_group_ids(user_id, conn)
    if not group_ids:
        return False
    placeholders = ",".join(["?"] * len(group_ids))
    row = conn.execute(
        f"""SELECT 1 FROM resource_grants
            WHERE group_id IN ({placeholders})
              AND resource_type = ?
              AND resource_id = ?
            LIMIT 1""",
        [*group_ids, resource_type, resource_id],
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


async def require_admin(
    user: dict = Depends(get_current_user),
    conn: duckdb.DuckDBPyConnection = Depends(_get_db),
) -> dict:
    """Dependency: require user is in the Admin group. Raises 403 otherwise.

    Replaces the v9 ``require_role(Role.ADMIN)`` and
    ``require_internal_role("core.admin")`` thin wrappers. Same calling
    convention as before — endpoints write ``Depends(require_admin)`` (no
    parens) and receive the user dict.
    """
    if not is_user_admin(user["id"], conn):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
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

    async def dep(
        request: Request,
        user: dict = Depends(get_current_user),
        conn: duckdb.DuckDBPyConnection = Depends(_get_db),
    ) -> dict:
        try:
            resource_id = path_template.format(**request.path_params)
        except KeyError as e:
            # Path template references a param the route doesn't expose —
            # programmer error, fail loud.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    f"require_resource_access: path_template "
                    f"{path_template!r} references missing path_param {e}"
                ),
            )
        if not can_access(user["id"], resource_type.value, resource_id, conn):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Access denied to {resource_type.value} "
                    f"{resource_id!r}"
                ),
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
    from src.db import get_system_db
    from src.repositories.users import UserRepository

    conn = get_system_db()
    try:
        row = UserRepository(conn).get_by_email(user_email)
    finally:
        try:
            conn.close()
        except Exception:
            pass
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
