"""SQLAlchemy models for the agent profiles + agent-as-API cluster (DuckDB v96).

Mirrors:
  - ``agents``                  (src/db.py, v96)
  - ``agent_scope``              (src/db.py, v96)
  - ``llm_usage``                (src/db.py, v96)
  - ``agent_scope_snapshots``    (src/db.py, v96)
  - ``idempotency_keys``         (src/db.py, v96)

and the Alembic migration ``migrations/versions/0043_agents_v96.py``. This is
the schema foundation for agent profiles + agent-as-API
(docs/superpowers/specs/2026-07-21-agent-profiles-and-agent-api-design.md);
repos/endpoints land in later tasks of the same wave.

No secondary indexes on any of these tables — see the ``_v94_to_v95``
DuckDB ART-index incident note in ``src/db.py``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class Agent(Base):
    """Owner-scoped agent profile — name/prompt/model + per-surface scope
    modes (plugins/connections/tables/memory) that later tasks resolve into
    an effective scope at request time."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    token_budget_monthly: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    plugins_mode: Mapped[str] = mapped_column(String, server_default=text("'all'"), nullable=False)
    connections_mode: Mapped[str] = mapped_column(String, server_default=text("'all'"), nullable=False)
    tables_mode: Mapped[str] = mapped_column(String, server_default=text("'all'"), nullable=False)
    memory_mode: Mapped[str] = mapped_column(String, server_default=text("'all'"), nullable=False)
    memory_write_mode: Mapped[str] = mapped_column(String, server_default=text("'propose'"), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("owner_user_id", "slug", name="uq_agents_owner_slug"),)


class AgentScope(Base):
    """Explicit scope grant — one row per (agent, item_type, item_id) the
    agent may access when its corresponding *_mode is not 'all'."""

    __tablename__ = "agent_scope"

    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    item_type: Mapped[str] = mapped_column(String, nullable=False)
    item_id: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (PrimaryKeyConstraint("agent_id", "item_type", "item_id", name="pk_agent_scope"),)


class LlmUsage(Base):
    """Per-call token accounting, optionally attributed to an agent."""

    __tablename__ = "llm_usage"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, server_default=text("0"), nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, server_default=text("0"), nullable=True)
    cache_read_tokens: Mapped[int | None] = mapped_column(BigInteger, server_default=text("0"), nullable=True)
    cache_creation_tokens: Mapped[int | None] = mapped_column(BigInteger, server_default=text("0"), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=True
    )


class AgentScopeSnapshot(Base):
    """Audit trail of the effective scope actually applied to a session,
    resolved from the agent's mode settings + agent_scope grants at the
    time the session started."""

    __tablename__ = "agent_scope_snapshots"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    effective_scope: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=True
    )


class IdempotencyKey(Base):
    """Idempotent-replay storage for the agent-as-API surface — a caller-
    supplied key scoped to (owner_user_id, agent_id) stores the response
    body/status so a retried request with the same key replays the
    original response instead of re-executing."""

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String, nullable=False)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    request_hash: Mapped[str] = mapped_column(String, nullable=False)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (PrimaryKeyConstraint("key", "owner_user_id", "agent_id", name="pk_idempotency_keys"),)
