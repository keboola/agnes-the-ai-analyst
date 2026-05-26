#!/usr/bin/env python3
"""Idempotent seed for the e2e smoke test user.

Creates ``e2e@example.com`` (Admin group member) with a hardcoded
dev-only password. The user exists ONLY in dev/CI containers -- the
container is the privilege boundary; see
docs/superpowers/specs/2026-05-25-smoke-login-flow-design.md.

Usage:
    python scripts/seed_e2e_user.py

Exits 0 on success (whether the user was newly created or already
existed), 1 if the system Admin group is missing (DB in half-init
state -- refuses to create an orphan user).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

E2E_USER_EMAIL = "e2e@example.com"
E2E_USER_NAME = "E2E Smoke Test"
E2E_USER_ID = "e2e-smoke-user"
E2E_USER_PASSWORD = "E2eSmokePass!"


def seed() -> None:
    """Idempotent. SystemExit(1) on missing Admin group or missing opt-in env."""
    # Defence-in-depth: the seed module ships in the production image via
    # `COPY . .` in the Dockerfile. Without this env-gate, anyone with
    # `docker exec` on a production container could mint an Admin user
    # with the committed password. The CI workflow sets AGNES_E2E_SEED=1
    # explicitly; production images run without it.
    if os.environ.get("AGNES_E2E_SEED") != "1":
        print(
            "error: refusing to seed -- set AGNES_E2E_SEED=1 to opt in. "
            "This script is intended for CI/local-dev e2e smoke setup only; "
            "see docs/superpowers/specs/2026-05-25-smoke-login-flow-design.md.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    from src.db import SYSTEM_ADMIN_GROUP
    from src.repositories import (
        user_group_members_repo,
        user_groups_repo,
        users_repo,
    )

    # Post-PG cutover the seed talks to repositories through the
    # factory; no per-call ``get_system_db()`` handle. Each repo holds
    # the SA engine singleton internally.
    groups = user_groups_repo()
    admin_row = groups.get_by_name(SYSTEM_ADMIN_GROUP)
    if not admin_row:
        print(
            f"error: {SYSTEM_ADMIN_GROUP!r} group not seeded -- refusing to "
            "create orphan e2e user. Run ``alembic upgrade head`` so the "
            "bootstrap seeds the system groups, then re-run this script.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    admin_gid = admin_row["id"]

    users = users_repo()
    memberships = user_group_members_repo()
    existing = users.get_by_email(E2E_USER_EMAIL)
    now = datetime.now(timezone.utc)

    # Single PasswordHasher instance — argon2-cffi reuses the same
    # defaults (time_cost / memory_cost / parallelism) across calls,
    # so the hasher is effectively stateless. Matches the pattern in
    # app/auth/providers/password.py.
    hasher = PasswordHasher()

    if existing is None:
        password_hash = hasher.hash(E2E_USER_PASSWORD)
        users.create(
            id=E2E_USER_ID,
            email=E2E_USER_EMAIL,
            name=E2E_USER_NAME,
            password_hash=password_hash,
        )
        user_id = E2E_USER_ID
    else:
        user_id = existing["id"]
        # Verify the stored hash. Skip the UPDATE on the common case
        # (hash already matches) to avoid a needless ~100 ms re-hash +
        # DB write. Only catch argon2 verifier failures — any other
        # exception (lock timeout, disk I/O, library version mismatch
        # surfacing as a non-argon2 error) propagates so a real bug
        # surfaces instead of silently re-hashing on every subsequent
        # run.
        try:
            hasher.verify(existing["password_hash"], E2E_USER_PASSWORD)
        except VerifyMismatchError:
            password_hash = hasher.hash(E2E_USER_PASSWORD)
            users.update(id=user_id, password_hash=password_hash, updated_at=now)

    # Re-assert Admin membership. add_member is idempotent on
    # (user_id, group_id) per the repository contract. ``added_by``
    # tags the row so the cleanup query that excludes
    # ``app.main:seed_admin`` can apply the same rule to scripts.* seeds.
    memberships.add_member(
        user_id, admin_gid, source="system_seed", added_by="scripts.seed_e2e_user"
    )

    print(f"seeded: {E2E_USER_EMAIL} (id={user_id}) in Admin group")


if __name__ == "__main__":
    seed()
