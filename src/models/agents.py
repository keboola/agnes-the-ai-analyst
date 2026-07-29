"""SQLAlchemy model for the agent registry (v103): agents.

Mirrors DuckDB DDL in src/db.py (``_AGENTS_CREATE_SQL``, applied by both
``_SYSTEM_SCHEMA`` on a fresh install and ``_v102_to_v103`` on upgrade) and the
Alembic migration ``0050_agents_v103``.

PG notes:
- ``knowledge``/``plugins``/``surfaces`` store JSON as Text (same as the DuckDB
  side), decoded in the repository layer — see
  ``src/repositories/agents.py::decode_json_column``. They are opaque id lists
  the Agent builder owns and are never joined against in SQL, so a text payload
  is the honest representation rather than three junction tables.
- Timestamps use ``DateTime(timezone=True)``, matching the sibling Collections
  cluster (``src/models/collections.py``).
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base

_text = sa.text


class Agent(Base):
    __tablename__ = "agents"
    # The Library and the builder both list "my agents"; created_by is the only
    # access-scoping predicate either one filters on.
    __table_args__ = (sa.Index("idx_agents_created_by", "created_by"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[str | None] = mapped_column(String, server_default="", nullable=True)
    instructions: Mapped[str | None] = mapped_column(Text, server_default="", nullable=True)
    tone: Mapped[str | None] = mapped_column(String, server_default="concise", nullable=True)
    greeting: Mapped[str | None] = mapped_column(Text, server_default="", nullable=True)
    knowledge: Mapped[str | None] = mapped_column(Text, server_default="[]", nullable=True)
    plugins: Mapped[str | None] = mapped_column(Text, server_default="[]", nullable=True)
    surfaces: Mapped[str | None] = mapped_column(Text, server_default="{}", nullable=True)
    status: Mapped[str] = mapped_column(String, server_default="draft", nullable=False)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
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
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
