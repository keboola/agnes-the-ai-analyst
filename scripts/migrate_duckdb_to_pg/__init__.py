"""One-shot DuckDB → Postgres data migration.

Usage:

    # Dry-run all tasks (no PG writes)
    python -m scripts.migrate_duckdb_to_pg --dry-run

    # Live migration of every registered table
    python -m scripts.migrate_duckdb_to_pg

    # Just one table, then validate
    python -m scripts.migrate_duckdb_to_pg --only users --validate

The framework:

  - :func:`build_task_list` iterates ``Base.metadata.sorted_tables`` and
    returns a :class:`~tasks.GenericCopyTask` for every table, substituting
    an explicit override from :data:`~tasks.EXPLICIT_TASKS` when one exists.
  - Each task's ``.run()`` selects rows from DuckDB and INSERTs into PG with
    ``ON CONFLICT DO NOTHING`` so re-runs are idempotent.
  - Each task's ``.validate()`` compares row counts + a SHA-256 checksum over
    the PK column set — bit-for-bit equality is too noisy given timestamp
    precision differences, but PK-set equality + count parity is the
    strongest practical signal.

Backwards-compatible shim layer:
  The public names ``MigrationTask``, ``TASKS``, ``run_task``,
  ``validate_task``, and ``run_all`` are preserved so that existing tests
  and operator scripts continue to work unchanged.  ``MigrationTask`` is an
  alias for :class:`~tasks.GenericCopyTask`.  ``TASKS`` is the full ordered
  list produced by :func:`build_task_list`.

After cutover, the DuckDB ``system.duckdb`` file becomes a one-time
snapshot — never written to again. Analytics keep using their own
``analytics.duckdb`` and ``extract.duckdb`` files, unaffected.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import duckdb
from sqlalchemy.engine import Engine

from scripts.migrate_duckdb_to_pg.tasks import (
    EXPLICIT_TASKS,
    GenericCopyTask,
    _JSON_COLUMNS,  # re-exported for any external consumers
    _checksum,
    _build_insert,
    _normalize_for_pg,
    _resolved_columns,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public type alias — kept for backwards compatibility
# ---------------------------------------------------------------------------

#: Alias so ``from scripts.migrate_duckdb_to_pg import MigrationTask`` keeps
#: working in the existing tests.
MigrationTask = GenericCopyTask


# ---------------------------------------------------------------------------
# PK column map — drives validate_task for every table
# ---------------------------------------------------------------------------

# Composite PKs can't be inferred from Base.metadata cheaply at runtime
# (the inspector would need a live connection), so we maintain an explicit
# map for tables whose PK is NOT a single column named "id".
_PK_COLUMNS: Dict[str, List[str]] = {
    "user_group_members": ["user_id", "group_id"],
    "sync_state": ["table_id"],
    "instance_templates": ["key"],
    "view_ownership": ["view_name"],
    "column_metadata": ["table_id", "column_name"],
    "bq_metadata_cache": ["table_id"],
    "user_sync_settings": ["user_id", "dataset"],
    "table_profiles": ["table_id"],
    "telegram_links": ["user_id"],
    "pending_codes": ["code"],
    "session_processor_state": ["processor_name", "session_file"],
    "session_extraction_state": ["session_file"],
    "usage_session_summary": ["session_file"],
    "usage_tool_daily": ["day", "tool_name", "source"],
    "usage_marketplace_item_daily": ["day", "source", "type", "parent_plugin", "name"],
    "usage_marketplace_item_window": ["period_label", "source", "type", "parent_plugin", "name"],
    "marketplace_plugins": ["marketplace_id", "name"],
    "user_store_installs": ["user_id", "entity_id"],
    "user_plugin_optouts": ["user_id", "marketplace_id", "plugin_name"],
    "knowledge_item_relations": ["item_a_id", "item_b_id", "relation_type"],
    "knowledge_votes": ["item_id", "user_id"],
    "knowledge_item_user_dismissed": ["user_id", "item_id"],
    "knowledge_item_domains": ["item_id", "domain_id"],
    "data_package_tables": ["package_id", "table_id"],
    "user_stack_subscriptions": ["user_id", "resource_type", "resource_id"],
}


# ---------------------------------------------------------------------------
# Task builder
# ---------------------------------------------------------------------------

def build_task_list() -> List[GenericCopyTask]:
    """Return migration tasks for every PG table, ordered by FK depth.

    Uses ``Base.metadata.sorted_tables`` for topological ordering.  Each
    table gets a :class:`GenericCopyTask`; tables in :data:`EXPLICIT_TASKS`
    use their registered override instead.
    """
    import src.models  # noqa: F401 — ensure all models are registered
    from src.db_pg import Base

    tasks: List[GenericCopyTask] = []
    for table in Base.metadata.sorted_tables:
        explicit = EXPLICIT_TASKS.get(table.name)
        if explicit is not None:
            tasks.append(explicit)
        else:
            pk_cols = _PK_COLUMNS.get(table.name, ["id"])
            tasks.append(GenericCopyTask(table_name=table.name, pk_columns=pk_cols))
    return tasks


# ---------------------------------------------------------------------------
# Public interface: all_table_names_handled
# ---------------------------------------------------------------------------

def all_table_names_handled() -> set[str]:
    """Names of PG tables this script can migrate.

    Used by
    ``tests/db_pg/test_data_migration.py::test_every_pg_model_has_a_migration_task``
    to catch new models added without migration coverage.  Returns the full
    set regardless of :data:`EXPLICIT_TASKS` — every table in
    ``Base.metadata`` is reachable (either via an explicit override or the
    generic copy loop).
    """
    import src.models  # noqa: F401
    from src.db_pg import Base

    return {t.name for t in Base.metadata.sorted_tables}


# ---------------------------------------------------------------------------
# Lazy TASKS list (backwards-compatible public attribute)
# ---------------------------------------------------------------------------

# Built once on first access via module-level assignment.  The existing tests
# do ``from scripts.migrate_duckdb_to_pg import TASKS`` and iterate it; the
# list must be fully populated at import time.
TASKS: List[GenericCopyTask] = build_task_list()


# ---------------------------------------------------------------------------
# Backwards-compatible shim functions (run_task / validate_task / run_all)
# ---------------------------------------------------------------------------

def run_task(
    task: GenericCopyTask,
    duck_conn: duckdb.DuckDBPyConnection,
    pg_engine: Engine,
    dry_run: bool = False,
) -> int:
    """Copy ``task.source_table`` from DuckDB into ``task.target_table`` in PG.

    Returns the number of rows considered.  ``ON CONFLICT (pk) DO NOTHING``
    may drop PK duplicates silently, so this is NOT the rows-inserted count.
    """
    return task.run(duck_conn, pg_engine, dry_run=dry_run)


def validate_task(
    task: GenericCopyTask,
    duck_conn: duckdb.DuckDBPyConnection,
    pg_engine: Engine,
) -> Dict[str, Any]:
    """Compare row counts + PK-set checksums between DuckDB and PG."""
    return task.validate(duck_conn, pg_engine)


def run_all(
    duck_conn: duckdb.DuckDBPyConnection,
    pg_engine: Engine,
    only: Optional[List[str]] = None,
    dry_run: bool = False,
    validate: bool = True,
) -> List[Dict[str, Any]]:
    """Run every registered task (or a subset by ``only``).

    Every task produces exactly one entry in the returned list:
    - On copy failure: ``{"table": ..., "error": ...}`` (copy exception caught).
    - On validate failure: ``{"table": ..., "error": ...}`` (validate exception caught).
    - On success with validate=True: full validate report (``duckdb_rows``,
      ``pg_rows``, ``checksum_match``, etc.).
    - On success with validate=False: ``{"table": ..., "ok": True}`` marker,
      so callers can count ``tables_migrated`` without validation overhead.

    The consistent per-task entry contract means callers like
    :func:`~scripts.db_state_migrator.copy_duckdb_to_pg` can always split
    reports into ``ok`` / ``err`` buckets by checking for an ``"error"`` key.
    """
    selected = [t for t in TASKS if not only or t.target_table in only]
    reports: List[Dict[str, Any]] = []
    for task in selected:
        try:
            run_task(task, duck_conn, pg_engine, dry_run=dry_run)
        except Exception as exc:
            log.exception("task %s failed: %s", task.source_table, exc)
            reports.append({"table": task.target_table, "error": str(exc)})
            continue
        if validate:
            try:
                reports.append(validate_task(task, duck_conn, pg_engine))
            except Exception as exc:
                log.exception("validate %s failed: %s", task.source_table, exc)
                reports.append({"table": task.target_table, "error": str(exc)})
        else:
            reports.append({"table": task.target_table, "ok": True})
    return reports
