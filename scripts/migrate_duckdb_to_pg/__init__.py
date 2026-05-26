"""One-shot DuckDB → Postgres data migration.

Usage:

    # Dry-run all tasks (no PG writes)
    python -m scripts.migrate_duckdb_to_pg --dry-run

    # Live migration of every registered table
    python -m scripts.migrate_duckdb_to_pg

    # Just one table, then validate
    python -m scripts.migrate_duckdb_to_pg --only users --validate

The framework is intentionally simple:

  - One ``MigrationTask`` per table.
  - ``run_task`` selects rows from DuckDB in batches and INSERTs into PG
    with ``ON CONFLICT DO NOTHING`` on the PK column so re-runs are
    idempotent.
  - ``validate_task`` returns row counts on both sides plus a checksum
    over the PK column (sorted) — bit-for-bit row equality is too noisy
    given timestamp precision differences, but PK-set equality + count
    parity is the strongest practical signal.

After cutover, the DuckDB ``system.duckdb`` file becomes a one-time
snapshot — never written to again. Analytics keep using their own
``analytics.duckdb`` and ``extract.duckdb`` files, unaffected.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

import duckdb
import sqlalchemy as sa
from sqlalchemy.engine import Engine


log = logging.getLogger(__name__)


@dataclass
class MigrationTask:
    """One DuckDB → PG table copy.

    Attributes:
      source_table: DuckDB table name (usually identical to target_table).
      target_table: Postgres table name (Alembic-managed).
      pk_columns: PK columns used for ``ON CONFLICT DO NOTHING`` clause.
      columns: Ordered list of columns to copy. None = all columns.
      transform: Optional per-row dict transformer (for JSON columns
        that need json.dumps/loads, type widening, etc).
      batch_size: Rows per INSERT batch. Tuned for Cloud SQL throughput.
    """
    source_table: str
    target_table: str
    pk_columns: Sequence[str]
    columns: Optional[Sequence[str]] = None
    transform: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
    batch_size: int = 500


def _resolved_columns(task: MigrationTask, duck_conn: duckdb.DuckDBPyConnection) -> List[str]:
    if task.columns:
        return list(task.columns)
    rows = duck_conn.execute(
        f"SELECT column_name FROM information_schema.columns "
        f"WHERE table_name = '{task.source_table}' AND table_schema = 'main' "
        f"ORDER BY ordinal_position"
    ).fetchall()
    if rows:
        return [r[0] for r in rows]
    pragma = duck_conn.execute(f"PRAGMA table_info('{task.source_table}')").fetchall()
    return [r[1] for r in pragma]


def _pg_columns(pg_engine: Engine, table: str) -> List[str]:
    """Return the column list for ``table`` from Postgres' information_schema.

    Used to intersect DuckDB's column list with PG's before building the
    INSERT — required when prod DuckDB has columns the local PG schema
    hasn't migrated yet (e.g. forward-evolved ``table_registry.bq_fqn``
    / ``store_entities.title`` that exist in prod but not in any alembic
    revision on this branch). Without filtering, the INSERT references
    nonexistent PG columns and the whole task aborts.
    """
    import sqlalchemy as sa
    with pg_engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        ).all()
    return [r[0] for r in rows]


def _filtered_columns(
    task: MigrationTask,
    duck_conn: duckdb.DuckDBPyConnection,
    pg_engine: Engine,
) -> List[str]:
    """DuckDB columns intersected with PG columns, preserving DuckDB order.

    Logs a one-line warning when prod has columns PG doesn't — operator
    sees the schema drift surface without the task failing on it.
    """
    duck_cols = _resolved_columns(task, duck_conn)
    pg_cols = set(_pg_columns(pg_engine, task.target_table))
    skipped = [c for c in duck_cols if c not in pg_cols]
    if skipped:
        log.warning(
            "%s: DuckDB columns absent in PG schema, dropped: %s",
            task.source_table,
            ", ".join(skipped),
        )
    return [c for c in duck_cols if c in pg_cols]


def _audit_log_transform(row: Dict[str, Any]) -> Dict[str, Any]:
    """No-op — JSON columns are CAST(... AS JSONB) at INSERT time."""
    return row


def _backfill_timestamps_transform(row: Dict[str, Any]) -> Dict[str, Any]:
    """Backfill NULL ``created_at`` / ``updated_at`` with each other or
    ``CURRENT_TIMESTAMP``.

    DuckDB-side rows from prod sometimes carry a NULL ``created_at``
    (system-seeded ``marketplace_plugins`` rows where the ingester
    only filled ``updated_at``). PG schema declares both NOT NULL with a
    ``CURRENT_TIMESTAMP`` server default — but the server default only
    fires when the column is *omitted* from the INSERT. The migrate
    path always binds every column, so a NULL passes through and the
    NOT NULL check fires.

    Backfill order:
      - ``created_at`` NULL → take ``updated_at`` if non-null, else now.
      - ``updated_at`` NULL → take ``created_at`` (post-backfill).

    Applied to ``marketplace_plugins`` (where the failure was observed)
    via the ``transform=`` slot on its ``MigrationTask``.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    created = row.get("created_at")
    updated = row.get("updated_at")
    if created is None:
        row["created_at"] = updated if updated is not None else now
    if row.get("updated_at") is None:
        row["updated_at"] = row["created_at"]
    return row


