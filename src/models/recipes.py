"""SQLAlchemy model for the recipes cluster.

Mirrors src/db.py:652-667 (v53 recipes table + v54 deleted_at soft-delete
column). Recipes are admin-curated, multi-table query templates surfaced
as a second tab on /catalog — analysts copy + adapt them, they don't
stack-subscribe. ``related_table_ids`` is JSON-encoded VARCHAR on DuckDB
and first-class ``JSONB`` on PG (same mirror pattern as data_packages.tags
/ knowledge_items.tags).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class Recipe(Base):
    __tablename__ = "recipes"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon: Mapped[str | None] = mapped_column(String, nullable=True)
    color: Mapped[str | None] = mapped_column(String, nullable=True)
    sql_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON list of table_registry.id values powering the drilldown links.
    # JSONB on PG, VARCHAR JSON on DuckDB.
    related_table_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Lifecycle pill ('prod' default; 'poc'; 'coming-soon'; 'draft'). DuckDB
    # DDL has no NOT NULL but a server default — mirror the data_packages
    # convention and treat as nullable=False on the PG side (rows always
    # land with a default).
    status: Mapped[str] = mapped_column(
        String, server_default=text("'prod'"), nullable=False
    )
    # v54: soft-delete column. DELETE handlers set this; list/get filter
    # ``deleted_at IS NULL``.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # ``created_by`` is NULLABLE in DuckDB DDL — mirror that here so
    # autogenerate doesn't see drift.
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
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
