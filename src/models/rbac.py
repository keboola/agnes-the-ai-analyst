"""SQLAlchemy models for users + the RBAC tables.

Mirrors the DuckDB shapes in ``src/db.py``:
  - ``users``               (lines 55-79)
  - ``user_groups``         (lines 434-441)
  - ``user_group_members``  (lines 454-461)
  - ``resource_grants``     (lines 470-478)
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    setup_token: Mapped[str | None] = mapped_column(String, nullable=True)
    setup_token_created: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reset_token: Mapped[str | None] = mapped_column(String, nullable=True)
    reset_token_created: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("TRUE"))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deactivated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    onboarded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("FALSE"))
    last_pull_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserGroup(Base):
    __tablename__ = "user_groups"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)


class UserGroupMember(Base):
    __tablename__ = "user_group_members"

    user_id: Mapped[str] = mapped_column(String, nullable=False)
    group_id: Mapped[str] = mapped_column(
        String, ForeignKey("user_groups.id"), nullable=False
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    added_by: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        PrimaryKeyConstraint("user_id", "group_id"),
        Index("ix_user_group_members_user_id", "user_id"),
        Index("ix_user_group_members_group_id", "group_id"),
        Index("ix_user_group_members_source", "source"),
    )


class ResourceGrant(Base):
    __tablename__ = "resource_grants"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    group_id: Mapped[str] = mapped_column(
        String, ForeignKey("user_groups.id"), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[str] = mapped_column(String, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    assigned_by: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "group_id",
            "resource_type",
            "resource_id",
            name="uq_resource_grants_group_type_id",
        ),
        Index("ix_resource_grants_group_id", "group_id"),
        Index("ix_resource_grants_resource_type", "resource_type"),
        Index("ix_resource_grants_resource_id", "resource_id"),
    )
