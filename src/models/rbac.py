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
    """Generic ``(group, resource_type, resource_id)`` access tuple.

    Polymorphic by design: ``resource_id`` is a free-form string whose
    schema is determined by the enum member persisted in
    ``resource_type``. There is intentionally NO PG-level foreign key
    from ``resource_id`` to the target entity table — see the
    cross-type table below for why.

    +------------------------+--------------------------------+----------------+
    | resource_type          | resource_id shape              | target table   |
    +========================+================================+================+
    | ``marketplace_plugin`` | ``<slug>/<plugin_name>``       | composite —    |
    |                        |                                | no surrogate   |
    +------------------------+--------------------------------+----------------+
    | ``table``              | ``<table_id>`` (UUID)          | table_registry |
    +------------------------+--------------------------------+----------------+
    | ``data_package``       | ``<package_id>`` (UUID)        | data_packages  |
    +------------------------+--------------------------------+----------------+
    | ``memory_domain``      | ``<memory_domain_id>`` (UUID)  | memory_domains |
    +------------------------+--------------------------------+----------------+
    | ``memory_item``        | ``<knowledge_item_id>`` (UUID) | knowledge_items|
    +------------------------+--------------------------------+----------------+
    | ``recipe``             | ``<recipe_id>`` (UUID)         | recipes        |
    +------------------------+--------------------------------+----------------+

    Why no FK (E.3 from round-2 review; cvrysanek's LOW finding):

    1. ``marketplace_plugin`` uses a composite path that doesn't match
       any single surrogate column on ``marketplace_plugins``. The
       table's PK is ``(marketplace_id, name)``; the grant id is
       ``"<slug>/<name>"`` where slug != marketplace_id. A FK would
       require either denormalising the grant (split into two columns)
       or denormalising the plugins table (add a ``slug_path`` column
       with a CHECK that it stays consistent with id+name).
    2. PG doesn't support polymorphic FKs natively. The five non-
       marketplace types all target tables with stable surrogate
       UUIDs, but a single FK constraint can only reference ONE
       target table — encoding the per-type target requires either
       five conditional CHECK-via-trigger constraints (verbose,
       maintenance burden when adding a new resource type) or five
       NULLable FK columns with a CHECK that exactly one is non-NULL
       (verbose; requires migrating every existing grant row to split
       resource_id into the right per-type column).
    3. Orphan grants are an information-leakage non-issue today:
       ``app.auth.access`` does the existence check at lookup time
       (the resource list returned from ``list_blocks`` drops missing
       entities), and the admin /access UI surfaces a per-grant
       "no longer exists" badge so operators clean up at their pace.
       The downside of orphan rows is purely cosmetic — they don't
       grant access to anything that doesn't exist.

    The pragmatic choice: keep the loose link, document why, and let
    the application layer be the source of truth for FK enforcement.
    A periodic admin "clean up orphan grants" job (currently manual
    via /admin/access) is the cheapest way to bound the orphan
    population if it becomes a real problem.
    """

    __tablename__ = "resource_grants"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    group_id: Mapped[str] = mapped_column(
        String, ForeignKey("user_groups.id"), nullable=False
    )
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    # Polymorphic — see class docstring for shape per ``resource_type``.
    # Intentionally no FK; orphan cleanup is application-layer.
    resource_id: Mapped[str] = mapped_column(String, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    assigned_by: Mapped[str | None] = mapped_column(String, nullable=True)
    # DuckDB v50+ adds the `requirement` column to flag grants as
    # ``available`` (default) vs ``required`` (must-install for the group).
    # Nullable so legacy rows that predate the column migrate cleanly.
    requirement: Mapped[str | None] = mapped_column(String, nullable=True)

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
