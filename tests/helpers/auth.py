"""v12 admin auth helper for tests.

In v12 the legacy ``users.role`` column is just a NULL artifact. Admin
status is determined by membership in the ``Admin`` system user_group via
``user_group_members``. Tests that create users with ``role='admin'``
through ``UserRepository.create`` must additionally place them into the
Admin group for ``require_admin`` (and the dataset bypass) to pass.

Use :func:`grant_admin` right after creating an admin user in your fixture.
"""

from __future__ import annotations


def grant_admin(conn, user_id: str) -> None:
    """Mark ``user_id`` as admin by inserting a ``user_group_members`` row
    pointing at the seeded Admin system group.

    Idempotent: ``UserGroupMembersRepository.add_member`` upserts.
    """
    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories.user_group_members import UserGroupMembersRepository

    row = conn.execute(
        "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP]
    ).fetchone()
    if not row:
        raise RuntimeError(
            f"Admin system group {SYSTEM_ADMIN_GROUP!r} not seeded — "
            "DB schema may be uninitialised."
        )
    UserGroupMembersRepository(conn).add_member(
        user_id, row[0], source="system_seed",
    )
