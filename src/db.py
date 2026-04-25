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

SCHEMA_VERSION = 10

_SYSTEM_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at TIMESTAMP DEFAULT current_timestamp
);

-- v9: role assignments moved to user_role_grants (direct grants) and
-- group_mappings (Cloud Identity group → role). The four legacy values
-- (viewer/analyst/km_admin/admin) are seeded as core.* internal_roles with
-- an implies hierarchy and granted to existing users by the v8→v9 backfill —
-- see _seed_core_roles + _backfill_users_role_to_grants.
--
-- DEPRECATED v9: users.role column kept as NULL-able legacy artifact because
-- DuckDB rejects DROP COLUMN while a FK (user_role_grants.user_id → users.id)
-- references the table. UserRepository ignores it on reads + writes; the
-- column will be physically dropped in a future major release once DuckDB
-- ALTER w/ FK support stabilizes (or via a planned table-rebuild migration).
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR PRIMARY KEY,
    email VARCHAR UNIQUE NOT NULL,
    name VARCHAR,
    role VARCHAR,
    password_hash VARCHAR,
    setup_token VARCHAR,
    setup_token_created TIMESTAMP,
    reset_token VARCHAR,
    reset_token_created TIMESTAMP,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    deactivated_at TIMESTAMP,
    deactivated_by VARCHAR,
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
    confidence DOUBLE,
    domain VARCHAR,
    entities JSON,
    source_type VARCHAR DEFAULT 'claude_local_md',
    source_ref VARCHAR,
    valid_from TIMESTAMP,
    valid_until TIMESTAMP,
    supersedes VARCHAR,
    sensitivity VARCHAR DEFAULT 'internal',
    is_personal BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT current_timestamp,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS knowledge_contradictions (
    id VARCHAR PRIMARY KEY,
    item_a_id VARCHAR NOT NULL,
    item_b_id VARCHAR NOT NULL,
    explanation TEXT,
    severity VARCHAR,
    suggested_resolution TEXT,
    resolved BOOLEAN DEFAULT FALSE,
    resolved_by VARCHAR,
    resolved_at TIMESTAMP,
    resolution VARCHAR,
    detected_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS session_extraction_state (
    session_file VARCHAR PRIMARY KEY,
    username VARCHAR NOT NULL,
    processed_at TIMESTAMP DEFAULT current_timestamp,
    items_extracted INTEGER DEFAULT 0,
    file_hash VARCHAR
);

CREATE TABLE IF NOT EXISTS verification_evidence (
    id VARCHAR PRIMARY KEY,
    item_id VARCHAR NOT NULL,
    source_user VARCHAR,
    source_ref VARCHAR,
    detection_type VARCHAR,
    user_quote TEXT,
    created_at TIMESTAMP DEFAULT current_timestamp
);

CREATE INDEX IF NOT EXISTS idx_verification_evidence_item ON verification_evidence(item_id);

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

CREATE TABLE IF NOT EXISTS personal_access_tokens (
    id           VARCHAR PRIMARY KEY,
    user_id      VARCHAR NOT NULL,
    name         VARCHAR NOT NULL,
    token_hash   VARCHAR NOT NULL,
    prefix       VARCHAR NOT NULL,
    scopes       VARCHAR,
    created_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
    expires_at   TIMESTAMP,
    last_used_at TIMESTAMP,
    last_used_ip VARCHAR,
    revoked_at   TIMESTAMP
);

-- Internal roles: app-defined capabilities (e.g. 'context_admin', 'agent_operator').
-- `key` is the immutable lower_snake_case identifier referenced from code; modules
-- self-register their roles on import and the startup hook syncs the registry to
-- this table. Admins map external Cloud Identity groups onto these roles via
-- group_mappings — they don't create roles in the UI.
CREATE TABLE IF NOT EXISTS internal_roles (
    id           VARCHAR PRIMARY KEY,
    key          VARCHAR UNIQUE NOT NULL,
    display_name VARCHAR NOT NULL,
    description  TEXT,
    owner_module VARCHAR,
    -- v9: implies is a JSON array of role keys this role transitively grants.
    -- Example: core.admin.implies = ["core.km_admin"], core.km_admin.implies =
    -- ["core.analyst"], core.analyst.implies = ["core.viewer"]. Resolver does
    -- BFS expansion at lookup time. Stored as VARCHAR (DuckDB JSON-as-text)
    -- because JSON typing on legacy DuckDB versions can be uneven; aplikační
    -- vrstva serializes/deserializes via stdlib json. Refactor to native JSON
    -- column is straightforward when we drop pre-1.0 compat.
    implies      VARCHAR DEFAULT '[]',
    -- v9: is_core distinguishes the seeded core.* hierarchy roles (viewer,
    -- analyst, km_admin, admin — the legacy users.role enum) from
    -- module-registered capability roles. UI uses this to render the user
    -- detail page differently (single-select for core, multi-select for
    -- module roles). Module authors should never set is_core=true.
    is_core      BOOLEAN NOT NULL DEFAULT false,
    created_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
    updated_at   TIMESTAMP NOT NULL DEFAULT current_timestamp
);

-- External-to-internal group mapping: which Cloud Identity groups grant which
-- internal role. Many-to-many. The resolver joins this table at sign-in and
-- writes the resulting role keys into session.internal_roles for cheap lookup
-- on subsequent requests.
CREATE TABLE IF NOT EXISTS group_mappings (
    id                VARCHAR PRIMARY KEY,
    external_group_id VARCHAR NOT NULL,
    internal_role_id  VARCHAR NOT NULL REFERENCES internal_roles(id),
    assigned_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
    assigned_by       VARCHAR,
    UNIQUE (external_group_id, internal_role_id)
);

-- v9: direct user → internal role grants. Complementary to group_mappings:
-- group_mappings drives session-cached resolution at sign-in for OAuth users,
-- user_role_grants drives DB-backed resolution for PAT/headless callers and
-- persists across sessions. require_internal_role checks both paths.
-- The v8→v9 backfill seeds one row per existing user with source='auto-seed'
-- mirroring their pre-v9 users.role value; admin-issued grants use
-- source='direct'.
CREATE TABLE IF NOT EXISTS user_role_grants (
    id                VARCHAR PRIMARY KEY,
    user_id           VARCHAR NOT NULL REFERENCES users(id),
    internal_role_id  VARCHAR NOT NULL REFERENCES internal_roles(id),
    granted_at        TIMESTAMP NOT NULL DEFAULT current_timestamp,
    granted_by        VARCHAR,
    source            VARCHAR DEFAULT 'direct',
    UNIQUE (user_id, internal_role_id)
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
                f"WHERE table_catalog='{ext_dir.name}' AND table_name='_remote_attach'"
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

_V4_TO_V5_MIGRATIONS = [
    # DuckDB doesn't allow ALTER TABLE ADD COLUMN with NOT NULL constraint,
    # so we add the column with a DEFAULT, backfill, then the app-level
    # code enforces non-null semantics (never inserts NULL for `active`).
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE",
    "UPDATE users SET active = TRUE WHERE active IS NULL",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS deactivated_by VARCHAR",
]

_V5_TO_V6_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS personal_access_tokens (
        id           VARCHAR PRIMARY KEY,
        user_id      VARCHAR NOT NULL,
        name         VARCHAR NOT NULL,
        token_hash   VARCHAR NOT NULL,
        prefix       VARCHAR NOT NULL,
        scopes       VARCHAR,
        created_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
        expires_at   TIMESTAMP,
        last_used_at TIMESTAMP,
        revoked_at   TIMESTAMP
    )
    """,
]

_V6_TO_V7_MIGRATIONS = [
    "ALTER TABLE personal_access_tokens ADD COLUMN IF NOT EXISTS last_used_ip VARCHAR",
]

_V7_TO_V8_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS internal_roles (
        id           VARCHAR PRIMARY KEY,
        key          VARCHAR UNIQUE NOT NULL,
        display_name VARCHAR NOT NULL,
        description  TEXT,
        owner_module VARCHAR,
        created_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
        updated_at   TIMESTAMP NOT NULL DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS group_mappings (
        id                VARCHAR PRIMARY KEY,
        external_group_id VARCHAR NOT NULL,
        internal_role_id  VARCHAR NOT NULL REFERENCES internal_roles(id),
        assigned_at       TIMESTAMP NOT NULL DEFAULT current_timestamp,
        assigned_by       VARCHAR,
        UNIQUE (external_group_id, internal_role_id)
    )
    """,
]

# v9 migration is multi-stage: ALTER + new table → seed core.* rows → backfill
# existing users.role values into user_role_grants → DROP users.role. The
# latter three steps run as Python helpers (_seed_core_roles +
# _backfill_users_role_to_grants) called from _ensure_schema, not raw SQL —
# they need DuckDB ConstraintException handling and per-user-role lookups
# that don't translate cleanly to a static SQL list.
_V8_TO_V9_MIGRATIONS = [
    "ALTER TABLE internal_roles ADD COLUMN IF NOT EXISTS implies VARCHAR DEFAULT '[]'",
    "ALTER TABLE internal_roles ADD COLUMN IF NOT EXISTS is_core BOOLEAN DEFAULT false",
    """
    CREATE TABLE IF NOT EXISTS user_role_grants (
        id                VARCHAR PRIMARY KEY,
        user_id           VARCHAR NOT NULL REFERENCES users(id),
        internal_role_id  VARCHAR NOT NULL REFERENCES internal_roles(id),
        granted_at        TIMESTAMP NOT NULL DEFAULT current_timestamp,
        granted_by        VARCHAR,
        source            VARCHAR DEFAULT 'direct',
        UNIQUE (user_id, internal_role_id)
    )
    """,
]

_V9_TO_V10_MIGRATIONS = [
    # New columns on knowledge_items for context engineering
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS confidence DOUBLE",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS domain VARCHAR",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS entities JSON",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS source_type VARCHAR DEFAULT 'claude_local_md'",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS source_ref VARCHAR",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS valid_from TIMESTAMP",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS valid_until TIMESTAMP",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS supersedes VARCHAR",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS sensitivity VARCHAR DEFAULT 'internal'",
    "ALTER TABLE knowledge_items ADD COLUMN IF NOT EXISTS is_personal BOOLEAN DEFAULT FALSE",
    # Backfill existing items
    "UPDATE knowledge_items SET source_type = 'claude_local_md' WHERE source_type IS NULL",
    # Contradiction tracking
    """
    CREATE TABLE IF NOT EXISTS knowledge_contradictions (
        id VARCHAR PRIMARY KEY,
        item_a_id VARCHAR NOT NULL,
        item_b_id VARCHAR NOT NULL,
        explanation TEXT,
        severity VARCHAR,
        suggested_resolution TEXT,
        resolved BOOLEAN DEFAULT FALSE,
        resolved_by VARCHAR,
        resolved_at TIMESTAMP,
        resolution VARCHAR,
        detected_at TIMESTAMP DEFAULT current_timestamp
    )
    """,
    # Track processed session files for verification detector
    """
    CREATE TABLE IF NOT EXISTS session_extraction_state (
        session_file VARCHAR PRIMARY KEY,
        username VARCHAR NOT NULL,
        processed_at TIMESTAMP DEFAULT current_timestamp,
        items_extracted INTEGER DEFAULT 0,
        file_hash VARCHAR
    )
    """,
]


# Core role seed data — single source of truth. Used by both _seed_core_roles
# (idempotent insert) and the v8→v9 backfill. Order matters: lowest privilege
# first so implies references resolve cleanly when expand_implies does BFS.
_CORE_ROLES_SEED = [
    # (key, display_name, description, implies)
    ("core.viewer", "Viewer",
     "Read-only access to permitted datasets.", []),
    ("core.analyst", "Analyst",
     "Default user role; query data, run analyses.", ["core.viewer"]),
    ("core.km_admin", "Knowledge-management admin",
     "Manages metric definitions and column metadata.", ["core.analyst"]),
    ("core.admin", "Administrator",
     "Full system access; bypasses dataset_permissions.", ["core.km_admin"]),
]

# Maps the legacy users.role string values onto core.* keys for the v8→v9
# backfill. Anything unrecognized falls back to core.viewer — safest default
# for existing rows that somehow held a value outside the documented enum.
_LEGACY_ROLE_TO_CORE_KEY = {
    "viewer": "core.viewer",
    "analyst": "core.analyst",
    "km_admin": "core.km_admin",
    "admin": "core.admin",
}


def _seed_core_roles(conn: duckdb.DuckDBPyConnection) -> None:
    """Idempotently insert/refresh the four core.* hierarchy roles.

    Called from _ensure_schema on every system-DB connect (the unconditional
    tail call below the migration guard) — fresh installs need the rows to
    exist before any user_role_grants can reference them, and existing DBs
    benefit from the safety net if a deployment somehow loses a row
    (e.g. accidental admin DELETE). Implies field is rewritten on every call
    to keep the hierarchy in sync with code; display_name + description are
    rewritten too so a doc tweak deploys without manual SQL.
    """
    import json as _json
    import uuid as _uuid

    for key, display_name, description, implies in _CORE_ROLES_SEED:
        existing = conn.execute(
            "SELECT id FROM internal_roles WHERE key = ?", [key]
        ).fetchone()
        implies_json = _json.dumps(implies)
        if existing:
            conn.execute(
                """UPDATE internal_roles
                   SET display_name = ?, description = ?, implies = ?,
                       is_core = true, owner_module = 'core',
                       updated_at = current_timestamp
                   WHERE id = ?""",
                [display_name, description, implies_json, existing[0]],
            )
        else:
            conn.execute(
                """INSERT INTO internal_roles
                   (id, key, display_name, description, owner_module, implies, is_core)
                   VALUES (?, ?, ?, ?, 'core', ?, true)""",
                [str(_uuid.uuid4()), key, display_name, description, implies_json],
            )


def _backfill_users_role_to_grants(conn: duckdb.DuckDBPyConnection) -> None:
    """One-shot: convert legacy users.role values into user_role_grants rows.

    Runs as part of the v8→v9 migration, after _seed_core_roles populated the
    target internal_roles rows and before users.role is dropped. Idempotent
    via the (user_id, internal_role_id) UNIQUE constraint — re-run is safe.
    """
    import uuid as _uuid

    # Verify users.role column still exists (we may be re-running after a
    # half-applied migration); skip silently if it's already gone.
    has_role_col = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'users' AND column_name = 'role'"
    ).fetchone()
    if not has_role_col:
        return

    rows = conn.execute(
        "SELECT id, role FROM users WHERE role IS NOT NULL"
    ).fetchall()
    backfilled = 0
    for user_id, role_str in rows:
        role_key = _LEGACY_ROLE_TO_CORE_KEY.get(role_str, "core.viewer")
        role_row = conn.execute(
            "SELECT id FROM internal_roles WHERE key = ?", [role_key]
        ).fetchone()
        if not role_row:
            logger.warning(
                "v9 backfill: core role %s missing — skipping user %s",
                role_key, user_id,
            )
            continue
        try:
            conn.execute(
                """INSERT INTO user_role_grants
                   (id, user_id, internal_role_id, granted_by, source)
                   VALUES (?, ?, ?, 'system:v9-backfill', 'auto-seed')""",
                [str(_uuid.uuid4()), user_id, role_row[0]],
            )
            backfilled += 1
        except duckdb.ConstraintException:
            pass  # already granted (idempotent re-run)
    if backfilled:
        logger.info(
            "v9 backfill: seeded user_role_grants for %d existing user(s)",
            backfilled,
        )

