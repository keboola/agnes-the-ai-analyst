"""Role-based access control — centralized permission checks using DuckDB.

Replaces Linux group-based auth (sudo/data-ops → admin, dataread → analyst).
Used by FastAPI (app/auth/dependencies.py).
"""

from enum import Enum
from typing import Optional

from src.db import get_system_db
from src.repositories.users import UserRepository


class Role(str, Enum):
    VIEWER = "viewer"
    ANALYST = "analyst"
    KM_ADMIN = "km_admin"
    ADMIN = "admin"


ROLE_HIERARCHY = {
    Role.VIEWER: 0,
    Role.ANALYST: 1,
    Role.KM_ADMIN: 2,
    Role.ADMIN: 3,
}


def get_user_role(email: str) -> Role:
    """Get role for a user by email. Returns VIEWER if not found."""
    conn = get_system_db()
    try:
        repo = UserRepository(conn)
        user = repo.get_by_email(email)
        if user:
            try:
                return Role(user.get("role", "viewer"))
            except ValueError:
                return Role.VIEWER
        return Role.VIEWER
    finally:
        conn.close()


def has_role(email: str, minimum_role: Role) -> bool:
    """Check if user has at least the given role level."""
    user_role = get_user_role(email)
    return ROLE_HIERARCHY.get(user_role, 0) >= ROLE_HIERARCHY.get(minimum_role, 0)


def is_admin(email: str) -> bool:
    """Check if user is an admin."""
    return has_role(email, Role.ADMIN)


def is_km_admin(email: str) -> bool:
    """Check if user is a KM admin or higher."""
    return has_role(email, Role.KM_ADMIN)


def is_analyst(email: str) -> bool:
    """Check if user is an analyst or higher."""
    return has_role(email, Role.ANALYST)


def has_dataset_access(email: str, dataset: str) -> bool:
    """Check if user has access to a specific dataset.

    Admins have access to all datasets.
    Other users need explicit permission in dataset_permissions table.
    """
    if is_admin(email):
        return True

    conn = get_system_db()
    try:
        user = UserRepository(conn).get_by_email(email)
        if not user:
            return False
        from src.repositories.sync_settings import DatasetPermissionRepository
        return DatasetPermissionRepository(conn).has_access(user["id"], dataset)
    finally:
        conn.close()


def can_access_table(user: dict, table_id: str, conn=None) -> bool:
    """Check if user can access a specific table.

    Rules:
    1. Admin -> always True
    2. Table is_public=True -> always True
    3. Explicit permission in dataset_permissions -> True
    4. Wildcard bucket permission (e.g., 'in.c-finance.*') -> True
    5. Otherwise -> False
    """
    if user.get("role") == "admin":
        return True

    should_close = False
    if conn is None:
        conn = get_system_db()
        should_close = True

    try:
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


def get_accessible_tables(user: dict, conn=None) -> list[str]:
    """Get list of table IDs the user can access. Used for filtering."""
    if user.get("role") == "admin":
        return None  # None means "all" — admin bypass

    should_close = False
    if conn is None:
        conn = get_system_db()
        should_close = True

    try:
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


def set_user_role(email: str, role: Role) -> bool:
    """Set role for a user. Returns True if successful."""
    conn = get_system_db()
    try:
        repo = UserRepository(conn)
        user = repo.get_by_email(email)
        if not user:
            return False
        repo.update(user["id"], role=role.value)
        return True
    finally:
        conn.close()
