"""agent profiles + agent-as-API foundation (DuckDB v96).

Revision ID: 0043_agents_v96
Revises: 0042_usage_summary_idx_fix_v95

Mirrors DuckDB's ``_v95_to_v96`` / ``_SYSTEM_SCHEMA`` additions (spec
docs/superpowers/specs/2026-07-21-agent-profiles-and-agent-api-design.md).
Creates the agent profile table (``agents``), its scope grants
(``agent_scope``), per-call token accounting (``llm_usage``), a per-session
audit trail of the effective scope actually applied (``agent_scope_snapshots``),
and idempotent-replay storage for the agent-as-API surface
(``idempotency_keys``); adds ``agent_id`` to ``personal_access_tokens`` and
``chat_sessions``.

No ``op.create_index`` calls anywhere in this revision — see the
``_v94_to_v95`` docstring for the DuckDB ART-index incident this repo avoids
repeating. ``chat_sessions.agent_id`` especially must stay unindexed.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0043_agents_v96"
down_revision: Union[str, None] = "0042_usage_summary_idx_fix_v95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("token_budget_monthly", sa.BigInteger(), nullable=True),
        sa.Column("plugins_mode", sa.String(), server_default=sa.text("'all'"), nullable=False),
        sa.Column("connections_mode", sa.String(), server_default=sa.text("'all'"), nullable=False),
        sa.Column("tables_mode", sa.String(), server_default=sa.text("'all'"), nullable=False),
        sa.Column("memory_mode", sa.String(), server_default=sa.text("'all'"), nullable=False),
        sa.Column("memory_write_mode", sa.String(), server_default=sa.text("'propose'"), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("FALSE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("owner_user_id", "slug", name="uq_agents_owner_slug"),
    )

    op.create_table(
        "agent_scope",
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("item_type", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("agent_id", "item_type", "item_id", name="pk_agent_scope"),
    )

    op.create_table(
        "llm_usage",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("cache_read_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("cache_creation_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
    )

    op.create_table(
        "agent_scope_snapshots",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("effective_scope", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("owner_user_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("key", "owner_user_id", "agent_id", name="pk_idempotency_keys"),
    )

    op.add_column("personal_access_tokens", sa.Column("agent_id", sa.String(), nullable=True))
    op.add_column("chat_sessions", sa.Column("agent_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "agent_id")
    op.drop_column("personal_access_tokens", "agent_id")
    op.drop_table("idempotency_keys")
    op.drop_table("agent_scope_snapshots")
    op.drop_table("llm_usage")
    op.drop_table("agent_scope")
    op.drop_table("agents")
