"""SQLAlchemy model for ``data_apps`` (v96) — hosted user web apps registry.

Mirrors the DuckDB DDL (``src/db.py``'s ``_DATA_APPS_CREATE_SQL`` / shared by
fresh-install and ``_v95_to_v96``) and the Alembic migration
``migrations/versions/0043_data_apps_v96.py`` column-for-column. Cross-engine
behavior parity (not just schema) is covered by
``tests/db_pg/test_data_apps_contract.py``; the raw-SQL PG repository lives at
``src/repositories/data_apps_pg.py``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from src.db_pg import Base


class DataApp(Base):
    __tablename__ = "data_apps"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, server_default=text("''"), nullable=True)
    owner_user_id: Mapped[str] = mapped_column(String, nullable=False)
    repo_mode: Mapped[str] = mapped_column(String, server_default=text("'internal'"), nullable=False)
    repo_url: Mapped[str | None] = mapped_column(String, server_default=text("''"), nullable=True)
    repo_branch: Mapped[str | None] = mapped_column(String, server_default=text("'main'"), nullable=True)
    deployed_sha: Mapped[str | None] = mapped_column(String, server_default=text("''"), nullable=True)
    runtime_tag: Mapped[str | None] = mapped_column(String, server_default=text("''"), nullable=True)
    state: Mapped[str] = mapped_column(String, server_default=text("'created'"), nullable=False)
    state_detail: Mapped[str | None] = mapped_column(Text, server_default=text("''"), nullable=True)
    secrets_enc: Mapped[str | None] = mapped_column(Text, server_default=text("''"), nullable=True)
    env: Mapped[str | None] = mapped_column(Text, server_default=text("'{}'"), nullable=True)
    cpu_limit: Mapped[str | None] = mapped_column(String, server_default=text("''"), nullable=True)
    mem_limit: Mapped[str | None] = mapped_column(String, server_default=text("''"), nullable=True)
    idle_timeout_s: Mapped[int | None] = mapped_column(Integer, server_default=text("1800"), nullable=True)
    sleep_mode: Mapped[str | None] = mapped_column(String, server_default=text("'recreate'"), nullable=True)
    service_token_id: Mapped[str | None] = mapped_column(String, server_default=text("''"), nullable=True)
    # NULL until the app's first (re)deploy / request — no DuckDB default either.
    last_request_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_deploy_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Neither the DuckDB DDL nor the alembic migration marks these NOT NULL
    # (both rely on the default) — mirror that exactly so autogenerate
    # doesn't see a constraint the applied migration never creates.
    created_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=text("now()"), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, server_default=text("now()"), nullable=True)
