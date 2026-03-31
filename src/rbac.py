"""Role-based access control — centralized permission checks using DuckDB.

Replaces Linux group-based auth (sudo/data-ops → admin, dataread → analyst).
Used by both FastAPI (app/auth/dependencies.py) and Flask webapp (webapp/auth.py).
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
