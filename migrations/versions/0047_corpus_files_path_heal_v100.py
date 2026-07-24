"""heal corpus_files.path on DBs stranded by the migration renumbering

Mirrors DuckDB ``_v99_to_v100``. The ``corpus_files.path`` column landed at
v97 and ``user_journey_state`` at v98 after a mid-branch renumbering. A DB
stamped at v97+ under the *old* numbering skips DuckDB's ``_v96_to_v97``
version guard forever, so the column never lands and every collection read /
file upload 500s. This step repairs the stranded ones.

Postgres reaches ``path`` linearly via ``0044_corpus_files_path_v97`` and is
not stranded the way the DuckDB version-integer guard is — so on a normal PG
DB this migration is a pure no-op that only advances the revision to keep the
two ladders' endpoints aligned. The guarded add/index keeps it correct even
if a PG DB somehow lacks the column, and idempotent so re-runs are safe.

Revision ID: 0047_corpus_files_path_heal_v100
Revises: 0046_chat_relay_proto_v99
Create Date: 2026-07-24

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0047_corpus_files_path_heal_v100"
down_revision: Union[str, None] = "0046_chat_relay_proto_v99"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    cols = {c["name"] for c in insp.get_columns("corpus_files")}
    if "path" not in cols:
        op.add_column("corpus_files", sa.Column("path", sa.String(), nullable=True))
    idx = {i["name"] for i in insp.get_indexes("corpus_files")}
    if "idx_corpus_files_corpus_path" not in idx:
        # NULLs are distinct on PG and DuckDB, so path=NULL rows are exempt
        # while set paths stay unique. Matches DuckDB's index.
        op.create_index(
            "idx_corpus_files_corpus_path",
            "corpus_files",
            ["corpus_id", "path"],
            unique=True,
        )


def downgrade() -> None:
    # A repair step, not a schema addition: ``corpus_files.path`` and its
    # index belong to 0044. Reversing to 0046 leaves them in place — dropping
    # them here would undo 0044's work. No-op.
    pass
