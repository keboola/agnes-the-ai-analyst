"""SQLAlchemy model for ``knowledge_digests`` (v89, K4, #799).

Mirrors DuckDB DDL in src/db.py (_v88_to_v89 / _SYSTEM_SCHEMA) and the
Alembic migration ``migrations/versions/0036_knowledge_digests_v89.py``.

``source_corpus_ids`` is a JSON array stored as text (String), decoded to a
list by the repository layer — same convention as ``processing_detail`` on
``corpus_files`` (src/models/collections.py).
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base

_text = sa.text  # alias so it doesn't shadow the Text column type import


class KnowledgeDigest(Base):
    __tablename__ = "knowledge_digests"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    source_corpus_ids: Mapped[str | None] = mapped_column(String, nullable=True)
    output_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    # pending (never generated) | fresh | stale (see status_reason)
    status: Mapped[str | None] = mapped_column(String, server_default=_text("'pending'"), nullable=True)
    status_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=_text("CURRENT_TIMESTAMP"),
        nullable=True,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=_text("CURRENT_TIMESTAMP"),
        nullable=True,
    )
