"""SQLAlchemy models for the agent profiles + agent-as-API cluster (DuckDB v100/v101/v102).

Mirrors:
  - ``agents``                  (src/db.py, v100)
  - ``agent_scope``              (src/db.py, v100)
  - ``llm_usage``                (src/db.py, v100)
  - ``agent_scope_snapshots``    (src/db.py, v100)
  - ``idempotency_keys``         (src/db.py, v100)
  - ``agent_webhooks``           (src/db.py, v101)
  - ``agent_artifacts``          (src/db.py, v101)
  - ``agent_memories``           (src/db.py, v102)

and the Alembic migrations ``migrations/versions/0048_agents_v101.py`` /
``migrations/versions/0048_agent_webhooks_artifacts_v101.py`` /
``migrations/versions/0050_agent_memories_v103.py``. This is the schema
foundation for agent profiles + agent-as-API
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
    # v110: paper-theme agent-builder superset. role/tone/greeting are authored
    # profile fields; knowledge/plugins/surfaces are opaque id-list JSON the
    # builder owns (never joined in SQL); status is the builder's draft|ready
    # lifecycle. The builder maps created_by→owner_user_id and
    # instructions→system_prompt, so those reuse the columns above.
    role: Mapped[str | None] = mapped_column(String, nullable=True)
    tone: Mapped[str | None] = mapped_column(String, server_default=text("'concise'"), nullable=True)
    greeting: Mapped[str | None] = mapped_column(Text, nullable=True)
    knowledge: Mapped[str | None] = mapped_column(Text, server_default=text("'[]'"), nullable=True)
    plugins: Mapped[str | None] = mapped_column(Text, server_default=text("'[]'"), nullable=True)
    surfaces: Mapped[str | None] = mapped_column(Text, server_default=text("'{}'"), nullable=True)
    status: Mapped[str | None] = mapped_column(String, server_default=text("'draft'"), nullable=True)
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


class AgentWebhook(Base):
    """Outbound webhook registration for an agent — HMAC-signed POSTs on
    the subscribed comma-joined ``events`` (e.g. ``job.completed,job.failed``).
    ``secret`` is a random signing secret shown once at create, like a PAT.
    ``consecutive_failures``/``disabled_at`` back the auto-disable-after-N-
    failures policy resolved in a later task."""

    __tablename__ = "agent_webhooks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(String, nullable=False)
    secret: Mapped[str] = mapped_column(String, nullable=False)
    events: Mapped[str] = mapped_column(String, server_default=text("'job.completed,job.failed'"), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("TRUE"), nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=True
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=True
    )


class AgentArtifact(Base):
    """Metadata row for a file an agent run produced — the blob itself
    lives in the object store under ``object_key``; this table is
    metadata only (filename/size/content-type/md5 for listing + download
    redirects)."""

    __tablename__ = "agent_artifacts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    owner_user_id: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    object_key: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    md5: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=True
    )


class AgentMemory(Base):
    """Per-agent private memory notebook entry (agent-api V1c). ``status``
    lifecycle is ``pending -> active -> archived``; ``owner_user_id`` is
    denormalized off ``agent_id`` for cheap owner-scoped listing."""

    __tablename__ = "agent_memories"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(String, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, server_default=text("'pending'"), nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=True
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
