"""User journey state — per-user onboarding progress (DuckDB v98).

Renumbered from v92 to v98 (and re-chained onto 0044_corpus_files_path_v97)
after upstream's connect_hint/glossary/jobs/usage-fix/data_apps/corpus_files_path
migrations claimed v92..v97 first.

Revision ID: 0045_user_journey_state_v98
Revises: 0044_corpus_files_path_v97
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0045_user_journey_state_v98"
down_revision: str = "0044_corpus_files_path_v97"
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
