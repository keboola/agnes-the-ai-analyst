"""heal mcp_sources.connect_hint on DBs stranded by the migration renumbering

Mirrors DuckDB ``_v100_to_v101``. ``connect_hint`` is added by
``0039_mcp_connect_hint_v92`` (DuckDB ``_v91_to_v92``); a DuckDB state DB
stamped at v92+ under the *old* numbering skips that version-guarded step and
never gets the column, 500ing every ``mcp_sources`` read that selects it.

Postgres reaches ``connect_hint`` linearly via ``0039`` and is not stranded
the way the DuckDB version-integer guard is — so on a normal PG DB this is a
pure no-op that only advances the revision to keep the two ladders' endpoints
aligned. The guarded add keeps it correct even if a PG DB somehow lacks the
column, and is idempotent so re-runs are safe.

Revision ID: 0048_mcp_connect_hint_heal_v101
Revises: 0047_corpus_files_path_heal_v100
Create Date: 2026-07-24

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0048_mcp_connect_hint_heal_v101"
down_revision: Union[str, None] = "0047_corpus_files_path_heal_v100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "mcp_sources" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("mcp_sources")}
    if "connect_hint" not in cols:
        op.add_column("mcp_sources", sa.Column("connect_hint", sa.String(), nullable=True))


def downgrade() -> None:
    # A repair step, not a schema addition: ``mcp_sources.connect_hint``
    # belongs to 0039. Reversing to 0047 leaves it in place — dropping it here
    # would undo 0039's work. No-op.
    pass
