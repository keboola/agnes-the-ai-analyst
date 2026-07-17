"""SQLAlchemy model for ``jobs`` (v94) — durable job queue.

Mirrors the DuckDB DDL in ``src/db.py`` (``_v93_to_v94`` / ``_SYSTEM_SCHEMA``)
and the Alembic migration ``migrations/versions/0041_jobs_v94.py``. This is
the wave-2B worker-runtime foundation: enqueue/get/list + idempotency
dedup only (claim/lease lifecycle + worker loop are later tasks).

``idx_jobs_idem`` is a *partial unique* index on Postgres — ``WHERE
idempotency_key IS NOT NULL AND status IN ('queued', 'running')`` — so a
duplicate key cannot be inserted concurrently while a queued/running row
holds it (``JobsPgRepository.enqueue()`` uses it as an ``ON CONFLICT``
arbiter). DuckDB has no partial-index support, so its sibling
(``src/db.py``) keeps this a plain index and enforces dedup in
``JobsRepository.enqueue()`` instead — see the migration's module
docstring for the full rationale for this asymmetry.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    payload_json: Mapped[str] = mapped_column(String, server_default=text("'{}'"), nullable=False)
    status: Mapped[str] = mapped_column(String, server_default=text("'queued'"), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"), nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, server_default=text("3"), nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    leased_by: Mapped[str | None] = mapped_column(String, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_jobs_claim", "status", "priority", "run_after"),
        Index(
            "idx_jobs_idem",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL AND status IN ('queued', 'running')"),
        ),
    )
