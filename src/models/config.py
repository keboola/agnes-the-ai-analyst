"""SQLAlchemy models for the config + auth-token cluster:
metric_definitions, glossary_terms, instance_templates, personal_access_tokens.

Mirrors:
  - ``metric_definitions``       (src/db.py:329-352)
  - ``glossary_terms``           (src/db.py, v92 — Keboola semantic-glossary import)
  - ``instance_templates``       (src/db.py:496-502)
  - ``personal_access_tokens``   (src/db.py:365-377)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class MetricDefinition(Base):
    __tablename__ = "metric_definitions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(String, server_default=text("'sum'"), nullable=False)
    unit: Mapped[str | None] = mapped_column(String, nullable=True)
    grain: Mapped[str] = mapped_column(String, server_default=text("'monthly'"), nullable=False)
    table_name: Mapped[str | None] = mapped_column(String, nullable=True)
    tables: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    expression: Mapped[str | None] = mapped_column(String, nullable=True)
    time_column: Mapped[str | None] = mapped_column(String, nullable=True)
    dimensions: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    filters: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    synonyms: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    notes: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    sql: Mapped[str] = mapped_column(Text, nullable=False)
    sql_variants: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    validation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    source: Mapped[str] = mapped_column(String, server_default=text("'manual'"), nullable=False)
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

    __table_args__ = (
        Index("ix_metric_definitions_category", "category"),
        Index("ix_metric_definitions_name", "name"),
    )


class GlossaryTerm(Base):
    """Keboola semantic-glossary import destination
    (docs/superpowers/specs/2026-07-17-keboola-glossary-import-design.md).

    ``id`` is ``keboola/{model_uuid}/{slug(term)}`` for Keboola-sourced rows.
    ``see_also`` is an opaque string list — not resolved/validated against
    other Metastore types.
    """

    __tablename__ = "glossary_terms"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    term: Mapped[str] = mapped_column(String, nullable=False)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    see_also: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    model_uuid: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, server_default=text("'manual'"), nullable=False)
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


class InstanceTemplate(Base):
    """Multi-key operator-customisable template store.

    Seeds at install: 'welcome', 'claude_md'. The news template grew its
    own (versioned) table — see ``news_template`` instead.
    """

    __tablename__ = "instance_templates"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    # v75 (#622): explicit Git⇄Editor source toggle for managed prompts.
    source_mode: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'editor'"))
    git_path: Mapped[str | None] = mapped_column(String, nullable=True)
    base_sha: Mapped[str | None] = mapped_column(String, nullable=True)


class PersonalAccessToken(Base):
    __tablename__ = "personal_access_tokens"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    token_hash: Mapped[str] = mapped_column(String, nullable=False)
    prefix: Mapped[str] = mapped_column(String, nullable=False)
    scopes: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_ip: Mapped[str | None] = mapped_column(String, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_personal_access_tokens_user_id", "user_id"),
        Index("ix_personal_access_tokens_prefix", "prefix"),
    )
