"""User journey state — per-user onboarding progress (DuckDB v97).

Renumbered from v92 to v97 (and re-chained onto 0043_data_apps_v96) after
upstream's connect_hint/glossary/jobs/usage-fix/data_apps migrations claimed
v92..v96 first.

Revision ID: 0044_user_journey_state_v97
Revises: 0043_data_apps_v96
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0044_user_journey_state_v97"
down_revision: str = "0043_data_apps_v96"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_journey_state",
        sa.Column("user_id", sa.String(), primary_key=True),
        sa.Column("first_asked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("stack_setup_done", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("explored_stack", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("catalog_discovered", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("use_anywhere", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("onboarded", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("successful_answers", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_journey_state")