TASKS: List[MigrationTask] = [
    MigrationTask(
        source_table="audit_log",
        target_table="audit_log",
        pk_columns=["id"],
        transform=_audit_log_transform,
    ),
    MigrationTask(
        source_table="users",
        target_table="users",
        pk_columns=["id"],
    ),
    MigrationTask(
        source_table="user_groups",
        target_table="user_groups",
        pk_columns=["id"],
    ),
    MigrationTask(
        source_table="user_group_members",
        target_table="user_group_members",
        pk_columns=["user_id", "group_id"],
    ),
    MigrationTask(
        source_table="resource_grants",
        target_table="resource_grants",
        pk_columns=["id"],
    ),
    MigrationTask(
        source_table="table_registry",
        target_table="table_registry",
        pk_columns=["id"],
    ),
    MigrationTask(
        source_table="sync_state",
        target_table="sync_state",
        pk_columns=["table_id"],
    ),
    MigrationTask(
        source_table="sync_history",
        target_table="sync_history",
        pk_columns=["id"],
    ),
    MigrationTask(
        source_table="metric_definitions",
        target_table="metric_definitions",
        pk_columns=["id"],
    ),
    MigrationTask(
        source_table="instance_templates",
        target_table="instance_templates",
        pk_columns=["key"],
    ),
    MigrationTask(
        source_table="personal_access_tokens",
        target_table="personal_access_tokens",
        pk_columns=["id"],
    ),
    MigrationTask(
        source_table="view_ownership",
        target_table="view_ownership",
        pk_columns=["view_name"],
    ),
    MigrationTask(
        source_table="column_metadata",
        target_table="column_metadata",
        pk_columns=["table_id", "column_name"],
    ),
    MigrationTask(
        source_table="bq_metadata_cache",
        target_table="bq_metadata_cache",
        pk_columns=["table_id"],
    ),
    MigrationTask(
        source_table="user_sync_settings",
        target_table="user_sync_settings",
        pk_columns=["user_id", "dataset"],
    ),
    # misc cluster
    MigrationTask(
        source_table="table_profiles",
        target_table="table_profiles",
        pk_columns=["table_id"],
    ),
    MigrationTask(
        source_table="telegram_links",
        target_table="telegram_links",
        pk_columns=["user_id"],
    ),
    MigrationTask(
        source_table="pending_codes",
        target_table="pending_codes",
        pk_columns=["code"],
    ),
    MigrationTask(
        source_table="script_registry",
        target_table="script_registry",
        pk_columns=["id"],
    ),
    MigrationTask(
        source_table="news_template",
        target_table="news_template",
        pk_columns=["id"],
    ),
    # telemetry cluster
    MigrationTask(
        source_table="session_processor_state",
        target_table="session_processor_state",
        pk_columns=["processor_name", "session_file"],
    ),
    MigrationTask(
        source_table="user_observability_views",
        target_table="user_observability_views",
        pk_columns=["id"],
    ),
    MigrationTask(
        source_table="usage_events",
        target_table="usage_events",
        pk_columns=["id"],
    ),
    MigrationTask(
        source_table="usage_session_summary",
        target_table="usage_session_summary",
        pk_columns=["session_file"],
    ),
    MigrationTask(
        source_table="usage_tool_daily",
        target_table="usage_tool_daily",
        pk_columns=["day", "tool_name", "source"],
    ),
    MigrationTask(
        source_table="usage_marketplace_item_daily",
        target_table="usage_marketplace_item_daily",
        pk_columns=["day", "source", "type", "parent_plugin", "name"],
    ),
    MigrationTask(
        source_table="usage_marketplace_item_window",
        target_table="usage_marketplace_item_window",
        pk_columns=["period_label", "source", "type", "parent_plugin", "name"],
    ),
    # store cluster
    MigrationTask(
        source_table="marketplace_registry",
        target_table="marketplace_registry",
        pk_columns=["id"],
    ),
    MigrationTask(
        source_table="marketplace_plugins",
        target_table="marketplace_plugins",
        pk_columns=["marketplace_id", "name"],
        transform=_backfill_timestamps_transform,
    ),
    MigrationTask(
        source_table="store_entities",
        target_table="store_entities",
        pk_columns=["id"],
    ),
    MigrationTask(
        source_table="user_store_installs",
        target_table="user_store_installs",
        pk_columns=["user_id", "entity_id"],
    ),
    MigrationTask(
        source_table="user_plugin_optouts",
        target_table="user_plugin_optouts",
        pk_columns=["user_id", "marketplace_id", "plugin_name"],
    ),
    MigrationTask(
        source_table="store_submissions",
        target_table="store_submissions",
        pk_columns=["id"],
    ),
    # knowledge cluster
    MigrationTask(
        source_table="knowledge_items",
        target_table="knowledge_items",
        pk_columns=["id"],
    ),
    MigrationTask(
        source_table="knowledge_contradictions",
        target_table="knowledge_contradictions",
        pk_columns=["id"],
    ),
    MigrationTask(
        source_table="knowledge_item_relations",
        target_table="knowledge_item_relations",
        pk_columns=["item_a_id", "item_b_id", "relation_type"],
    ),
    MigrationTask(
        source_table="verification_evidence",
        target_table="verification_evidence",
        pk_columns=["id"],
    ),
    MigrationTask(
        source_table="knowledge_votes",
        target_table="knowledge_votes",
        pk_columns=["item_id", "user_id"],
    ),
    MigrationTask(
        source_table="knowledge_item_user_dismissed",
        target_table="knowledge_item_user_dismissed",
        pk_columns=["user_id", "item_id"],
    ),
]


