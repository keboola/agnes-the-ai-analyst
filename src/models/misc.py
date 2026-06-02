"""SQLAlchemy models for the small assorted tables:
table_profiles, telegram_links, pending_codes, script_registry, news_template,
plus four legacy single-row template tables retained for DuckDB → PG parity.

Mirrors:
  - ``table_profiles``           (src/db.py:319-323)
  - ``telegram_links``           (src/db.py:263-267)
  - ``pending_codes``            (src/db.py:269-273)
  - ``script_registry``          (src/db.py:275-284)
  - ``news_template``            (src/db.py:511-524)
  - ``claude_md_template``       (legacy; consolidate into instance_templates one day)
  - ``welcome_template``         (legacy; consolidate into instance_templates one day)
  - ``setup_banner``             (legacy; same)
  - ``session_extraction_state`` (Claude Code session ingestion checkpoint)
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class TableProfile(Base):
    __tablename__ = "table_profiles"

    table_id: Mapped[str] = mapped_column(String, primary_key=True)
    profile: Mapped[dict] = mapped_column(JSONB, nullable=False)
    profiled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class TelegramLink(Base):
    __tablename__ = "telegram_links"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class PendingCode(Base):
    __tablename__ = "pending_codes"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class ScriptRegistry(Base):
    __tablename__ = "script_registry"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    owner: Mapped[str | None] = mapped_column(String, nullable=True)
    schedule: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    last_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (Index("ix_script_registry_owner", "owner"),)


class NewsTemplate(Base):
    """Multi-version news/announcements record.

    Reads use ``WHERE published = TRUE ORDER BY version DESC LIMIT 1``;
    admin browses all rows. Invariant: at most one row with
    ``published = FALSE`` at any time (the active draft).
    """
    __tablename__ = "news_template"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    intro: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    published: Mapped[bool] = mapped_column(
        Boolean, server_default=text("FALSE"), nullable=False
    )
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
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_news_template_pub_ver", "published", "version"),
    )


# ----------------------------------------------------------------------------
# Legacy single-row template tables.  These predate ``instance_templates``
# in DuckDB and survive in production schemas; consolidating them into the
# keyed ``instance_templates`` store is a follow-up data migration the
# state-machine cutover deliberately does NOT do (it would require dual
# writes from every code path during the rollout). Mirror them 1:1 in PG
# so DuckDB → PG cutover preserves content; a later PR collapses to a
# single table.
# ----------------------------------------------------------------------------

class ClaudeMdTemplate(Base):
    """Single-row table — ``id = 1`` always. Operator-edited CLAUDE.md."""
    __tablename__ = "claude_md_template"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)


class WelcomeTemplate(Base):
    """Single-row table — ``id = 1`` always. Welcome page content."""
    __tablename__ = "welcome_template"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)


class SetupBanner(Base):
    """Single-row table — ``id = 1`` always. Setup-banner content (admin)."""
    __tablename__ = "setup_banner"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)


class SessionExtractionState(Base):
    """Claude Code session ingestion checkpoint — keyed by session file path
    so the extractor can skip already-processed sessions on rerun.
    """
    __tablename__ = "session_extraction_state"

    session_file: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    items_extracted: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_hash: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("ix_session_extraction_state_username", "username"),
    )
