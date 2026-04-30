"""Table-access checks — thin wrappers over ``app.auth.access.can_access``.

Module exists for legacy import paths (``app/api/data.py``, ``app/api/sync.py``,
``app/api/catalog.py``, ``app/api/v2_*``, ``app/api/query.py``) that already
import ``can_access_table`` / ``get_accessible_tables`` from here. The
authorization itself flows through ``app.auth.access`` — this file is a
shim mapping the table-grain helpers onto the generic resource_grants check.
"""

from typing import Optional

import duckdb

from src.db import get_system_db


def can_access_table(
    user: dict,
    table_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> bool:
    """True iff the user can read ``table_id``.

    Admin short-circuit (members of the Admin system group) plus
    per-(group, table) grants in ``resource_grants``. Nothing else — no
    ``is_public`` bypass, no per-user permissions table, no bucket
    wildcards. Every non-admin access requires an explicit
    ``resource_grants(group, "table", table_id)`` row.
    """
    user_id = user.get("id")
    if not user_id:
        return False

    should_close = False
    if conn is None:
        conn = get_system_db()
        should_close = True
    try:
        from app.auth.access import can_access
        from app.resource_types import ResourceType
        return can_access(user_id, ResourceType.TABLE.value, table_id, conn)
    finally:
        if should_close:
            conn.close()


def get_accessible_tables(
    user: dict,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> Optional[list[str]]:
    """List of table IDs the user can read. ``None`` means "all" (admin)."""
    user_id = user.get("id")
    if not user_id:
        return []

    should_close = False
    if conn is None:
        conn = get_system_db()
        should_close = True
    try:
        from app.auth.access import is_user_admin
        if is_user_admin(user_id, conn):
            return None  # admin sees everything

        # Non-admin: list every table_id with a matching grant via any group
        # the user belongs to. Single SQL — no Python-side filtering loop.
        rows = conn.execute(
            """SELECT DISTINCT rg.resource_id
               FROM resource_grants rg
               JOIN user_group_members m ON m.group_id = rg.group_id
               WHERE m.user_id = ? AND rg.resource_type = 'table'""",
            [user_id],
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        if should_close:
            conn.close()
