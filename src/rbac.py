"""Dataset access checks — orthogonal to the v12 admin/group RBAC model.

The user_groups + user_group_members + resource_grants triple in
``app.auth.access`` covers app-level (Admin) and resource-level
(``ResourceType``) authorization. Dataset access is a separate axis (rows
in ``dataset_permissions`` keyed by dataset / wildcard bucket); we keep the
legacy helpers here so admin-bypass + per-table checks Just Work without
plumbing them into the resource-grants model.

This module is what ``app/api/sync.py`` and ``app/api/catalog.py`` call when
they need to filter the visible table list for a non-admin user.
"""

from typing import Optional

import duckdb

from src.db import get_system_db


def _is_admin_user_dict(user: dict, conn: Optional[duckdb.DuckDBPyConnection] = None) -> bool:
    """True iff the user is in the Admin system group.

    Wraps ``app.auth.access.is_user_admin`` with the open-on-demand DB hop
    that the table-access helpers expect (they accept ``conn=None`` and
    open one if missing). Imported lazily so importing this module from
    test fixtures doesn't pull the FastAPI deps tree.
    """
    user_id = user.get("id")
    if not user_id:
        return False
    from app.auth.access import is_user_admin
    if conn is not None:
        return is_user_admin(user_id, conn)
    own_conn = get_system_db()
    try:
        return is_user_admin(user_id, own_conn)
    finally:
        own_conn.close()


def has_dataset_access(email: str, dataset: str) -> bool:
    """Check if user has access to a specific dataset.

    Admins (Admin user_group) have access to all datasets.
    Other users need explicit permission in dataset_permissions table.
    """
    from src.repositories.users import UserRepository
    from src.repositories.sync_settings import DatasetPermissionRepository

    conn = get_system_db()
    try:
        user = UserRepository(conn).get_by_email(email)
        if not user:
            return False
        if _is_admin_user_dict(user, conn=conn):
            return True
        return DatasetPermissionRepository(conn).has_access(user["id"], dataset)
    finally:
        conn.close()


def can_access_table(user: dict, table_id: str, conn: Optional[duckdb.DuckDBPyConnection] = None) -> bool:
    """Check if user can access a specific table.

    Rules:
    1. Admin -> always True
    2. Table is_public=True -> always True
    3. Explicit permission in dataset_permissions -> True
    4. Wildcard bucket permission (e.g., 'in.c-finance.*') -> True
    5. Otherwise -> False
    """
    should_close = False
    if conn is None:
        conn = get_system_db()
        should_close = True

    try:
        if _is_admin_user_dict(user, conn=conn):
            return True

        from src.repositories.table_registry import TableRegistryRepository
        from src.repositories.sync_settings import DatasetPermissionRepository

        # Check if table is public
        table = TableRegistryRepository(conn).get(table_id)
        if table and table.get("is_public", True):
            return True

        user_id = user.get("id", "")
        perm_repo = DatasetPermissionRepository(conn)

        # Check explicit permission
        if perm_repo.has_access(user_id, table_id):
            return True

        # Check wildcard bucket permission
        bucket = table.get("bucket", "") if table else ""
        if bucket and perm_repo.has_access(user_id, f"{bucket}.*"):
            return True

        return False
    finally:
        if should_close:
            conn.close()


def get_accessible_tables(user: dict, conn: Optional[duckdb.DuckDBPyConnection] = None) -> Optional[list[str]]:
    """List of table IDs the user can access. None means "all" — admin bypass."""
    should_close = False
    if conn is None:
        conn = get_system_db()
        should_close = True

    try:
        if _is_admin_user_dict(user, conn=conn):
            return None  # None means "all" — admin bypass

        from src.repositories.table_registry import TableRegistryRepository
        repo = TableRegistryRepository(conn)
        all_tables = repo.list_all()

        accessible = []
        for t in all_tables:
            if can_access_table(user, t["id"], conn):
                accessible.append(t["id"])
        return accessible
    finally:
        if should_close:
            conn.close()
