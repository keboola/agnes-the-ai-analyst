"""DuckDB connection management and schema initialization.

Provides connections to the system state database and analytics database,
with automatic directory creation and schema bootstrapping.
"""
import os
from pathlib import Path

import duckdb

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          VARCHAR PRIMARY KEY,
    timestamp   TIMESTAMP DEFAULT current_timestamp,
    actor       VARCHAR,
    action      VARCHAR NOT NULL,
    entity_type VARCHAR,
    entity_id   VARCHAR,
    details     JSON
);

CREATE TABLE IF NOT EXISTS dataset_permissions (
    id          VARCHAR PRIMARY KEY,
    user_email  VARCHAR NOT NULL,
    dataset     VARCHAR NOT NULL,
    permission  VARCHAR NOT NULL DEFAULT 'read',
    granted_by  VARCHAR,
    granted_at  TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS knowledge_items (
    id          VARCHAR PRIMARY KEY,
    title       VARCHAR NOT NULL,
    content     VARCHAR,
    category    VARCHAR,
    author      VARCHAR,
    status      VARCHAR DEFAULT 'active',
    metadata    JSON,
    created_at  TIMESTAMP DEFAULT current_timestamp,
    updated_at  TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS knowledge_votes (
    id          VARCHAR PRIMARY KEY,
    item_id     VARCHAR NOT NULL,
    user_email  VARCHAR NOT NULL,
    vote        INTEGER NOT NULL,
    created_at  TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS pending_codes (
    code        VARCHAR PRIMARY KEY,
    user_email  VARCHAR NOT NULL,
    purpose     VARCHAR,
    created_at  TIMESTAMP DEFAULT current_timestamp,
    expires_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS script_registry (
    id          VARCHAR PRIMARY KEY,
    name        VARCHAR NOT NULL,
    path        VARCHAR NOT NULL,
    description VARCHAR,
    author      VARCHAR,
    metadata    JSON,
    created_at  TIMESTAMP DEFAULT current_timestamp,
    updated_at  TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS sync_history (
    id          VARCHAR PRIMARY KEY,
    table_name  VARCHAR NOT NULL,
    status      VARCHAR NOT NULL,
    rows_synced INTEGER,
    started_at  TIMESTAMP DEFAULT current_timestamp,
    finished_at TIMESTAMP,
    error       VARCHAR,
    metadata    JSON
);

CREATE TABLE IF NOT EXISTS sync_state (
    table_name  VARCHAR PRIMARY KEY,
    last_sync   TIMESTAMP,
    status      VARCHAR DEFAULT 'pending',
    row_count   INTEGER,
    file_hash   VARCHAR,
    metadata    JSON
);

CREATE TABLE IF NOT EXISTS table_profiles (
    table_name  VARCHAR PRIMARY KEY,
    profile     JSON,
    profiled_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS table_registry (
    table_name  VARCHAR PRIMARY KEY,
    bucket      VARCHAR,
    source      VARCHAR,
    sync_strategy VARCHAR DEFAULT 'full',
    primary_key VARCHAR,
    description VARCHAR,
    metadata    JSON,
    registered_at TIMESTAMP DEFAULT current_timestamp,
    updated_at  TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS telegram_links (
    chat_id     VARCHAR PRIMARY KEY,
    user_email  VARCHAR NOT NULL,
    linked_at   TIMESTAMP DEFAULT current_timestamp,
    active      BOOLEAN DEFAULT true
);

CREATE TABLE IF NOT EXISTS user_sync_settings (
    user_email  VARCHAR PRIMARY KEY,
    settings    JSON,
    updated_at  TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS users (
    email       VARCHAR PRIMARY KEY,
    name        VARCHAR,
    picture     VARCHAR,
    role        VARCHAR DEFAULT 'analyst',
    is_active   BOOLEAN DEFAULT true,
    metadata    JSON,
    created_at  TIMESTAMP DEFAULT current_timestamp,
    last_login  TIMESTAMP
);
"""


def _get_data_dir() -> Path:
    """Return the DATA_DIR path, defaulting to ./data."""
    return Path(os.environ.get("DATA_DIR", "data"))


def get_system_db() -> duckdb.DuckDBPyConnection:
    """Open (or create) the system state database and ensure schema exists.

    Returns a DuckDB connection to {DATA_DIR}/state/system.duckdb.
    Creates directories and all schema tables on first call.
    """
    db_dir = _get_data_dir() / "state"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "system.duckdb"

    conn = duckdb.connect(str(db_path))
    conn.execute(_SCHEMA_SQL)

    # Seed schema_version if empty
    row = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
    if row[0] == 0:
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)", [SCHEMA_VERSION]
        )

    return conn


def get_analytics_db() -> duckdb.DuckDBPyConnection:
    """Open (or create) the analytics database.

    Returns a DuckDB connection to {DATA_DIR}/analytics/server.duckdb.
    Creates directories if needed.
    """
    db_dir = _get_data_dir() / "analytics"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "server.duckdb"

    return duckdb.connect(str(db_path))


def get_schema_version(conn: duckdb.DuckDBPyConnection) -> int:
    """Return the current schema version, or 0 if no schema_version table."""
    try:
        row = conn.execute(
            "SELECT version FROM schema_version ORDER BY applied_at DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else 0
    except duckdb.CatalogException:
        return 0
