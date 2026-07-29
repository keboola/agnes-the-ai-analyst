"""personal_access_tokens.surface — credential data-read surface (v106).

'all' (legacy default, admin god-mode read surface) or 'stack'
(catalog/query scoped to the owner's stack even for admins). The
server_default backfills every existing row to 'all' in the same DDL, so
legacy PATs keep today's behavior. Mirrors ``src/db.py::_v105_to_v106``.

Revision ID: 0053_pat_surface_v106
Revises: 0052_sessions_uploaded_v105
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0053_pat_surface_v106"
down_revision: Union[str, None] = "0052_sessions_uploaded_v105"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "personal_access_tokens",
        sa.Column("surface", sa.String(), nullable=True, server_default="all"),
    )


def downgrade() -> None:
    op.drop_column("personal_access_tokens", "surface")
