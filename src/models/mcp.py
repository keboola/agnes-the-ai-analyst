"""SQLAlchemy models for the MCP / Cowork cluster.

Mirrors:
  - setup_tokens                (src/db.py v63)
  - mcp_sources                 (src/db.py v64 + v66 scope column + v92 connect_hint column)
  - tool_registry               (src/db.py v64)
  - tool_grants                 (src/db.py v64)
  - mcp_secrets                 (src/db.py v65)
  - mcp_user_secrets            (src/db.py v66)
  - mcp_source_oauth_clients    (src/db.py v109)
  - mcp_user_oauth_tokens       (src/db.py v109)
  - mcp_oauth_flows             (src/db.py v109)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class SetupToken(Base):
    """Short-lived one-time tokens for Agnes Cowork one-click setup (v63)."""

    __tablename__ = "setup_tokens"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    token_hash: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (Index("ix_setup_tokens_user_id", "user_id"),)


class MCPSource(Base):
    """External MCP server registered for inbound tool ingestion (v64 + v66 + v92)."""

    __tablename__ = "mcp_sources"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    transport: Mapped[str] = mapped_column(String, nullable=False)
    command: Mapped[str | None] = mapped_column(String, nullable=True)
    args: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    env: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    auth_method: Mapped[str | None] = mapped_column(String, nullable=True)
    auth_secret_env: Mapped[str | None] = mapped_column(String, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("TRUE"), nullable=False)
    scope: Mapped[str | None] = mapped_column(String, server_default=text("'shared'"), nullable=True)
    connect_hint: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class ToolRegistry(Base):
    """Curated tools extracted from MCP sources (v64)."""

    __tablename__ = "tool_registry"

    tool_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(String, nullable=False)
    original_name: Mapped[str] = mapped_column(String, nullable=False)
    exposed_name: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    table_id: Mapped[str | None] = mapped_column(String, nullable=True)
    input_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    mutating: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)
    pii_fields: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    rate_limit_pm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    schedule: Mapped[str | None] = mapped_column(String, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("TRUE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (Index("ix_tool_registry_source_id", "source_id"),)


class ToolGrant(Base):
    """Per-group ACL for passthrough tools (v64)."""

    __tablename__ = "tool_grants"

    tool_id: Mapped[str] = mapped_column(String, nullable=False)
    group_id: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("tool_id", "group_id"),
        Index("ix_tool_grants_group_id", "group_id"),
    )


class MCPSecret(Base):
    """Server-wide vault for MCP source auth credentials (v65)."""

    __tablename__ = "mcp_secrets"

    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    secret_value_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class MCPUserSecret(Base):
    """Per-user MCP source secrets for per_user-scope sources (v66)."""

    __tablename__ = "mcp_user_secrets"

    source_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    secret_value_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (PrimaryKeyConstraint("source_id", "user_id"),)


class MCPSourceOAuthClient(Base):
    """Agnes's own OAuth client registration at an upstream MCP source's
    authorization server (v109). One row per OAuth ``mcp_sources`` row.

    Named distinctly from ``oauth_clients`` (the INBOUND issuer's DCR
    table, v82) — mirror-image concept, opposite direction.
    """

    __tablename__ = "mcp_source_oauth_clients"

    source_id: Mapped[str] = mapped_column(String, primary_key=True)
    issuer: Mapped[str] = mapped_column(String, nullable=False)
    client_id: Mapped[str] = mapped_column(String, nullable=False)
    client_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    registration_access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    authorization_endpoint: Mapped[str] = mapped_column(String, nullable=False)
    token_endpoint: Mapped[str] = mapped_column(String, nullable=False)
    scopes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class MCPUserOAuthToken(Base):
    """Per-``(source_id, user_id)`` OAuth access/refresh token pair (v109).

    Kept separate from ``mcp_user_secrets``: different lifecycle
    (server-side refresh mutates rows here; user secrets are write-only
    from the user) and different deletion semantics (best-effort
    revoke-at-AS on disconnect).
    """

    __tablename__ = "mcp_user_oauth_tokens"

    source_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    access_token_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    refresh_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (PrimaryKeyConstraint("source_id", "user_id"),)


class MCPOAuthFlow(Base):
    """In-flight authorize-flow state — PKCE verifier + nonce (v109).

    DB-backed (rather than an in-process/session store) so multi-replica
    Postgres deployments need no sticky sessions and single-replica DuckDB
    works identically. Rows are meant to be single-use (deleted on
    ``consume()``) and swept opportunistically after a ~10 minute TTL.
    """

    __tablename__ = "mcp_oauth_flows"

    nonce: Mapped[str] = mapped_column(String, primary_key=True)
    source_id: Mapped[str] = mapped_column(String, nullable=False)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    pkce_verifier_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
