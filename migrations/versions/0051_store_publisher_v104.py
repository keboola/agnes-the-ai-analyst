"""store_entities publisher_kind + verification columns

Mirrors DuckDB ``_v103_to_v104``. Two orthogonal axes for the catalog card's
trust line:

- ``publisher_kind`` ('organization' | 'user') — who stands behind the item.
  Stored, never derived from the owner's current Admin-group membership, since
  group membership is mutable and re-synced from the identity provider: a
  derived value would silently reclassify already-published skills when an
  author leaves the Admin group.
- ``verification_state`` (+ ``verified_at`` / ``verified_by`` /
  ``verification_note``) — the org's advisory review of a USER-published item.
  Never a read gate; only a chip and a filter value.

Backfill: every pre-v104 row is a user upload, and 'none' is the state that
renders no chip, so the defaults make the upgrade visually inert.

Unlike DuckDB, PG enforces the CHECK constraints — they are the real guard
behind the repository-layer validation.

Revision ID: 0051_store_publisher_v104
Revises: 0050_agents_v103
Create Date: 2026-07-29

Note on the revision id: ``alembic_version.version_num`` is
``VARCHAR(32)``, so a descriptive-but-long id (e.g.
``0051_store_entity_publisher_verification_v104``, 45 chars) fails at
stamp time with ``StringDataRightTruncation`` — and because stamping happens
in test fixture setup, it takes out every Postgres test, not just this one.
Keep new ids short.

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0051_store_publisher_v104"
down_revision: Union[str, None] = "0050_agents_v103"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_COLUMNS = (
    "publisher_kind",
    "verification_state",
    "verified_at",
    "verified_by",
    "verification_note",
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "store_entities" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("store_entities")}

    if "publisher_kind" not in existing:
        op.add_column(
            "store_entities",
            sa.Column(
                "publisher_kind",
                sa.String(),
                nullable=False,
                server_default="user",
            ),
        )
        op.create_check_constraint(
            "ck_store_entities_publisher_kind",
            "store_entities",
            "publisher_kind IN ('organization', 'user')",
        )
    if "verification_state" not in existing:
        op.add_column(
            "store_entities",
            sa.Column(
                "verification_state",
                sa.String(),
                nullable=False,
                server_default="none",
            ),
        )
        op.create_check_constraint(
            "ck_store_entities_verification_state",
            "store_entities",
            "verification_state IN ('none', 'requested', 'verified', 'changes_requested')",
        )
    if "verified_at" not in existing:
        op.add_column(
            "store_entities",
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "verified_by" not in existing:
        op.add_column("store_entities", sa.Column("verified_by", sa.String(), nullable=True))
    if "verification_note" not in existing:
        op.add_column("store_entities", sa.Column("verification_note", sa.Text(), nullable=True))

    # The publisher facet ("Organization" / "Me" / "Other users") is the
    # listing's primary provenance filter, so it is on the hot path of every
    # browse query.
    indexes = {ix["name"] for ix in insp.get_indexes("store_entities")}
    if "idx_store_entities_publisher_kind" not in indexes:
        op.create_index(
            "idx_store_entities_publisher_kind",
            "store_entities",
            ["publisher_kind"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "store_entities" not in insp.get_table_names():
        return
    indexes = {ix["name"] for ix in insp.get_indexes("store_entities")}
    if "idx_store_entities_publisher_kind" in indexes:
        op.drop_index("idx_store_entities_publisher_kind", table_name="store_entities")
    existing = {c["name"] for c in insp.get_columns("store_entities")}
    constraints = {c["name"] for c in insp.get_check_constraints("store_entities")}
    if "ck_store_entities_verification_state" in constraints:
        op.drop_constraint("ck_store_entities_verification_state", "store_entities", type_="check")
    if "ck_store_entities_publisher_kind" in constraints:
        op.drop_constraint("ck_store_entities_publisher_kind", "store_entities", type_="check")
    for col in _COLUMNS:
        if col in existing:
            op.drop_column("store_entities", col)
