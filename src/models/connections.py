"""SQLAlchemy models for named source connections (spec 2026-06-12).

Mirrors:
  - source_connections (src/db.py v79)
  - connection_secrets (src/db.py v79)
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class SourceConnection(Base):
    """A named data-source connection (one stack/project per row).

    `config` is JSON-as-text (stack_url, project, location, …); `token_env`
    is the legacy/ops credential fallback. `is_default` is unique per
    source_type — enforced in the repository layer, not the DB.
    """

    __tablename__ = "source_connections"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    config: Mapped[str] = mapped_column(Text, nullable=False)
    token_env: Mapped[str | None] = mapped_column(String, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, server_default=text("FALSE"), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class ConnectionSecret(Base):
    """Vault scope for a source connection's token (Fernet ciphertext)."""

    __tablename__ = "connection_secrets"

    connection_id: Mapped[str] = mapped_column(String, primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
