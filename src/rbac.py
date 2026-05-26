"""Table-access checks — thin wrappers over ``app.auth.access.can_access``.

Module exists for legacy import paths (``app/api/data.py``, ``app/api/sync.py``,
``app/api/catalog.py``, ``app/api/v2_*``, ``app/api/query.py``) that already
import ``can_access_table`` / ``get_accessible_tables`` from here. The
authorization itself flows through ``app.auth.access`` — this file is a
shim mapping the table-grain helpers onto the generic resource_grants check.
"""

from typing import Optional


def can_access_table(user: dict, table_id: str, conn=None) -> bool:
    """True iff the user can read ``table_id``.

    Admin short-circuit (members of the Admin system group) plus
    per-(group, table) grants in ``resource_grants``. Nothing else — no
    ``is_public`` bypass, no per-user permissions table, no bucket
    wildcards. Every non-admin access requires an explicit
    ``resource_grants(group, "table", table_id)`` row.

    ``conn`` is accepted for backward-compat with old callers and ignored.
    """
    user_id = user.get("id")
    if not user_id:
        return False

    # Internal data-source tables (agnes_sessions / agnes_usage / agnes_audit)
    # are implicitly accessible to every authenticated user — RBAC there is
    # row-level (the per-request view filters to the caller's rows) rather
    # than table-level. Admin gets the unscoped view; non-admin gets their
    # own rows. Both paths are gated downstream; the table-grain check just
    # needs to wave them through.
    from connectors.internal.access import is_internal_table
    if is_internal_table(table_id):
        return True

    from app.auth.access import can_access
    from app.resource_types import ResourceType
    return can_access(user_id, ResourceType.TABLE.value, table_id)


def get_accessible_tables(user: dict, conn=None) -> Optional[list[str]]:
    """List of table IDs the user can read. ``None`` means "all" (admin).

    ``conn`` is accepted for backward-compat with old callers and ignored.
    """
    user_id = user.get("id")
    if not user_id:
        return []

    from app.auth.access import is_user_admin
    if is_user_admin(user_id):
        return None  # admin sees everything

    # Non-admin: list every table_id with a matching grant via any group
    # the user belongs to.
    # Internal tables are auto-granted (see can_access_table) — they're
    # always in every authenticated user's accessible set even without
    # a resource_grants row.
    from src.repositories import resource_grants_repo, user_group_members_repo

    group_ids = user_group_members_repo().list_groups_for_user(user_id)
    if not group_ids:
        result: list[str] = []
    else:
        rows = resource_grants_repo().list_for_groups(
            list(group_ids), resource_type="table",
        )
        seen: set[str] = set()
        result = []
        for r in rows:
            rid = str(r.get("resource_id") or "")
            if rid and rid not in seen:
                seen.add(rid)
                result.append(rid)

    from connectors.internal.access import INTERNAL_TABLES
    for t in INTERNAL_TABLES:
        if t.registry_id not in result:
            result.append(t.registry_id)
    return result
