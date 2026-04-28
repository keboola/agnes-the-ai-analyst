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
from typing import Optional

import duckdb
from fastapi import Depends, HTTPException, Request, status

from app.auth.dependencies import _get_db, get_current_user
from app.resource_types import ResourceType
from src.db import SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP

logger = logging.getLogger(__name__)


def _get_group_id_by_name(name: str, conn: duckdb.DuckDBPyConnection) -> Optional[str]:
    """Look up a group's id by its (unique) name. Returns None if absent —
    typically only happens during the very first migration pass before
    _seed_system_groups has run, or in mis-seeded test fixtures."""
    row = conn.execute(
        "SELECT id FROM user_groups WHERE name = ?", [name]
    ).fetchone()
    return row[0] if row else None


def _user_group_ids(user_id: str, conn: duckdb.DuckDBPyConnection) -> set[str]:
    """Set of group_ids the user is in.

    Returns only real ``user_group_members`` rows — there is no implicit
    Everyone. Membership in the Everyone group now comes from being a
    member of ``<AGNES_GOOGLE_GROUP_PREFIX>everyone@`` in Google Workspace
    (via ``source='google_sync'``) or from explicit admin assignment.
    Email-only users with no admin-assigned membership see zero groups,
    which means zero resource grants — correct fail-closed default.
    """
    rows = conn.execute(
        "SELECT group_id FROM user_group_members WHERE user_id = ?",
        [user_id],
    ).fetchall()
    return {r[0] for r in rows}


def is_user_admin(user_id: str, conn: duckdb.DuckDBPyConnection) -> bool:
    """True iff the user is a member of the Admin system group.

    Cheap — one SELECT EXISTS-style check (the inner _user_group_ids does
    one fetchall + a name lookup; both are tiny, both indexed).
    """
    admin_id = _get_group_id_by_name(SYSTEM_ADMIN_GROUP, conn)
    if admin_id is None:
        # No Admin group seeded — defensively deny. Fail-closed beats the
        # alternative of silently granting elevated access.
        logger.warning(
            "is_user_admin: Admin group missing in user_groups; denying access"
        )
        return False
    return admin_id in _user_group_ids(user_id, conn)


def can_access(
    user_id: str,
    resource_type: str,
    resource_id: str,
    conn: duckdb.DuckDBPyConnection,
) -> bool:
    """Generic access check. Admin short-circuits; otherwise group JOIN.

    Two SELECTs in the worst case:
      1. _user_group_ids — fetch group membership.
      2. has_grant on resource_grants for (group_ids, resource_type, resource_id).
    """
    group_ids = _user_group_ids(user_id, conn)
    admin_id = _get_group_id_by_name(SYSTEM_ADMIN_GROUP, conn)
    if admin_id is not None and admin_id in group_ids:
        return True

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
