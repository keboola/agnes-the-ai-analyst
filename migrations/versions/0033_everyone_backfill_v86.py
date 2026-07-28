"""Backfill users missing an Everyone membership (DuckDB v86, issue #748)

PR #131 removed the implicit Everyone grant at user creation; every
creation call site now re-adds it going forward (see
``app.auth.group_sync.ensure_everyone_membership``), but this migration
covers users created in the interim on already-deployed instances.

Env-conditional like the v17->v18 stranded-membership cleanup: when
``AGNES_GROUP_EVERYONE_EMAIL`` is set, Everyone is Workspace-controlled
(memberships come exclusively from google_sync) and this backfill would
inject stray local rows — so it no-ops. On a fresh PG install, Alembic
runs before the app's first boot seeds the system groups, so there are
no users yet either way; the lookup below gracefully no-ops if the
Everyone group row is absent.

Revision ID: 0033_everyone_backfill_v86
Revises: 0032_vscode_mcp_client_v85
"""

import os

import sqlalchemy as sa
from alembic import op

revision: str = "0033_everyone_backfill_v86"
down_revision: str = "0032_vscode_mcp_client_v85"
branch_labels = None
depends_on = None

_BACKFILL_ADDED_BY = "system:v86-backfill"


def upgrade() -> None:
    if os.environ.get("AGNES_GROUP_EVERYONE_EMAIL", "").strip():
        return

    conn = op.get_bind()
    row = conn.execute(sa.text("SELECT id FROM user_groups WHERE name = 'Everyone' AND is_system")).fetchone()
    if row is None:
        # Fresh PG install: Alembic runs before the app's first boot seeds
        # the system groups, so there are no users to backfill anyway.
        return
    everyone_id = row[0]

    # bindparam(..., type_=String) pins the parameter's type explicitly —
    # without it, psycopg3 sees ":everyone_id" used once as a bare SELECT
    # literal and once compared against the (typed VARCHAR) group_id column
    # in the same statement and raises "AmbiguousParameter: inconsistent
    # types deduced for parameter".
    conn.execute(
        sa.text(
            """
            INSERT INTO user_group_members (user_id, group_id, source, added_by)
            SELECT u.id, :everyone_id, 'system_seed', :added_by
              FROM users u
             WHERE NOT EXISTS (
                 SELECT 1 FROM user_group_members m
                  WHERE m.user_id = u.id AND m.group_id = :everyone_id
             )
            """
        ).bindparams(sa.bindparam("everyone_id", type_=sa.String)),
        {"everyone_id": everyone_id, "added_by": _BACKFILL_ADDED_BY},
    )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("DELETE FROM user_group_members WHERE added_by = :added_by"),
        {"added_by": _BACKFILL_ADDED_BY},
    )
