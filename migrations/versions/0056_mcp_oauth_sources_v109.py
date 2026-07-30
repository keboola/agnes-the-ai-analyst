"""outbound MCP OAuth data-layer foundation

Mirrors DuckDB ``_v108_to_v109``. Three new tables backing outbound OAuth
(authorization-code + PKCE) auth for upstream MCP sources (2026-07-30 spec,
PR 1 / phase 1 — data layer only, no runtime behavior yet):

- ``mcp_source_oauth_clients`` — Agnes's own dynamic client registration
  (RFC 7591) at the upstream authorization server, one row per OAuth
  ``mcp_sources`` row.
- ``mcp_user_oauth_tokens`` — per ``(source_id, user_id)`` access/refresh
  token pair.
- ``mcp_oauth_flows`` — in-flight authorize-flow state (PKCE verifier +
  nonce), DB-backed so multi-replica deployments need no sticky sessions.

Revision ID: 0056_mcp_oauth_sources_v109
Revises: 0055_dataapps_linked_v108
Create Date: 2026-07-30

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0056_mcp_oauth_sources_v109"
down_revision: Union[str, None] = "0055_dataapps_linked_v108"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "mcp_source_oauth_clients",
        sa.Column("source_id", sa.String(), primary_key=True),
        sa.Column("issuer", sa.String(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("client_secret_enc", sa.LargeBinary(), nullable=True),
        sa.Column("registration_access_token_enc", sa.LargeBinary(), nullable=True),
        sa.Column("authorization_endpoint", sa.String(), nullable=False),
        sa.Column("token_endpoint", sa.String(), nullable=False),
        sa.Column("scopes", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
    )
    op.create_table(
        "mcp_user_oauth_tokens",
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("access_token_enc", sa.LargeBinary(), nullable=False),
        sa.Column("refresh_token_enc", sa.LargeBinary(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.PrimaryKeyConstraint("source_id", "user_id"),
    )
    op.create_table(
        "mcp_oauth_flows",
        sa.Column("nonce", sa.String(), primary_key=True),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("pkce_verifier_enc", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("mcp_oauth_flows")
    op.drop_table("mcp_user_oauth_tokens")
    op.drop_table("mcp_source_oauth_clients")
