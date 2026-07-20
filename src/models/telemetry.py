"""SQLAlchemy models for the telemetry + observability cluster:
session_processor_state, user_observability_views, usage_events,
usage_session_summary, usage_tool_daily, usage_marketplace_item_daily,
usage_marketplace_item_window.

Mirrors src/db.py:200-208, 2885-2892, 721-789, 795-839.
"""

from __future__ import annotations

from datetime import datetime, date as _date

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class SessionProcessorState(Base):
    __tablename__ = "session_processor_state"

    processor_name: Mapped[str] = mapped_column(String, nullable=False)
    session_file: Mapped[str] = mapped_column(String, nullable=False)
    username: Mapped[str] = mapped_column(String, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    items_extracted: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    file_hash: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (PrimaryKeyConstraint("processor_name", "session_file"),)


class UserObservabilityView(Base):
    __tablename__ = "user_observability_views"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    query_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_obs_views_user_name"),
        Index("idx_obs_views_user", "user_id", "created_at"),
    )


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    session_file: Mapped[str] = mapped_column(String, nullable=False)
    username: Mapped[str] = mapped_column(String, nullable=False)
    event_uuid: Mapped[str | None] = mapped_column(String, nullable=True)
    parent_uuid: Mapped[str | None] = mapped_column(String, nullable=True)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String, nullable=True)
    skill_name: Mapped[str | None] = mapped_column(String, nullable=True)
    subagent_type: Mapped[str | None] = mapped_column(String, nullable=True)
    command_name: Mapped[str | None] = mapped_column(String, nullable=True)
    is_error: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    ref_id: Mapped[str | None] = mapped_column(String, nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    cwd: Mapped[str | None] = mapped_column(String, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processor_version: Mapped[int] = mapped_column(Integer, nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    friction_tags: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("idx_usage_events_session", "session_id"),
        Index("idx_usage_events_user_time", "username", "occurred_at"),
        Index("idx_usage_events_tool", "tool_name"),
        Index("idx_usage_events_skill", "skill_name"),
        Index("idx_usage_events_ref", "source", "ref_id"),
        Index("idx_usage_events_user_id", "user_id"),
    )


class UsageSessionSummary(Base):
    __tablename__ = "usage_session_summary"

    session_file: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    username: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wall_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_messages: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    assistant_messages: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    tool_calls: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    tool_errors: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    skill_invocations: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    subagent_dispatches: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    mcp_calls: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    slash_commands: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    distinct_tools: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    distinct_skills: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    primary_model: Mapped[str | None] = mapped_column(String, nullable=True)
    processor_version: Mapped[int] = mapped_column(Integer, nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    input_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    cache_creation_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # No secondary indexes here (dropped in the matching Alembic revision,
    # 0041_usage_summary_idx_fix_v94 — mirrors DuckDB _v93_to_v94):
    # upsert_summary's ON CONFLICT DO UPDATE refreshes username / started_at
    # / user_id on every re-process tick, and on DuckDB updating an
    # ART-indexed column made a corrupt secondary-index entry fatal
    # (INCIDENT 2026-07-20), so the indexes were removed — updating an
    # unindexed column is safe. Do not re-add them. session_file remains
    # the sole PRIMARY KEY.


class UsageToolDaily(Base):
    __tablename__ = "usage_tool_daily"

    day: Mapped[_date] = mapped_column(Date, nullable=False)
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    invocations: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    distinct_users: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    distinct_sessions: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)

    __table_args__ = (PrimaryKeyConstraint("day", "tool_name", "source"),)


class UsageMarketplaceItemDaily(Base):
    __tablename__ = "usage_marketplace_item_daily"

    day: Mapped[_date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    parent_plugin: Mapped[str] = mapped_column(String, server_default=text("''"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    distinct_users: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("day", "source", "type", "parent_plugin", "name"),
        Index("idx_mid_lookup", "source", "type", "parent_plugin", "name"),
    )


class UsageMarketplaceItemWindow(Base):
    __tablename__ = "usage_marketplace_item_window"

    period_label: Mapped[str] = mapped_column(String, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    parent_plugin: Mapped[str] = mapped_column(String, server_default=text("''"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    invocations: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    distinct_users: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        PrimaryKeyConstraint("period_label", "source", "type", "parent_plugin", "name"),
        Index("idx_miw_lookup", "period_label", "source", "type"),
    )
