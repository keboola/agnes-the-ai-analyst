"""SQLAlchemy model for ``user_journey_state`` (v92).

Per-user onboarding "journey" progress — the backend foundation for
chat-driven onboarding. Mirrors the DuckDB DDL in ``src/db.py``
(``_v91_to_v92`` / ``_SYSTEM_SCHEMA``) and the Alembic migration
``migrations/versions/0039_user_journey_state_v92.py``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class UserJourneyState(Base):
    __tablename__ = "user_journey_state"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)
    first_asked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    stack_setup_done: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    explored_stack: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    catalog_discovered: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    use_anywhere: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    onboarded: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    successful_answers: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default="now()")
