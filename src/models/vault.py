"""SQLAlchemy model for the system-secrets vault.

Mirrors:
  - system_secrets (src/db.py v72)
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, LargeBinary, String, text
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class SystemSecret(Base):
    """Server-wide vault for system-level secrets keyed by name (v72).

    Holds the Fernet ciphertext of server-wide secrets not tied to an MCP
    source — currently the three Slack bot tokens.
    """
    __tablename__ = "system_secrets"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    secret_value_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
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
