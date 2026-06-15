"""SQLAlchemy models for the Collections cluster (v77):
file_corpora, corpus_files, corpus_chunks.

Mirrors DuckDB DDL in src/db.py (_v76_to_v77 / _SYSTEM_SCHEMA).

PG notes:
- corpus_chunks.embedding uses float8[] (sa.ARRAY(sa.Float)); pgvector
  vector(384) is a Retrieval-slice option, not a foundation dependency.
- processing_detail stores JSON as VARCHAR text (same as DuckDB side);
  no JSONB cast needed since reads come back as strings.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class FileCorpus(Base):
    __tablename__ = "file_corpora"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=True,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CorpusFile(Base):
    __tablename__ = "corpus_files"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    corpus_id: Mapped[str] = mapped_column(String, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    sha256: Mapped[str] = mapped_column(String, nullable=False)
    file_type: Mapped[str | None] = mapped_column(String, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    storage_path: Mapped[str | None] = mapped_column(String, nullable=True)
    # Four-state lifecycle: pending | processing | indexed | rejected
    processing_status: Mapped[str] = mapped_column(String, server_default=text("'pending'"), nullable=False)
    # JSON text: {tier, vision_used, error, derived_table_id, chunk_count}
    processing_detail: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=True,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=True,
    )


class CorpusChunk(Base):
    __tablename__ = "corpus_chunks"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    corpus_id: Mapped[str] = mapped_column(String, nullable=False)
    file_id: Mapped[str] = mapped_column(String, nullable=False)
    ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str | None] = mapped_column(String, nullable=True)
    # float8[]: plain PG array; pgvector vector(384) is a Retrieval-slice option.
    embedding: Mapped[list[float] | None] = mapped_column(
        ARRAY(item_type=String),  # declared as ARRAY; actual type resolved by Alembic
        nullable=True,
    )
    section_path: Mapped[str | None] = mapped_column(String, nullable=True)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bbox: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=True,
    )
