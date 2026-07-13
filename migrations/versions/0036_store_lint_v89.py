"""Skill lint (store guardrails) — store_lint_runs/findings/dismissals/entity_state (DuckDB v89).

Revision ID: 0036_store_lint_v89
Revises: 0035_parent_file_id_v88
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0036_store_lint_v89"
down_revision: str = "0035_parent_file_id_v88"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "store_lint_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entities_linted", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("entities_skipped", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("findings_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_table(
        "store_lint_findings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("rule_id", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("message", sa.String(), nullable=False),
        sa.Column("evidence", sa.String(), server_default=sa.text("'{}'"), nullable=True),
        sa.Column("doc_url", sa.String(), server_default=sa.text("''"), nullable=True),
        sa.Column("content_hash", sa.String(), server_default=sa.text("''"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_store_lint_findings_entity", "store_lint_findings", ["entity_id"])
    op.create_table(
        "store_lint_dismissals",
        sa.Column("entity_id", sa.String(), nullable=False),
        sa.Column("rule_id", sa.String(), nullable=False),
        sa.Column("dismissed_by", sa.String(), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("entity_id", "rule_id"),
    )
    op.create_table(
        "store_lint_entity_state",
        sa.Column("entity_id", sa.String(), primary_key=True),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("linted_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("store_lint_entity_state")
    op.drop_table("store_lint_dismissals")
    op.drop_index("idx_store_lint_findings_entity", table_name="store_lint_findings")
    op.drop_table("store_lint_findings")
    op.drop_table("store_lint_runs")
