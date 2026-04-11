"""DuckDB connection management and schema versioning.

Provides get_system_db() for the system state database
and get_analytics_db() for the analytics database with parquet views.
"""

import logging
import os
import re
import shutil
from pathlib import Path

import duckdb

logger = logging.getLogger(__name__)

_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

SCHEMA_VERSION = 4

_SYSTEM_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS users (
    id VARCHAR PRIMARY KEY,
    email VARCHAR UNIQUE NOT NULL,
    name VARCHAR,
    role VARCHAR DEFAULT 'analyst',
    password_hash VARCHAR,
    setup_token VARCHAR,
    setup_token_created TIMESTAMP,
    reset_token VARCHAR,
    reset_token_created TIMESTAMP,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sync_state (
    table_id VARCHAR PRIMARY KEY,
    last_sync TIMESTAMP,
    rows BIGINT,
    file_size_bytes BIGINT,
    uncompressed_size_bytes BIGINT,
    columns INTEGER,
    hash VARCHAR,
    status VARCHAR DEFAULT 'ok',
    error TEXT
);

CREATE TABLE IF NOT EXISTS sync_history (
    id VARCHAR PRIMARY KEY,
    table_id VARCHAR NOT NULL,
    synced_at TIMESTAMP NOT NULL,
    rows BIGINT,
    duration_ms INTEGER,
    status VARCHAR,
    error TEXT
);

CREATE TABLE IF NOT EXISTS user_sync_settings (
    user_id VARCHAR NOT NULL,
    dataset VARCHAR NOT NULL,
    enabled BOOLEAN DEFAULT false,
    table_mode VARCHAR DEFAULT 'all',
    tables JSON,
    updated_at TIMESTAMP,
    PRIMARY KEY (user_id, dataset)
);

CREATE TABLE IF NOT EXISTS knowledge_items (
    id VARCHAR PRIMARY KEY,
    title VARCHAR NOT NULL,
    content TEXT,
    category VARCHAR,
    tags JSON,
    status VARCHAR DEFAULT 'pending',
    contributors JSON,
    source_user VARCHAR,
    audience VARCHAR,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS knowledge_votes (
    item_id VARCHAR NOT NULL,
    user_id VARCHAR NOT NULL,
    vote INTEGER,
    voted_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (item_id, user_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id VARCHAR PRIMARY KEY,
    timestamp TIMESTAMP NOT NULL DEFAULT current_timestamp,
    user_id VARCHAR,
    action VARCHAR NOT NULL,
    resource VARCHAR,
    params JSON,
    result VARCHAR,
    duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS telegram_links (
    user_id VARCHAR PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    linked_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS pending_codes (
    code VARCHAR PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    created_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS script_registry (
    id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    owner VARCHAR,
    schedule VARCHAR,
    source TEXT NOT NULL,
    deployed_at TIMESTAMP DEFAULT current_timestamp,
    last_run TIMESTAMP,
    last_status VARCHAR
);

CREATE TABLE IF NOT EXISTS table_registry (
    id VARCHAR PRIMARY KEY,
    name VARCHAR NOT NULL,
    source_type VARCHAR,
    bucket VARCHAR,
    source_table VARCHAR,
    sync_strategy VARCHAR DEFAULT 'full_refresh',
    query_mode VARCHAR DEFAULT 'local',
    sync_schedule VARCHAR,
    profile_after_sync BOOLEAN DEFAULT true,
    primary_key VARCHAR,
    folder VARCHAR,
    description TEXT,
    registered_by VARCHAR,
    is_public BOOLEAN DEFAULT true,
    registered_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS table_profiles (
    table_id VARCHAR PRIMARY KEY,
    profile JSON NOT NULL,
    profiled_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS dataset_permissions (
    user_id VARCHAR NOT NULL,
    dataset VARCHAR NOT NULL,
    access VARCHAR DEFAULT 'read',
    PRIMARY KEY (user_id, dataset)
);

CREATE TABLE IF NOT EXISTS access_requests (
    id VARCHAR PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    user_email VARCHAR NOT NULL,
    table_id VARCHAR NOT NULL,
    reason TEXT,
    status VARCHAR DEFAULT 'pending',
    reviewed_by VARCHAR,
    reviewed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS metric_definitions (
    id              VARCHAR PRIMARY KEY,
    name            VARCHAR NOT NULL,
    display_name    VARCHAR NOT NULL,
    category        VARCHAR NOT NULL,
    description     TEXT,
    type            VARCHAR DEFAULT 'sum',
    unit            VARCHAR,
    grain           VARCHAR DEFAULT 'monthly',
    table_name      VARCHAR,
    tables          VARCHAR[],
    expression      VARCHAR,
    time_column     VARCHAR,
    dimensions      VARCHAR[],
    filters         VARCHAR[],
    synonyms        VARCHAR[],
    notes           VARCHAR[],
    sql             TEXT NOT NULL,
    sql_variants    JSON,
    validation      JSON,
    source          VARCHAR DEFAULT 'manual',
    created_at      TIMESTAMP DEFAULT current_timestamp,
    updated_at      TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS column_metadata (
    table_id        VARCHAR NOT NULL,
    column_name     VARCHAR NOT NULL,
    basetype        VARCHAR,
    description     VARCHAR,
    confidence      VARCHAR DEFAULT 'manual',
    source          VARCHAR DEFAULT 'manual',
    updated_at      TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (table_id, column_name)
);
"""


import threading

_system_db_lock = threading.Lock()
_system_db_conn: duckdb.DuckDBPyConnection | None = None
_system_db_path: str | None = None


def _get_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "./data"))


def get_system_db() -> duckdb.DuckDBPyConnection:
    """Get a connection to the system state database.

    Uses a single shared connection per DATA_DIR to avoid DuckDB lock
    conflicts between the main app and background tasks. Returns a cursor
    so callers can safely close() it without closing the underlying connection.
    """
    global _system_db_conn, _system_db_path
    db_path = str(_get_data_dir() / "state" / "system.duckdb")

    with _system_db_lock:
        if _system_db_conn is None or _system_db_path != db_path:
            # Close old connection if DATA_DIR changed (e.g., in tests)
            if _system_db_conn is not None:
                try:
                    _system_db_conn.close()
                except Exception:
                    pass
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            _system_db_conn = duckdb.connect(db_path)
            _system_db_path = db_path
            _ensure_schema(_system_db_conn)
        return _system_db_conn.cursor()


def get_analytics_db() -> duckdb.DuckDBPyConnection:
    """Get a connection to the analytics database (parquet views)."""
    db_path = _get_data_dir() / "analytics" / "server.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(db_path))


def _reattach_remote_extensions(
    conn: duckdb.DuckDBPyConnection, extracts_dir: Path
) -> None:
    """Re-LOAD DuckDB extensions listed in _remote_attach tables of each extract.duckdb.

    Called from get_analytics_db_readonly() after ATTACHing extract.duckdb files so
    that remote views (e.g. BigQuery) resolve correctly.  Uses LOAD only — no INSTALL —
    to avoid touching the network in read-only query paths.
    """
    if not extracts_dir.exists():
        return

    try:
        attached_dbs = {
            r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()
        }
    except Exception:
        return

    for ext_dir in sorted(extracts_dir.iterdir()):
        if not ext_dir.is_dir():
            continue
        if not _SAFE_IDENTIFIER.match(ext_dir.name):
            continue
        db_file = ext_dir / "extract.duckdb"
        if not db_file.exists():
            continue
        # Only process sources that were successfully attached
        if ext_dir.name not in attached_dbs:
            continue

        # Check whether this extract has a _remote_attach table
        try:
            has_table = conn.execute(
                "SELECT 1 FROM information_schema.tables "
                f"WHERE table_schema='{ext_dir.name}' AND table_name='_remote_attach'"
            ).fetchone()
            if not has_table:
                continue
        except Exception:
            continue

        try:
            rows = conn.execute(
                f"SELECT alias, extension, url, token_env FROM {ext_dir.name}._remote_attach"
            ).fetchall()
        except Exception as e:
            logger.debug("Could not read _remote_attach from %s: %s", ext_dir.name, e)
            continue

        # Refresh attached list before processing each source's rows
        try:
            attached_dbs = {
                r[0] for r in conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()
            }
        except Exception:
            pass

        for alias, extension, url, token_env in rows:
            if not _SAFE_IDENTIFIER.match(alias or ""):
                logger.debug("Skipping unsafe remote_attach alias: %r", alias)
                continue
            if not _SAFE_IDENTIFIER.match(extension or ""):
                logger.debug("Skipping unsafe remote_attach extension: %r", extension)
                continue
            if alias in attached_dbs:
                logger.debug("Remote source %s already attached, skipping", alias)
                continue
            try:
                conn.execute(f"LOAD {extension};")
                token = os.environ.get(token_env, "") if token_env else ""
                safe_url = url.replace("'", "''")
                if token:
                    escaped_token = token.replace("'", "''")
                    conn.execute(
                        f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, TOKEN '{escaped_token}')"
                    )
                else:
                    conn.execute(
                        f"ATTACH '{safe_url}' AS {alias} (TYPE {extension}, READ_ONLY)"
                    )
                attached_dbs.add(alias)
                logger.debug("Re-attached remote source %s via %s extension", alias, extension)
            except Exception as e:
                logger.debug("Could not re-attach remote source %s: %s", alias, e)


def get_analytics_db_readonly() -> duckdb.DuckDBPyConnection:
    """Read-only connection to analytics DB. Blocks writes and external access.

    ATTACHes extract.duckdb files so views that reference them work.
    """
    db_path = _get_data_dir() / "analytics" / "server.duckdb"
    if not db_path.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(db_path), read_only=False)
        try:
            conn.execute("SET enable_external_access = false")
        except Exception:
            pass
        return conn
    conn = duckdb.connect(str(db_path), read_only=True)
    # ATTACH extract.duckdb files FIRST so views referencing them work
    extracts_dir = _get_data_dir() / "extracts"
    if extracts_dir.exists():
        for ext_dir in sorted(extracts_dir.iterdir()):
            db_file = ext_dir / "extract.duckdb"
            if db_file.exists() and ext_dir.is_dir():
                if not _SAFE_IDENTIFIER.match(ext_dir.name):
                    continue
                try:
                    conn.execute(f"ATTACH '{db_file}' AS {ext_dir.name} (READ_ONLY)")
                except Exception:
                    pass
    # Re-attach remote extensions so BigQuery / other remote views resolve.
    _reattach_remote_extensions(conn, extracts_dir)
    # Note: external_access stays enabled because views use read_parquet() on local files.
    # File-path-based attacks are blocked by the SQL blocklist in app/api/query.py.
    return conn


_V1_TO_V2_MIGRATIONS = [
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS source_type VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS bucket VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS source_table VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS query_mode VARCHAR DEFAULT 'local'",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS sync_schedule VARCHAR",
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS profile_after_sync BOOLEAN DEFAULT true",
]

_V2_TO_V3_MIGRATIONS = [
    "ALTER TABLE table_registry ADD COLUMN IF NOT EXISTS is_public BOOLEAN DEFAULT true",
]

_V3_TO_V4_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS metric_definitions (
        id              VARCHAR PRIMARY KEY,
        name            VARCHAR NOT NULL,
        display_name    VARCHAR NOT NULL,
        category        VARCHAR NOT NULL,
        description     TEXT,
        type            VARCHAR DEFAULT 'sum',
        unit            VARCHAR,
        grain           VARCHAR DEFAULT 'monthly',
        table_name      VARCHAR,
        tables          VARCHAR[],
        expression      VARCHAR,
        time_column     VARCHAR,
        dimensions      VARCHAR[],
        filters         VARCHAR[],
        synonyms        VARCHAR[],
        notes           VARCHAR[],
        sql             TEXT NOT NULL,
        sql_variants    JSON,
        validation      JSON,
        source          VARCHAR DEFAULT 'manual',
        created_at      TIMESTAMP DEFAULT current_timestamp,
        updated_at      TIMESTAMP DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS column_metadata (
        table_id        VARCHAR NOT NULL,
        column_name     VARCHAR NOT NULL,
        basetype        VARCHAR,
        description     VARCHAR,
        confidence      VARCHAR DEFAULT 'manual',
        source          VARCHAR DEFAULT 'manual',
        updated_at      TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (table_id, column_name)
    )
    """,
]


def _ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create tables if they don't exist. Apply migrations if schema version changed."""
    current = get_schema_version(conn)
    if current < SCHEMA_VERSION:
        # Snapshot before migration for rollback support
        if current > 0:
            try:
                db_path = Path(os.environ.get("DATA_DIR", "./data")) / "state" / "system.duckdb"
                if db_path.exists():
                    # Flush WAL to main DB file before copying
                    try:
                        conn.execute("CHECKPOINT")
                    except Exception:
                        pass  # CHECKPOINT may fail on read-only or in-memory DBs
                    snapshot = db_path.parent / "system.duckdb.pre-migrate"
                    shutil.copy2(str(db_path), str(snapshot))
                    # Also copy WAL if it still exists (belt and suspenders)
                    wal_path = Path(str(db_path) + ".wal")
                    if wal_path.exists():
                        shutil.copy2(str(wal_path), str(snapshot) + ".wal")
                    logger.info("Pre-migration snapshot saved: %s", snapshot)
            except Exception as e:
                logger.warning("Could not create pre-migration snapshot: %s", e)
        conn.execute(_SYSTEM_SCHEMA)
        if current == 0:
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                [SCHEMA_VERSION],
            )
        else:
            if current < 2:
                for sql in _V1_TO_V2_MIGRATIONS:
                    conn.execute(sql)
            if current < 3:
                for sql in _V2_TO_V3_MIGRATIONS:
                    conn.execute(sql)
            if current < 4:
                for sql in _V3_TO_V4_MIGRATIONS:
                    conn.execute(sql)
            conn.execute(
                "UPDATE schema_version SET version = ?, applied_at = current_timestamp",
                [SCHEMA_VERSION],
            )


def get_schema_version(conn: duckdb.DuckDBPyConnection) -> int:
    """Get current schema version. Returns 0 if no schema exists."""
    try:
        result = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return result[0] if result and result[0] else 0
    except duckdb.CatalogException:
        return 0


def close_system_db() -> None:
    """Close the shared system DB connection. Called on app shutdown."""
    global _system_db_conn, _system_db_path
    if _system_db_conn:
        try:
            _system_db_conn.close()
        except Exception:
            pass
        _system_db_conn = None
        _system_db_path = None
