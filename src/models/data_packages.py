"""SQLAlchemy models for the data_packages cluster:
data_packages, data_package_tables.

Mirrors src/db.py:526-583 (v49 + v50 cover_image_url + v51 status/category
+ v54 deleted_at + v56 extended content). DuckDB stores ``tags``,
``when_to_use``, ``when_not_to_use``, ``example_questions`` as VARCHAR
JSON-encoded strings; on the PG side they are first-class ``JSONB``
columns (same pattern as ``knowledge_items.tags`` etc.). Repository code
on the DuckDB side lives in ``src/repositories/data_packages.py``.
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
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon: Mapped[str | None] = mapped_column(String, nullable=True)
    color: Mapped[str | None] = mapped_column(String, nullable=True)
    # v50: admin-uploaded cover image (served from /uploads/covers/<sha>.<ext>).
    cover_image_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # v51: lifecycle + classification surface for /catalog cards.
    status: Mapped[str] = mapped_column(
        String, server_default=text("'prod'"), nullable=False
    )
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    # v54: soft-delete column. DELETE handlers set this; list/get filter
    # ``deleted_at IS NULL``.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # v56: extended content for the /catalog/p/<slug> detail-page rewrite.
    # JSON list columns are JSONB on PG (VARCHAR on DuckDB).
    owner_name: Mapped[str | None] = mapped_column(String, nullable=True)
    owner_team: Mapped[str | None] = mapped_column(String, nullable=True)
    tags: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    long_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    when_to_use: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    when_not_to_use: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    example_questions: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # ``created_by`` is NULLABLE in DuckDB DDL (no NOT NULL); mirror that
    # here so alembic autogenerate doesn't see drift against the duck side.
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


class DataPackageTable(Base):
    """Bridge between data_packages and table_registry (M:N).

    The DuckDB DDL declares ``REFERENCES data_packages(id)`` and
    ``REFERENCES table_registry(id)`` but no ``ON DELETE`` clause —
    repository code clears the junction explicitly. On the PG side we
    keep the ``data_packages`` FK with ``ON DELETE CASCADE`` so a
    soft-delete-bypassing hard DELETE doesn't leave orphan junctions.
    """
    __tablename__ = "data_package_tables"

    package_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("data_packages.id", ondelete="CASCADE"),
        nullable=False,
    )
    table_id: Mapped[str] = mapped_column(String, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    # ``added_by`` is NULLABLE in DuckDB DDL — mirror it.
    added_by: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("package_id", "table_id"),
        Index("idx_data_package_tables_table", "table_id"),
    )
