"""agents builder superset columns

Mirrors DuckDB ``_v109_to_v110``. Combines the paper-theme agent-builder's
authored fields onto main's canonical ``agents`` table so both the
agent-as-API backend (main) and the /agents builder (paper-theme) read one
table:

- ``role`` / ``tone`` / ``greeting`` — authored profile fields.
- ``knowledge`` / ``plugins`` / ``surfaces`` — opaque id-list payloads (JSON
  text) the builder owns, never joined against in SQL.
- ``status`` — the builder's draft | ready lifecycle.

The builder maps ``created_by`` → ``owner_user_id`` and ``instructions`` →
``system_prompt`` in the repository layer, so no duplicate columns are added
for those. All adds are guarded on existence so the step is a no-op where the
columns are already present.

Revision ID: 0057_agents_superset_v110
Revises: 0051_store_publisher_v104
Create Date: 2026-07-29

Note on the revision id: ``alembic_version.version_num`` is ``VARCHAR(32)``,
so keep new ids short (see 0051_store_publisher_v104's note).

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0057_agents_superset_v110"
down_revision: Union[str, None] = "0051_store_publisher_v104"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (name, type, server_default) — matches src/db.py _v109_to_v110 and the
# src/models/agents.py Agent model.
_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, str | None], ...] = (
    ("role", sa.String(), None),
    ("tone", sa.String(), "concise"),
    ("greeting", sa.Text(), None),
    ("knowledge", sa.Text(), "[]"),
    ("plugins", sa.Text(), "[]"),
    ("surfaces", sa.Text(), "{}"),
    ("status", sa.String(), "draft"),
)


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "agents" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("agents")}
    for name, type_, default in _COLUMNS:
        if name not in existing:
            op.add_column(
                "agents",
                sa.Column(name, type_, nullable=True, server_default=default),
            )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "agents" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("agents")}
    for name, _type, _default in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("agents", name)
