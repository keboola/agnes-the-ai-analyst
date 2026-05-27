"""SQLAlchemy models for the v49+ catalog cluster.

Tables created by alembic ``0012_data_packages``:

  - ``data_packages``               — admin-curated package metadata
  - ``data_package_tables``         — package → table_registry M:N
  - ``memory_domains``              — first-class memory-domain entity
  - ``knowledge_item_domains``      — knowledge_item → memory_domain M:N
  - ``memory_domain_suggestions``   — non-admin domain-creation queue
  - ``recipes``                     — admin-curated query templates
  - ``user_stack_subscriptions``    — per-user opt-in for ``available`` grants

Kept in one module because every entity here is part of the same
admin-curated catalog surface; splitting them across files just for
narrative separation would scatter the FK declarations needlessly.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class DataPackage(Base):
    __tablename__ = "data_packages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon: Mapped[str | None] = mapped_column(String, nullable=True)
    color: Mapped[str | None] = mapped_column(String, nullable=True)
    cover_image_url: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str | None] = mapped_column(
        String, server_default=text("'prod'"), nullable=True
    )
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # v56 extended-content surface — all NULLABLE + additive.
    owner_name: Mapped[str | None] = mapped_column(String, nullable=True)
    owner_team: Mapped[str | None] = mapped_column(String, nullable=True)
    tags: Mapped[str | None] = mapped_column(String, nullable=True)
    long_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    when_to_use: Mapped[str | None] = mapped_column(String, nullable=True)
    when_not_to_use: Mapped[str | None] = mapped_column(String, nullable=True)
    example_questions: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class DataPackageTable(Base):
    __tablename__ = "data_package_tables"

    package_id: Mapped[str] = mapped_column(
        String, ForeignKey("data_packages.id"), nullable=False
    )
    table_id: Mapped[str] = mapped_column(
        String, ForeignKey("table_registry.id"), nullable=False
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    added_by: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("package_id", "table_id"),
        Index("idx_data_package_tables_table", "table_id"),
    )


class MemoryDomain(Base):
    __tablename__ = "memory_domains"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon: Mapped[str | None] = mapped_column(String, nullable=True)
    color: Mapped[str | None] = mapped_column(String, nullable=True)
    cover_image_url: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str | None] = mapped_column(
        String, server_default=text("'prod'"), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class KnowledgeItemDomain(Base):
    __tablename__ = "knowledge_item_domains"

    item_id: Mapped[str] = mapped_column(
        String, ForeignKey("knowledge_items.id"), nullable=False
    )
    domain_id: Mapped[str] = mapped_column(
        String, ForeignKey("memory_domains.id"), nullable=False
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    added_by: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("item_id", "domain_id"),
        Index("idx_knowledge_item_domains_domain", "domain_id"),
    )


class MemoryDomainSuggestion(Base):
    __tablename__ = "memory_domain_suggestions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(
        String, server_default=text("'pending'"), nullable=True
    )
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_domain_id: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("idx_memory_domain_suggestions_status", "status"),
    )


class Recipe(Base):
    __tablename__ = "recipes"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon: Mapped[str | None] = mapped_column(String, nullable=True)
    color: Mapped[str | None] = mapped_column(String, nullable=True)
    sql_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    related_table_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str | None] = mapped_column(
        String, server_default=text("'prod'"), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class UserStackSubscription(Base):
    __tablename__ = "user_stack_subscriptions"

    user_id: Mapped[str] = mapped_column(String, nullable=False)
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[str] = mapped_column(String, nullable=False)
    subscribed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )

    __table_args__ = (
        PrimaryKeyConstraint("user_id", "resource_type", "resource_id"),
    )
