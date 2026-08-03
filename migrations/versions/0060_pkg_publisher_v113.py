"""data_packages.publisher_kind — stored trust axis, replacing derived `curated`

Mirrors DuckDB ``_v112_to_v113``. Adds ``publisher_kind`` ('user' |
'organization') to ``data_packages``, the same stored axis ``store_entities``
has carried since v104, so a package and a skill make the same trust claim the
same way and every surface can render one shared marker.

It replaces a badge that was DERIVED on each render from "is ``created_by``
currently in the Admin group". That is not a property of the package: an admin
leaving the Admin group silently un-curated everything they had created. It is
also the exact derivation ``store_entities.publisher_kind`` exists to avoid —
group membership is mutable and re-synced from the identity provider.

The backfill is the reason this is a migration and not just a column: it freezes
whatever the derivation said at upgrade time, so an existing admin-created
package keeps reading as Organization instead of silently dropping to Community.
It is done in SQL here (rather than through the repositories, as the DuckDB
sibling does) because Alembic runs with only a bind — and it covers both shapes
of ``created_by`` seen in the wild, a user id on some rows and an email on
others.

Revision ID: 0060_pkg_publisher_v113
Revises: 0059_chat_pinned_v112
Create Date: 2026-08-03

Note on the revision id: ``alembic_version.version_num`` is ``VARCHAR(32)``,
so keep new ids short (see 0051_store_publisher_v104's note).

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0060_pkg_publisher_v113"
down_revision: Union[str, None] = "0059_chat_pinned_v112"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    if "data_packages" not in tables:
        return
    existing = {c["name"] for c in insp.get_columns("data_packages")}
    if "publisher_kind" not in existing:
        op.add_column(
            "data_packages",
            sa.Column("publisher_kind", sa.String(), nullable=True, server_default="user"),
        )
    # Rows that predate the column read NULL, not the server default (which
    # applies to inserts only) — normalize before promoting.
    op.execute("UPDATE data_packages SET publisher_kind = 'user' WHERE publisher_kind IS NULL")

    # Backfill: promote packages whose creator is an Admin-group member right
    # now. Requires the RBAC tables; on an instance without them every row
    # correctly stays 'user'.
    if not {"users", "user_groups", "user_group_members"} <= tables:
        return
    op.execute(
        """
        UPDATE data_packages AS dp
           SET publisher_kind = 'organization'
         WHERE dp.created_by IS NOT NULL
           AND EXISTS (
                 SELECT 1
                   FROM users u
                   JOIN user_group_members m ON m.user_id = u.id
                   JOIN user_groups g        ON g.id = m.group_id
                  WHERE g.name = 'Admin'
                    AND (u.id = dp.created_by OR u.email = dp.created_by)
               )
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "data_packages" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("data_packages")}
    if "publisher_kind" in existing:
        op.drop_column("data_packages", "publisher_kind")