_JSON_COLUMNS = {
    # (table, column) → cast to JSONB in PG INSERT
    ("audit_log", "params"),
    ("audit_log", "params_before"),
    ("metric_definitions", "sql_variants"),
    ("metric_definitions", "validation"),
    ("bq_metadata_cache", "clustered_by"),
    ("bq_metadata_cache", "known_columns"),
    ("user_sync_settings", "tables"),
    ("table_profiles", "profile"),
    ("user_observability_views", "query_json"),
    ("usage_events", "friction_tags"),
    ("marketplace_plugins", "source_spec"),
    ("marketplace_plugins", "raw"),
    ("marketplace_plugins", "doc_links"),
    ("store_entities", "doc_paths"),
    ("store_entities", "version_history"),
    ("store_submissions", "inline_checks"),
    ("store_submissions", "llm_findings"),
    ("knowledge_items", "tags"),
    ("knowledge_items", "contributors"),
    ("knowledge_items", "entities"),
}


def _build_insert(task: MigrationTask, columns: Sequence[str]) -> str:
    placeholders: List[str] = []
    for c in columns:
        if (task.target_table, c) in _JSON_COLUMNS:
            placeholders.append(f"CAST(:{c} AS JSONB)")
        else:
            placeholders.append(f":{c}")
    col_list = ", ".join(columns)
    val_list = ", ".join(placeholders)
    conflict = ", ".join(task.pk_columns)
    return (
        f"INSERT INTO {task.target_table} ({col_list}) "
        f"VALUES ({val_list}) "
        f"ON CONFLICT ({conflict}) DO NOTHING"
    )


