"""corpus_files.parent_file_id — bundle (zip) child linkage (DuckDB v88, K1)

Children extracted from an uploaded archive point at the archive's own
corpus_files row; directly-uploaded files (and archive rows themselves)
keep NULL.

Revision ID: 0035_parent_file_id_v88
Revises: 0034_marketplace_ref_pin_v87
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0035_parent_file_id_v88"
down_revision: str = "0034_marketplace_ref_pin_v87"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("corpus_files", sa.Column("parent_file_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("corpus_files", "parent_file_id")
