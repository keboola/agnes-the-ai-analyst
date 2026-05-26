"""Allow ``visibility_status = 'reconstructed'`` on store_entities.

Prod DuckDB has rows with ``visibility_status='reconstructed'`` —
operator state captured when a Store bundle is rebuilt from a
manifest after a deletion (recovered metadata, not a fresh submission).
The PG ``ck_store_entities_visibility`` constraint defined in 0009 only
allowed ``pending | approved | hidden | archived``, so importing prod
state failed on the check constraint. Add ``'reconstructed'`` to the
list so PG matches the (laxer) DuckDB shape — and so the seed/import
path doesn't drop these rows.

Revision: 0011
Revises:  0010_knowledge
"""
from alembic import op
import sqlalchemy as sa


revision = "0011_reconstructed_vis"
down_revision = "0010_knowledge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_store_entities_visibility",
        "store_entities",
        type_="check",
    )
    op.create_check_constraint(
        "ck_store_entities_visibility",
        "store_entities",
        "visibility_status IN ('pending','approved','hidden','archived','reconstructed')",
    )


def downgrade() -> None:
    # Revert to the v0009 four-value form. ``reconstructed`` rows must
    # be deleted or relabelled by the operator before the downgrade — we
    # don't auto-coerce, since the deletion target is operator policy.
    op.drop_constraint(
        "ck_store_entities_visibility",
        "store_entities",
        type_="check",
    )
    op.create_check_constraint(
        "ck_store_entities_visibility",
        "store_entities",
        "visibility_status IN ('pending','approved','hidden','archived')",
    )
