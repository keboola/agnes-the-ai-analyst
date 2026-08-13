"""user_journey_state.agent_created — sixth onboarding step

Mirrors DuckDB ``_v117_to_v118``. Adds the ``agent_created`` boolean to
``user_journey_state`` so the "Create your first agent" checklist step is
backed by a real column.

The checklist step had no backing column, so it could not be tracked. The
BACKFILL is the substance of this migration, not the column: a flag landing
FALSE for everyone re-opens a checklist people had already finished, which
reads as the product losing their progress. Anyone who owns an agent, or who
is already flagged ``onboarded``, is marked done — see the DuckDB twin for the
full reasoning.

Revision ID: 0065_journey_agent_created_v118
Revises: 0064_sem_models_v117
Create Date: 2026-08-13

Note on the revision id: chains after ``0064_sem_models_v117`` rather than
``0062_knowledge_domains_backfill`` (the head at the time this migration was
written) because ``0063_access_policy_columns_v116`` and
``0064_sem_models_v117`` landed on ``main`` first — the DuckDB ladder position
moved from v115→v116 to v117→v118 to match. ``alembic_version.version_num``
is ``VARCHAR(32)``, so keep new ids short (see 0051_store_publisher_v104's
note).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0065_journey_agent_created_v118"
down_revision: Union[str, None] = "0064_sem_models_v117"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "user_journey_state" not in set(insp.get_table_names()):
        return
    existing_cols = {col["name"] for col in insp.get_columns("user_journey_state")}
    if "agent_created" in existing_cols:
        return
    op.add_column(
        "user_journey_state",
        sa.Column(
            "agent_created",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Same backfill as the DuckDB ladder, or the two engines disagree about
    # whose checklist re-opens.
    op.execute("UPDATE user_journey_state SET agent_created = TRUE WHERE onboarded = TRUE")
    if "agents" in set(insp.get_table_names()):
        op.execute(
            "UPDATE user_journey_state SET agent_created = TRUE "
            "WHERE user_id IN (SELECT DISTINCT owner_user_id FROM agents)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if "user_journey_state" not in set(insp.get_table_names()):
        return
    existing_cols = {col["name"] for col in insp.get_columns("user_journey_state")}
    if "agent_created" in existing_cols:
        op.drop_column("user_journey_state", "agent_created")