# Per-detection evidence rows — one knowledge_item can accumulate multiple
# evidence rows over time (each new analyst confirmation adds one). Persisting
# user_quote + detection_type per row is what enables future Bayesian re-
# calibration and "additional verifiers" boost computation. See Q3 of
# docs/pd-ps-comments.md.
_V10_TO_V11_MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS verification_evidence (
        id VARCHAR PRIMARY KEY,
        item_id VARCHAR NOT NULL,
        source_user VARCHAR,
        source_ref VARCHAR,
        detection_type VARCHAR,
        user_quote TEXT,
        created_at TIMESTAMP DEFAULT current_timestamp
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_verification_evidence_item ON verification_evidence(item_id)",
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
            # Fresh-install seed is handled by the unconditional
            # _seed_core_roles call at the bottom of _ensure_schema —
            # left as a no-op branch here so the migration ladder still
            # reads chronologically.
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
            if current < 5:
                for sql in _V4_TO_V5_MIGRATIONS:
                    conn.execute(sql)
            if current < 6:
                for sql in _V5_TO_V6_MIGRATIONS:
                    conn.execute(sql)
            if current < 7:
                for sql in _V6_TO_V7_MIGRATIONS:
                    conn.execute(sql)
            if current < 8:
                for sql in _V7_TO_V8_MIGRATIONS:
                    conn.execute(sql)
            if current < 9:
                for sql in _V8_TO_V9_MIGRATIONS:
                    conn.execute(sql)
                # v9 finalize: seed core.* roles, backfill grants from
                # legacy users.role, then drop the column. Order matters —
                # backfill needs the seed rows to exist; drop must be last.
                _seed_core_roles(conn)
                _backfill_users_role_to_grants(conn)
                # DuckDB rejects DROP COLUMN while user_role_grants FK
                # references users(id), so we NULL the legacy values instead
                # — UserRepository ignores the column going forward. Physical
                # drop is deferred to a future schema-rebuild migration.
                # Skip UPDATE if the column never existed (e.g. test fixtures
                # starting from v2/v3 with a hand-crafted minimal users table).
                has_role_col = conn.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'users' AND column_name = 'role'"
                ).fetchone()
                if has_role_col:
                    conn.execute("UPDATE users SET role = NULL")
            if current < 10:
                for sql in _V9_TO_V10_MIGRATIONS:
                    conn.execute(sql)
            conn.execute(
                "UPDATE schema_version SET version = ?, applied_at = current_timestamp",
                [SCHEMA_VERSION],
            )

    # Always run the core-role seed when the DB is on a version this binary
    # understands — the per-connect safety net the function's docstring
    # promises. UPSERTs four rows; near-zero cost. Three reasons this lives
    # OUTSIDE the migration guard:
    #   1. recovery — if a row gets DELETEd manually (or a doc-tweak release
    #      lands a new display_name), the next process start re-syncs without
    #      operator action;
    #   2. fresh installs — the (current == 0) branch above no longer needs
    #      its own seed call;
    #   3. v8→v9 migration — keeps its own _seed_core_roles call inside the
    #      block because _backfill_users_role_to_grants depends on the rows
    #      existing first; this tail call is the redundant-but-cheap follow-up.
    #
    # Skip when current > SCHEMA_VERSION — that's the future-version-is-noop
    # rollback contract (future schema may not even have an internal_roles
    # table; this binary leaves it alone).
    if get_schema_version(conn) <= SCHEMA_VERSION:
        _seed_core_roles(conn)


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