def _normalize_for_pg(value: Any) -> Any:
    """Bridge DuckDB-native types that psycopg can't bind directly.

    DuckDB returns JSON columns as Python dict/list. PG's CAST(... AS JSONB)
    expects text; re-encode here.
    """
    import json as _json
    if isinstance(value, (dict, list)):
        return _json.dumps(value)
    return value


def run_task(
    task: MigrationTask,
    duck_conn: duckdb.DuckDBPyConnection,
    pg_engine: Engine,
    dry_run: bool = False,
) -> int:
    """Copy ``task.source_table`` from DuckDB into ``task.target_table`` in PG.

    Returns the number of rows considered. ``ON CONFLICT DO NOTHING`` may
    drop duplicates silently, so this is NOT the rows-inserted count.
    """
    # Filter through PG's column list so DuckDB-side forward-evolved
    # columns (prod-only fields not yet in alembic) don't cause the
    # INSERT to reference nonexistent PG columns.
    columns = _filtered_columns(task, duck_conn, pg_engine)
    if not columns:
        log.warning(
            "%s: no overlapping columns between DuckDB and PG; skipping",
            task.source_table,
        )
        return 0
    log.info("migrate %s (%d cols, dry_run=%s)", task.source_table, len(columns), dry_run)

    select_sql = f"SELECT {', '.join(columns)} FROM {task.source_table}"
    rows = duck_conn.execute(select_sql).fetchall()
    if not rows:
        log.info("  empty source; nothing to do")
        return 0

    insert_sql = _build_insert(task, columns)
    considered = 0
    batch: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(zip(columns, r))
        if task.transform:
            d = task.transform(d)
        d = {k: _normalize_for_pg(v) for k, v in d.items()}
        batch.append(d)
        considered += 1
        if len(batch) >= task.batch_size and not dry_run:
            with pg_engine.begin() as conn:
                conn.execute(sa.text(insert_sql), batch)
            batch.clear()
    if batch and not dry_run:
        with pg_engine.begin() as conn:
            conn.execute(sa.text(insert_sql), batch)

    log.info("  considered %d rows%s", considered, " (dry-run)" if dry_run else "")
    return considered


def _checksum(values: Sequence[Sequence[Any]]) -> str:
    """Stable digest over a sorted list of PK tuples."""
    h = hashlib.sha256()
    for v in sorted(tuple(str(x) for x in row) for row in values):
        for item in v:
            h.update(item.encode("utf-8"))
            h.update(b"|")
        h.update(b"\n")
    return h.hexdigest()


def validate_task(
    task: MigrationTask,
    duck_conn: duckdb.DuckDBPyConnection,
    pg_engine: Engine,
) -> Dict[str, Any]:
    """Compare row counts + PK-set checksums between DuckDB and PG."""
    pk_select = ", ".join(task.pk_columns)
    duck_rows = duck_conn.execute(
        f"SELECT {pk_select} FROM {task.source_table}"
    ).fetchall()
    duck_count = len(duck_rows)

    with pg_engine.connect() as conn:
        pg_rows = conn.execute(
            sa.text(f"SELECT {pk_select} FROM {task.target_table}")
        ).all()
    pg_count = len(pg_rows)

    return {
        "table": task.target_table,
        "duckdb_rows": duck_count,
        "pg_rows": pg_count,
        "checksum_match": (
            duck_count == pg_count and _checksum(duck_rows) == _checksum(pg_rows)
        ),
    }


def run_all(
    duck_conn: duckdb.DuckDBPyConnection,
    pg_engine: Engine,
    only: Optional[List[str]] = None,
    dry_run: bool = False,
    validate: bool = True,
) -> List[Dict[str, Any]]:
    """Run every registered task (or a subset by ``only``)."""
    selected = [t for t in TASKS if not only or t.target_table in only]
    reports: List[Dict[str, Any]] = []
    for task in selected:
        try:
            run_task(task, duck_conn, pg_engine, dry_run=dry_run)
        except Exception as exc:  # pragma: no cover — logged for operator
            log.exception("task %s failed: %s", task.source_table, exc)
            reports.append({"table": task.target_table, "error": str(exc)})
            continue
        if validate:
            reports.append(validate_task(task, duck_conn, pg_engine))
    return reports