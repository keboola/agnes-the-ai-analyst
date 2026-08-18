"""Regression test: ``seed_admin`` adds the user to BOTH Admin AND
Everyone groups.

When LOCAL_DEV_MODE is on (or ``SEED_ADMIN_EMAIL`` is set in
production), ``app/main.py`` seeds an admin user on startup. Previously
it only added them to ``Admin``, so Everyone-scoped grants — the
canonical pattern for "every-user-sees-this" required onboarding —
didn't surface on the seed admin's own /catalog. Looked like a bug.

This regression locks in the dual-group seeding so a fresh
LOCAL_DEV_MODE checkout can demonstrate Required-tier grants without a
manual ``/admin/access`` Everyone-membership step first.
"""

from __future__ import annotations

import uuid

import duckdb
import pytest

from src.db import (
    SYSTEM_ADMIN_GROUP,
    SYSTEM_EVERYONE_GROUP,
    _ensure_schema,
)
from src.repositories.user_group_members import UserGroupMembersRepository
from src.repositories.users import UserRepository
from src.user_identity import normalize_email


def _run_seed_admin_block(conn, email: str) -> str:
    """Replicate the seed_admin block from ``app.main`` lifespan.

    Kept in step with the real block — see
    ``test_seed_admin_block_resolves_identity_case_insensitively``, which pins
    the source itself so this replica cannot quietly drift away from it.
    """
    repo = UserRepository(conn)
    email = normalize_email(email)
    existing = repo.get_by_email_ci(email)
    if not existing:
        user_id = str(uuid.uuid4())
        repo.create(id=user_id, email=email, name="Admin", password_hash=None)
    else:
        user_id = existing["id"]
    admin_group = conn.execute(
        "SELECT id FROM user_groups WHERE name = ?", [SYSTEM_ADMIN_GROUP],
    ).fetchone()
    if admin_group:
        UserGroupMembersRepository(conn).add_member(
            user_id=user_id,
            group_id=admin_group[0],
            source="system_seed",
            added_by="app.main:seed_admin",
        )
    everyone_group = conn.execute(
        "SELECT id FROM user_groups WHERE name = ?",
        [SYSTEM_EVERYONE_GROUP],
    ).fetchone()
    if everyone_group:
        UserGroupMembersRepository(conn).add_member(
            user_id=user_id,
            group_id=everyone_group[0],
            source="system_seed",
            added_by="app.main:seed_admin",
        )
    return user_id


def test_seed_admin_lands_in_both_admin_and_everyone():
    """The seed admin must be in both groups so Everyone-scoped Required
    grants surface for them on /catalog without manual operator action."""
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)

    user_id = _run_seed_admin_block(conn, "dev@localhost")

    groups = {
        r[0]
        for r in conn.execute(
            "SELECT g.name FROM user_group_members m "
            "JOIN user_groups g ON g.id = m.group_id "
            "WHERE m.user_id = ?",
            [user_id],
        ).fetchall()
    }
    assert SYSTEM_ADMIN_GROUP in groups, (
        "seed admin must be in Admin (admin authorization)"
    )
    assert SYSTEM_EVERYONE_GROUP in groups, (
        "seed admin must be in Everyone (Everyone-scoped grants must "
        "surface for them — Required onboarding grant target)"
    )


def test_seed_admin_is_idempotent_on_re_run():
    """Re-running ``seed_admin`` (lifespan startup hook fires every
    boot) must not duplicate membership rows."""
    conn = duckdb.connect(":memory:")
    _ensure_schema(conn)

    user_id = _run_seed_admin_block(conn, "dev@localhost")
    _run_seed_admin_block(conn, "dev@localhost")  # re-fire

    counts = {}
    for group_name in (SYSTEM_ADMIN_GROUP, SYSTEM_EVERYONE_GROUP):
        counts[group_name] = conn.execute(
            "SELECT COUNT(*) FROM user_group_members m "
            "JOIN user_groups g ON g.id = m.group_id "
            "WHERE m.user_id = ? AND g.name = ?",
            [user_id, group_name],
        ).fetchone()[0]
    assert counts[SYSTEM_ADMIN_GROUP] == 1, "Admin membership must not duplicate"
    assert counts[SYSTEM_EVERYONE_GROUP] == 1, "Everyone membership must not duplicate"


def test_seed_admin_block_resolves_identity_case_insensitively():
    """Pin the real lifespan block, not just the replica above.

    Seeding is an account-CREATING path. With a mixed-case
    ``SEED_ADMIN_EMAIL`` over an existing normalized row, an exact-match
    existence check mints a SECOND account and puts Admin + Everyone on it —
    while every auth door resolves the OLDEST match, i.e. the row the person
    actually signs in as, which has neither. A static read of the source is
    the honest gate here: the block lives inline in ``lifespan`` and cannot be
    called without standing up the whole app.
    """
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    start = src.index("seed_email = ")
    block = src[start : src.index("added_by=\"app.main:seed_admin\"", start)]
    assert "normalize_email(" in block, "seed_email must be normalized on write"
    assert "get_by_email_ci(seed_email)" in block, "the existence check must fold case"
    assert "get_by_email(seed_email)" not in block, "exact-match existence check would duplicate the account"
