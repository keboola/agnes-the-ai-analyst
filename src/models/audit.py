"""SQLAlchemy model for the audit_log table.

Shape mirrors the DuckDB equivalent defined inline in
``src/db.py:248-261`` (lines 248-261 of the _SYSTEM_SCHEMA block).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, String, Integer, DateTime, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    action: Mapped[str] = mapped_column(String, nullable=False)
    resource: Mapped[str | None] = mapped_column(String, nullable=True)
    # JSONB for indexability + key-path queries; matches DuckDB JSON role
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    params_before: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String, nullable=True)
    client_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (
        # Mirrors the v41 add_audit_log_indices migration: keyset pagination
        # over (timestamp DESC, id DESC), and lookup helpers used by the
        # /admin/activity endpoint.
        Index("ix_audit_log_timestamp", "timestamp"),
        Index("ix_audit_log_user_id", "user_id"),
        Index("ix_audit_log_action", "action"),
        Index("ix_audit_log_correlation_id", "correlation_id"),
        Index("ix_audit_log_resource", "resource"),
    )
