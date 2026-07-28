"""Alembic environment script for Agnes's Postgres state.

URL resolution (in priority order):
  1. ``cfg.attributes["sqlalchemy.url"]`` — tests pass it in directly to
     avoid touching process-global state.
  2. ``DATABASE_URL`` environment variable — preferred (12-factor convention).
  3. ``AGNES_DB_URL`` environment variable — legacy alias, accepted for
     backward compatibility.

We deliberately do NOT use the ``[alembic] sqlalchemy.url`` ini setting:
configparser interpolates ``%`` characters, which breaks Postgres
connection strings that contain percent-encoded socket paths
(e.g. pgserver, Cloud SQL Unix-socket connections).
"""

from __future__ import annotations

import os
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context

# Alembic Config object
config = context.config

# Logging — skipped when the caller already owns logging config (the
# in-process startup auto-migrate in src/db_pg.py passes
# configure_logger=False); the alembic CLI and tests keep it.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Target metadata = the DeclarativeBase metadata from src/db_pg.py,
# populated by importing src.models (which imports every model module
# so each table is attached to Base.metadata before autogenerate runs).
try:
    from src.db_pg import Base
    import src.models  # noqa: F401 — side-effect import to register models

    target_metadata = Base.metadata
except ImportError:
    target_metadata = None


def _resolve_url() -> str:
    attrs_url = config.attributes.get("sqlalchemy.url") if hasattr(config, "attributes") else None
    if attrs_url:
        return attrs_url
    env_url = os.environ.get("DATABASE_URL") or os.environ.get("AGNES_DB_URL")
    if env_url:
        return env_url
    raise RuntimeError("no database URL: set DATABASE_URL env var or pass cfg.attributes['sqlalchemy.url']")


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a DB.

    Useful for code review and for shipping a migration set to a DBA who
    runs the SQL by hand.
    """
    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect to the target DB and apply migrations."""
    url = _resolve_url()
    connectable = sa.create_engine(url, future=True, poolclass=sa.pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
