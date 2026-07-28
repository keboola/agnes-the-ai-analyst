"""knowledge cluster: knowledge_items, knowledge_contradictions,
knowledge_item_relations, verification_evidence, knowledge_votes,
knowledge_item_user_dismissed.

Revision ID: 0010_knowledge
Revises: 0009_store
Create Date: 2026-05-24

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "0010_knowledge"
down_revision: Union[str, None] = "0009_store"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_items",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("tags", JSONB(), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("contributors", JSONB(), nullable=True),
        sa.Column("source_user", sa.String(), nullable=True),
        sa.Column("audience", sa.String(), nullable=True),
        sa.Column("confidence", sa.Double(), nullable=True),
        sa.Column("domain", sa.String(), nullable=True),
        sa.Column("entities", JSONB(), nullable=True),
        sa.Column("source_type", sa.String(), server_default=sa.text("'claude_local_md'"), nullable=False),
        sa.Column("source_ref", sa.String(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes", sa.String(), nullable=True),
        sa.Column("sensitivity", sa.String(), server_default=sa.text("'internal'"), nullable=False),
        sa.Column("is_personal", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_knowledge_items_status", "knowledge_items", ["status"])
    op.create_index("idx_knowledge_items_category", "knowledge_items", ["category"])
    op.create_index("idx_knowledge_items_domain", "knowledge_items", ["domain"])
    op.create_index("idx_knowledge_items_source_user", "knowledge_items", ["source_user"])

    op.create_table(
        "knowledge_contradictions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("item_a_id", sa.String(), nullable=False),
        sa.Column("item_b_id", sa.String(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("severity", sa.String(), nullable=True),
        sa.Column("suggested_resolution", sa.Text(), nullable=True),
        sa.Column("resolved", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.String(), nullable=True),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "knowledge_item_relations",
        sa.Column("item_a_id", sa.String(), nullable=False),
        sa.Column("item_b_id", sa.String(), nullable=False),
        sa.Column("relation_type", sa.String(), nullable=False),
        sa.Column("score", sa.Double(), nullable=True),
        sa.Column("resolved", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("item_a_id", "item_b_id", "relation_type"),
    )
    op.create_index("idx_knowledge_item_relations_resolved", "knowledge_item_relations", ["resolved"])

    op.create_table(
        "verification_evidence",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("source_user", sa.String(), nullable=True),
        sa.Column("source_ref", sa.String(), nullable=True),
        sa.Column("detection_type", sa.String(), nullable=True),
        sa.Column("user_quote", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_verification_evidence_item", "verification_evidence", ["item_id"])

    op.create_table(
        "knowledge_votes",
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("vote", sa.Integer(), nullable=True),
        sa.Column(
            "voted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("item_id", "user_id"),
    )

    op.create_table(
        "knowledge_item_user_dismissed",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column(
            "dismissed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id", "item_id"),
    )
    op.create_index(
        "idx_knowledge_item_user_dismissed_user",
        "knowledge_item_user_dismissed",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_knowledge_item_user_dismissed_user", table_name="knowledge_item_user_dismissed")
    op.drop_table("knowledge_item_user_dismissed")
    op.drop_table("knowledge_votes")
    op.drop_index("idx_verification_evidence_item", table_name="verification_evidence")
    op.drop_table("verification_evidence")
    op.drop_index("idx_knowledge_item_relations_resolved", table_name="knowledge_item_relations")
    op.drop_table("knowledge_item_relations")
    op.drop_table("knowledge_contradictions")
    op.drop_index("idx_knowledge_items_source_user", table_name="knowledge_items")
    op.drop_index("idx_knowledge_items_domain", table_name="knowledge_items")
    op.drop_index("idx_knowledge_items_category", table_name="knowledge_items")
    op.drop_index("idx_knowledge_items_status", table_name="knowledge_items")
    op.drop_table("knowledge_items")
