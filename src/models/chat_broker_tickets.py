"""SQLAlchemy model for ``chat_broker_tickets`` (v90).

Opaque, short-lived tickets used by the chat sandbox secret broker: a
sandbox-local relay holds a ticket in memory (never in any process env) and
presents it to the broker routes (`app/api/broker.py`) instead of a real
credential. Mirrors the DuckDB DDL in ``src/db.py`` (``_v89_to_v90`` /
``_SYSTEM_SCHEMA``) and the Alembic migration
``migrations/versions/0037_chat_broker_tickets_v90.py``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class ChatBrokerTicket(Base):
    __tablename__ = "chat_broker_tickets"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_chat_broker_tickets_session_id", "session_id"),)
