"""Role-based access control — centralized permission checks using DuckDB.

v9 redesign: legacy `users.role` enum (viewer/analyst/km_admin/admin) is
backed by `user_role_grants` rows pointing at the seeded core.* internal
roles. `has_role(...)` and friends now delegate to `resolve_internal_roles`
so direct grants AND group-mapping grants both satisfy the hierarchy.

Used by FastAPI (`app/auth/dependencies.py`) and a handful of business-logic
callsites (table access checks). Module-author capabilities live in
`app.auth.role_resolver` and require_internal_role — those are the path
forward; the helpers here exist to keep the legacy `Role` API ergonomic
for code that just wants "is this user at least an admin?".
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


def _get_internal_role_keys(user_id: str, conn=None) -> list[str]:
    """v9: load expanded internal-role keys for a user via the resolver.

    Returns the union of direct grants (user_role_grants) and group-mapped
    grants, expanded along the implies hierarchy. Empty list when user has
    no role assignments. Caller may pass an existing connection to avoid
    the per-call get_system_db() open/close cycle on hot paths.
    """
    from app.auth.role_resolver import resolve_internal_roles
    should_close = False
    if conn is None:
        conn = get_system_db()
        should_close = True
    try:
        return resolve_internal_roles([], conn, user_id=user_id)
    finally:
        if should_close:
            conn.close()


def get_user_role(email: str) -> Role:
    """Return the highest legacy Role granted to ``email``.

    v9 deprecated shim: scans the user's expanded internal-role keys for
    core.* membership and returns the highest-level Role match. Falls back
    to ``VIEWER`` when the user is unknown or has no core.* grant —
    matching pre-v9 behavior where unset users.role defaulted to viewer.
    """
    conn = get_system_db()
    try:
        user = UserRepository(conn).get_by_email(email)
        if not user:
            return Role.VIEWER
        keys = _get_internal_role_keys(user["id"], conn=conn)
        for level in (Role.ADMIN, Role.KM_ADMIN, Role.ANALYST, Role.VIEWER):
            if f"core.{level.value}" in keys:
                return level
        return Role.VIEWER
    finally:
        conn.close()


def has_role(email: str, minimum_role: Role) -> bool:
    """Check if user has at least ``minimum_role``.

    v9: relies on the implies expansion done by resolve_internal_roles —
    holding ``core.admin`` directly already expands to core.km_admin etc.,
    so a single membership check covers the hierarchy. No per-level
    comparison needed.
    """
    conn = get_system_db()
    try:
        user = UserRepository(conn).get_by_email(email)
        if not user:
            return False
        keys = _get_internal_role_keys(user["id"], conn=conn)
        return f"core.{minimum_role.value}" in keys
    finally:
        conn.close()


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


def _is_admin_user_dict(user: dict, conn=None) -> bool:
    """v9 admin-shortcut for table-access helpers.

    Pre-v9 callers passed user dicts where ``user["role"] == "admin"`` was
    cheap to test inline. Post-v9 the dict no longer carries role, so we
    look up internal-role grants — but only when needed (table-access hot
    path), keeping the admin bypass cheap. Falls back to legacy lookup if
    the dict happens to still carry a role key (transitional safety).
    """
    if user.get("role") == "admin":
        return True
    user_id = user.get("id")
    if not user_id:
        return False
    return "core.admin" in _get_internal_role_keys(user_id, conn=conn)


def can_access_table(user: dict, table_id: str, conn=None) -> bool:
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


def get_accessible_tables(user: dict, conn=None) -> list[str]:
    """Get list of table IDs the user can access. Used for filtering."""
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


def set_user_role(email: str, role: Role) -> bool:
    """Set the legacy core.* role for a user via user_role_grants.

    v9: clears all existing core.* grants for the user and inserts a fresh
    one for the requested role. Module-role grants (e.g.
    corporate_memory.curator) are untouched — set_user_role only manages
    the core.* hierarchy. Returns False when the user is unknown.
    """
    import uuid
    conn = get_system_db()
    try:
        user_repo = UserRepository(conn)
        user = user_repo.get_by_email(email)
        if not user:
            return False

        # Clear existing core.* grants (any of viewer/analyst/km_admin/admin).
        conn.execute(
            """DELETE FROM user_role_grants
               WHERE user_id = ?
               AND internal_role_id IN (
                   SELECT id FROM internal_roles WHERE is_core = true
               )""",
            [user["id"]],
        )

        # Insert the new core.* grant.
        target_key = f"core.{role.value}"
        target_role = conn.execute(
            "SELECT id FROM internal_roles WHERE key = ?", [target_key],
        ).fetchone()
        if not target_role:
            return False
        conn.execute(
            """INSERT INTO user_role_grants
               (id, user_id, internal_role_id, granted_by, source)
               VALUES (?, ?, ?, 'set_user_role', 'direct')""",
            [str(uuid.uuid4()), user["id"], target_role[0]],
        )
        return True
    finally:
        conn.close()
